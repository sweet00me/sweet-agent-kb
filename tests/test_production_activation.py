"""正式接管离线回归：只使用临时 SQLite 和模拟进程/服务，不向真实进程发信号。

验证旧请求保留、新请求与忙状态拒绝、身份变化保护、优雅排空及 Serve 旧入口保护。
"""

import copy
import io
import json
import signal
import sqlite3
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from deploy import activate_production as activation


OLD_SERVE = {'TCP': {'8080': {'HTTP': True}}, 'Web': {
    activation.HOSTNAME + ':8080': {'Handlers': {'/': {'Path': '/fixture/other-project'}}},
}}


def with_https():
    config = copy.deepcopy(OLD_SERVE)
    config['TCP']['443'] = {'HTTPS': True}
    config['Web'][activation.HOSTNAME + ':443'] = {'Handlers': {'/': {'Proxy': activation.TARGET}}}
    return config


def request(status='running', updated='2026-10-22T08:25:31+00:00'):
    return {'request_id': 'req_fixture_orphan', 'thread_id': 'thread-fixture', 'operation': 'review',
            'status': status, 'created_at': '2026-10-22T08:25:31+00:00', 'updated_at': updated,
            'payload_sha256': 'fixture-only'}


def diagnostic(status='running', checkpoint='unfinished'):
    return {'journal_status': status, 'checkpoint_status': checkpoint, 'automatic_replay_allowed': False}


class ActivationSafetyTests(unittest.TestCase):
    started = datetime.fromisoformat('2026-10-22T11:16:47+00:00').timestamp()

    def test_old_incomplete_request_is_only_read_and_not_marked_success(self):
        rows = [request()]
        baseline = copy.deepcopy(rows)
        probe = Mock(return_value=diagnostic())
        self.assertEqual(activation.check_requests(rows, self.started, probe), 1)
        probe.assert_called_once_with('/api/v1/requests/req_fixture_orphan')
        self.assertEqual(rows, baseline)
        self.assertEqual(rows[0]['status'], 'running')

    def test_current_process_requests_block_before_probing_or_stopping(self):
        for status in ('accepted', 'running', 'uncertain'):
            with self.subTest(status=status):
                probe = Mock()
                with self.assertRaises(activation.ActivationError):
                    activation.check_requests([request(status, '2026-10-22T11:17:00+00:00')], self.started, probe)
                probe.assert_not_called()

    def test_busy_checkpoint_changed_status_and_ambiguous_time_are_rejected(self):
        for result in (diagnostic(checkpoint='not_checked'), diagnostic(status='completed')):
            with self.assertRaises(activation.ActivationError):
                activation.check_requests([request()], self.started, Mock(return_value=result))
        with self.assertRaises(activation.ActivationError):
            activation.check_requests([request(updated='2026-10-22T08:25:31')], self.started, Mock())

    def test_listener_must_be_a_unique_loopback_process(self):
        line = 'LISTEN 0 2048 127.0.0.1:8010 0.0.0.0:* users:(("python",pid=123,fd=14))'
        self.assertEqual(activation.listener_pid(line), 123)
        for bad in ('', 'incomplete', line.replace('127.0.0.1:8010', '0.0.0.0:8010'),
                    line + '\n' + line, line + ' users:(("python",pid=456,fd=14))'):
            with self.subTest(value=bad), self.assertRaises(activation.ActivationError):
                activation.listener_pid(bad)

    def test_readonly_sqlite_preserves_all_rows_and_file_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'agent_sessions.sqlite3'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE api_requests(request_id TEXT,thread_id TEXT,operation TEXT,status TEXT,created_at TEXT,updated_at TEXT,payload_sha256 TEXT)')
                connection.execute('INSERT INTO api_requests VALUES (:request_id,:thread_id,:operation,:status,:created_at,:updated_at,:payload_sha256)', request())
            with sqlite3.connect(root / 'langgraph_agent.sqlite3') as connection:
                connection.execute('CREATE TABLE checkpoints(thread_id TEXT,checkpoint_ns TEXT,checkpoint_id TEXT,type TEXT,checkpoint BLOB,metadata BLOB)')
                connection.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?,?)',
                                   ('thread-fixture', '', 'checkpoint-1', 'fixture', b'fixture-body', b'{}'))
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            self.assertEqual(activation.request_rows(db), [request()])
            with patch.object(activation, 'SQLITE', root), patch.object(activation, 'request_rows', return_value=[request()]):
                first = activation.persistent_snapshot()
                self.assertEqual(first, activation.persistent_snapshot())
                self.assertEqual(first[0][0]['status'], 'running')
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})

    def test_private_https_preserves_existing_port_and_rejects_collisions(self):
        activation.verify_serve(OLD_SERVE, with_https())
        self.assertFalse(activation.validate_serve_target(OLD_SERVE))
        self.assertTrue(activation.validate_serve_target(with_https()))
        for mutation in ('old-path', 'funnel', 'wrong-proxy'):
            config = with_https()
            if mutation == 'old-path':
                config['Web'][activation.HOSTNAME + ':8080']['Handlers']['/']['Path'] = '/changed'
            elif mutation == 'funnel':
                config['AllowFunnel'] = {activation.HOSTNAME + ':443': True}
            else:
                config['Web'][activation.HOSTNAME + ':443']['Handlers']['/']['Proxy'] = 'http://127.0.0.1:9999'
            with self.subTest(mutation=mutation), self.assertRaises(activation.ActivationError):
                activation.verify_serve(OLD_SERVE, config)
        occupied = {'TCP': {'443': {'HTTPS': True}}}
        with self.assertRaises(activation.ActivationError):
            activation.validate_serve_target(occupied)

    def prepare_drain(self, ticks=123):
        self.enterContext(patch.object(activation.os, 'pidfd_open', return_value=77, create=True))
        close = self.enterContext(patch.object(activation.os, 'close'))
        self.enterContext(patch.object(activation, 'process_identity', return_value=(ticks, self.started)))
        self.enterContext(patch.object(activation, 'current_listener', return_value=222))
        self.enterContext(patch.object(activation, 'service_pid', return_value=0))
        self.enterContext(patch.object(activation, 'service_state', return_value='inactive'))
        self.enterContext(patch.object(activation, 'request_rows', return_value=[request()]))
        self.enterContext(patch.object(activation, 'check_requests'))
        return close

    def test_pid_reuse_never_receives_a_signal(self):
        close = self.prepare_drain(ticks=456)
        with patch.object(activation.signal, 'pidfd_send_signal', create=True) as send, self.assertRaises(activation.ActivationError):
            activation.drain({'pid': 222, 'ticks': 123})
        send.assert_not_called()
        close.assert_called_once_with(77)

    def test_graceful_drain_sends_one_sigterm_and_waits_without_killing(self):
        close = self.prepare_drain()
        with patch.object(activation.signal, 'pidfd_send_signal', create=True) as send, \
                patch.object(activation.select, 'select', side_effect=[([], [], []), ([77], [], [])]) as wait, \
                patch.object(activation, 'command', return_value=''), redirect_stdout(io.StringIO()):
            activation.drain({'pid': 222, 'ticks': 123})
        send.assert_called_once_with(77, signal.SIGTERM)
        self.assertEqual(wait.call_count, 2)
        close.assert_called_once_with(77)

    def pipeline(self, *, managed=False, https=False, changed=False, ready_error=False):
        events = []
        plan = {'managed': managed, 'https_exists': https, 'serve': with_https() if https else OLD_SERVE}
        def call(*args, **kwargs):
            if args[0] == '/usr/bin/systemctl':
                events.append('start')
                return ''
            if args[1:] == ('serve', 'status', '--json'):
                events.append('read-serve')
                return json.dumps(with_https() if https or 'add-https' in events else OLD_SERVE)
            if args[1:3] == ('serve', '--bg'):
                events.append('add-https')
                return ''
            self.fail(f'意外命令：{args}')
        self.enterContext(patch.object(activation, 'drain', side_effect=lambda _plan: events.append('drain')))
        self.enterContext(patch.object(activation, 'persistent_snapshot', side_effect=['same', 'changed' if changed else 'same']))
        self.enterContext(patch.object(activation, 'command', side_effect=call))
        def ready():
            events.append('ready')
            if ready_error:
                raise activation.ActivationError('fixture startup failed')
        self.enterContext(patch.object(activation, 'wait_ready', side_effect=ready))
        self.enterContext(patch.object(activation, 'verify_tls', side_effect=lambda: events.append('tls')))
        return plan, events

    def test_order_is_drain_start_health_then_add_https(self):
        plan, events = self.pipeline()
        with redirect_stdout(io.StringIO()):
            activation.activate(plan)
        self.assertEqual(events, ['drain', 'start', 'ready', 'read-serve', 'add-https', 'read-serve', 'tls'])

    def test_startup_failure_and_persisted_state_change_do_not_modify_serve(self):
        for kwargs in ({'ready_error': True}, {'changed': True}):
            with self.subTest(kwargs=kwargs):
                plan, events = self.pipeline(**kwargs)
                with self.assertRaises(activation.ActivationError):
                    activation.activate(plan)
                self.assertNotIn('add-https', events)

    def test_already_active_recheck_never_restarts_backend_or_rewrites_https(self):
        plan, events = self.pipeline(managed=True, https=True)
        with redirect_stdout(io.StringIO()):
            activation.activate(plan)
        self.assertEqual(events, ['ready', 'read-serve', 'tls'])

    def test_missing_root_is_rejected_before_any_process_or_service_action(self):
        with patch.object(activation.os, 'geteuid', return_value=1000), \
                patch('sys.argv', ['activate-production']), patch.object(activation, 'preflight') as preflight, \
                patch.object(activation, 'activate') as activate, redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as error:
            activation.main()
        self.assertEqual(error.exception.code, 1)
        preflight.assert_not_called()
        activate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
