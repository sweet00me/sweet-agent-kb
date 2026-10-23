"""正式接管：核实旧进程及遗留请求、pidfd 优雅退出、systemd 就绪及私有 HTTPS。

只在已授权的维护窗口运行；数据库始终只读，不恢复分析、不更改云端开关。
先通过 root 验证才允许发信号；旧进程退出前绝不启动第二个后端，不使用强杀。
"""

import argparse
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
import re
import select
import signal
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

PROJECT = Path('/home/sweet/Workspace/sweet-agent-kb')
PYTHON = Path('/home/sweet/miniconda3/envs/agent_kb/bin/python')
SQLITE = Path('/home/sweet/Storage/KnowledgeBase/sqlite')
SERVICE = 'sweet-research-agent.service'
HOSTNAME = 'sweetserver.taile94715.ts.net'
TARGET = 'http://127.0.0.1:8088'
OLD_ARGS = ['-m', 'uvicorn', 'app.api.main:app', '--host', '127.0.0.1', '--port', '8010', '--workers', '1']
MANAGED_ARGS = OLD_ARGS + ['--proxy-headers', '--forwarded-allow-ips', '127.0.0.1', '--no-access-log']
ACTIVE_REQUESTS = {'accepted', 'running', 'uncertain'}


class ActivationError(RuntimeError):
    """安全前提或接管检查不满足；不通过重试分析或强杀修复。"""


def command(*args, timeout=15, stream=False):
    return subprocess.run(args, check=True, capture_output=not stream, text=True, timeout=timeout).stdout or ''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get_json(path):
    # 仅访问回环代理，禁止继承代理或跟随重定向，不触发聊天/审批 POST。
    with build_opener(ProxyHandler({}), NoRedirect()).open(TARGET + path, timeout=5) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def health():
    data = get_json('/api/v1/health')
    if data.get('status') != 'ok' or data.get('service') != 'sweet-research-agent':
        raise ActivationError('API 健康响应不符')


def service_pid():
    return int(command('/usr/bin/systemctl', 'show', SERVICE, '-p', 'MainPID', '--value').strip())


def service_state():
    return command('/usr/bin/systemctl', 'show', SERVICE, '-p', 'ActiveState', '--value').strip()


def listener_pid(output):
    lines = output.strip().splitlines()
    if len(lines) != 1 or len(lines[0].split()) < 4 or lines[0].split()[3] != '127.0.0.1:8010':
        raise ActivationError('8010 不是预期的唯一回环监听，停止接管')
    owners = set(re.findall(r'pid=(\d+)', lines[0]))
    if len(owners) != 1:
        raise ActivationError('无法唯一识别 8010 进程，不会发送信号')
    return int(owners.pop())


def current_listener():
    return listener_pid(command('/usr/bin/ss', '-Hltpn', 'sport = :8010'))


def process_identity(pid, managed):
    proc = Path('/proc') / str(pid)
    if (proc.stat().st_uid != pwd.getpwnam('sweet').pw_uid
            or (proc / 'cwd').resolve() != PROJECT
            or (proc / 'exe').resolve() != PYTHON.resolve()):
        raise ActivationError('8010 进程的用户、目录或 Python 不符，不会发送信号')
    args = (proc / 'cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    if args[1:] != (MANAGED_ARGS if managed else OLD_ARGS):
        raise ActivationError('后端启动参数与审计结果不同，不会发送信号')
    stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    ticks = int(stat[19])
    boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
    return ticks, boot + ticks / os.sysconf('SC_CLK_TCK')


def request_rows(database=SQLITE / 'agent_sessions.sqlite3'):
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            'SELECT request_id,thread_id,operation,status,created_at,updated_at,payload_sha256 '
            'FROM api_requests ORDER BY request_id')]


def check_requests(rows, process_started, diagnose=get_json):
    orphans = [row for row in rows if row['status'] in ACTIVE_REQUESTS]
    for row in orphans:
        dates = [datetime.fromisoformat(row[key]) for key in ('created_at', 'updated_at')]
        if any(value.tzinfo is None for value in dates):
            raise ActivationError('请求时间缺少时区，不能判定为前进程遗留')
        times = [value.timestamp() for value in dates]
        if max(times) >= process_started:
            raise ActivationError('有当前进程接受的未确认请求，先等待并核对，不自动切换')
    # 非阻塞诊断只有取得现有 graph_lock 才会返回 Checkpoint；不将旧记录改为成功。
    probes = orphans or rows[-1:]
    for row in probes:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}', row['request_id']):
            raise ActivationError('请求 ID 不符合已有接口规则，停止检查')
        diagnostic = diagnose('/api/v1/requests/' + row['request_id'])
        if diagnostic.get('checkpoint_status') not in {
                'unfinished', 'idle', 'awaiting_approval', 'interrupted', 'no_checkpoint'}:
            raise ActivationError('Graph 当前忙或无法只读确认状态，停止接管')
        if diagnostic.get('journal_status') != row['status'] or diagnostic.get('automatic_replay_allowed') is not False:
            raise ActivationError('请求状态已变化或恢复规则不符，停止接管')
    return len(orphans)


def persistent_snapshot():
    rows = request_rows()
    checkpoints = []
    with sqlite3.connect((SQLITE / 'langgraph_agent.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2) as connection:
        connection.execute('PRAGMA query_only=ON')
        for thread in sorted({row['thread_id'] for row in rows}):
            latest = connection.execute(
                'SELECT checkpoint_id,type,checkpoint,metadata FROM checkpoints '
                'WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id DESC LIMIT 1',
                (thread, ''),
            ).fetchone()
            if latest:
                ident, kind, body, metadata = latest
                metadata_bytes = metadata.encode() if isinstance(metadata, str) else metadata
                checkpoints.append((thread, ident, kind, hashlib.sha256(body).hexdigest(), hashlib.sha256(metadata_bytes).hexdigest()))
    # 不打印正文或原始响应；只在内存中比较持久化请求元数据及 Checkpoint 摘要。
    return rows, checkpoints


def desired_https(config):
    web = config.get('Web', {}).get(HOSTNAME + ':443', {})
    return (config.get('TCP', {}).get('443') == {'HTTPS': True}
            and web.get('Handlers') == {'/': {'Proxy': TARGET}}
            and not config.get('AllowFunnel', {}).get(HOSTNAME + ':443', False))


def validate_serve_target(config):
    if '443' in config.get('TCP', {}) or any(key.endswith(':443') for key in config.get('Web', {})):
        if not desired_https(config):
            raise ActivationError('Serve 443 已有其他配置，不覆盖')
        return True
    if config.get('AllowFunnel', {}).get(HOSTNAME + ':443', False):
        raise ActivationError('443 有 Funnel 授权状态，停止而不更改基础配置')
    return False


def verify_serve(before, after):
    if not desired_https(after):
        raise ActivationError('新 HTTPS 代理或私有访问检查不符')
    expected = copy.deepcopy(after)
    for group, key in [('TCP', '443'), ('Web', HOSTNAME + ':443')]:
        expected[group].pop(key)
        if not expected[group] and group not in before:
            expected.pop(group)
    if not before.get('AllowFunnel') and not expected.get('AllowFunnel'):
        expected.pop('AllowFunnel', None)
    original = copy.deepcopy(before)
    if not original.get('AllowFunnel'):
        original.pop('AllowFunnel', None)
    if expected != original:
        raise ActivationError('原 Serve 配置发生变化，请人工核对；不 reset 或覆盖其他服务')


def preflight():
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ActivationError('当前解释器缺少 pidfd，请使用 activate-production.sh 调用 Ubuntu 系统 Python')
    if PROJECT.resolve() != PROJECT or Path(__file__).resolve() != PROJECT / 'deploy/activate_production.py':
        raise ActivationError('激活脚本或项目路径不符')
    for source, installed in [
        ('deploy/systemd/sweet-research-agent.service', '/etc/systemd/system/sweet-research-agent.service'),
        ('deploy/nginx/sweet-research-agent.conf', '/etc/nginx/sites-available/sweet-research-agent.conf'),
    ]:
        path = Path(installed)
        if path.is_symlink() or path.read_bytes() != (PROJECT / source).read_bytes():
            raise ActivationError('已安装配置与审阅版本不符，停止接管')
    command(str(PYTHON), '-I', str(PROJECT / 'deploy/check_runtime.py'))
    command('/usr/bin/systemctl', 'is-enabled', SERVICE)
    if os.geteuid() == 0:
        command('/usr/sbin/nginx', '-t')
    health()
    config = Path('/home/sweet/.config/sweet-research-agent/runtime.env')
    if config.is_symlink() or config.stat().st_uid != pwd.getpwnam('sweet').pw_uid or config.stat().st_mode & 0o777 != 0o600:
        raise ActivationError('运行环境文件的用户或私有权限不符')
    if config.read_bytes() != (PROJECT / 'deploy/runtime.env.example').read_bytes():
        raise ActivationError('运行环境与已审计代理配置不同，先核对，不停止旧后端')
    node = json.loads(command('/usr/bin/tailscale', 'status', '--json'))
    if node.get('BackendState') != 'Running' or node.get('Self', {}).get('DNSName', '').rstrip('.') != HOSTNAME:
        raise ActivationError('Tailscale 在线状态或节点域名不符')
    serve = json.loads(command('/usr/bin/tailscale', 'serve', 'status', '--json'))
    exists = validate_serve_target(serve)
    if not exists and command('/usr/bin/ss', '-Hlt', 'sport = :443').strip():
        raise ActivationError('443 已被其他进程占用，不会覆盖或停止它')
    pid = current_listener()
    managed = service_pid() == pid
    if (not managed and (service_pid() != 0 or service_state() != 'inactive')) or (managed and service_state() != 'active'):
        raise ActivationError('systemd 与监听进程不一致，停止接管')
    ticks, started = process_identity(pid, managed)
    orphans = check_requests(request_rows(), started)
    print(f'只读检查通过；保留 {orphans} 条前进程遗留记录，不重放、不标记成功。', flush=True)
    return {'pid': pid, 'ticks': ticks, 'started': started, 'managed': managed, 'serve': serve, 'https_exists': exists}


def drain(plan):
    # pidfd 锁定进程身份，避免检查后 PID 被重用而误伤其他服务。
    fd = os.pidfd_open(plan['pid'])
    try:
        ticks, started = process_identity(plan['pid'], False)
        if ticks != plan['ticks'] or current_listener() != plan['pid'] or service_pid() != 0 or service_state() != 'inactive':
            raise ActivationError('切换前进程身份已改变，不发送信号')
        check_requests(request_rows(), started)
        print('发送一次 SIGTERM，等待 Uvicorn、SSE 执行器和同步服务优雅退出；不会强杀。', flush=True)
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        while not select.select([fd], [], [], 30)[0]:
            print('仍在等待现有连接或后台任务结束；未启动第二个后端。', flush=True)
    finally:
        os.close(fd)
    if command('/usr/bin/ss', '-Hlt', 'sport = :8010').strip():
        raise ActivationError('8010 尚未释放，不启动第二个后端')


def wait_ready(timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health()
            pid = service_pid()
            if pid > 0 and current_listener() == pid:
                process_identity(pid, True)
                return
        except (OSError, ValueError, ActivationError, subprocess.SubprocessError):
            pass
        time.sleep(1)
    raise ActivationError('systemd 后端尚未通过就绪检查；请查看 journal，不会继续修改 Serve')


def verify_tls():
    info = json.loads(command('/usr/bin/tailscale', 'status', '--json'))['Self']
    if info['DNSName'].rstrip('.') != HOSTNAME:
        raise ActivationError('Tailnet 名称与已审查 Nginx 域名不符')
    address = next(value for value in info['TailscaleIPs'] if ipaddress.ip_address(value).version == 4)
    args = ['/usr/bin/curl', '--noproxy', '*', '--fail', '--silent', '--show-error', '--max-time', '30',
            '--resolve', f'{HOSTNAME}:443:{address}']
    body = command(*args, f'https://{HOSTNAME}/api/v1/health', timeout=35)
    data = json.loads(body)
    if data.get('status') != 'ok' or data.get('service') != 'sweet-research-agent':
        raise ActivationError('HTTPS Health 未通过')
    homepage = command(*args, f'https://{HOSTNAME}/', timeout=35)
    if homepage.encode() != (Path('/srv/sweet-research-agent/current') / 'index.html').read_bytes():
        raise ActivationError('HTTPS 页面与当前正式前端不符')


def activate(plan):
    if not plan['managed']:
        drain(plan)
    baseline = persistent_snapshot()
    if not plan['managed']:
        command('/usr/bin/systemctl', 'start', SERVICE, timeout=30)
    wait_ready()
    if baseline != persistent_snapshot():
        raise ActivationError('接管期间请求或 Checkpoint 已变化，先人工核对；不更改 Serve')
    current = json.loads(command('/usr/bin/tailscale', 'serve', 'status', '--json'))
    if current != plan['serve']:
        raise ActivationError('Serve 在检查后发生变化，不覆盖')
    if not plan['https_exists']:
        command('/usr/bin/tailscale', 'serve', '--bg', '--https=443', '--yes', TARGET, timeout=60, stream=True)
        verify_serve(current, json.loads(command('/usr/bin/tailscale', 'serve', 'status', '--json')))
    verify_tls()
    print(f'接管与 HTTPS 检查通过：https://{HOSTNAME}/；原 Serve 及未完成任务保留。', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='仅只读核对，不创建锁、发信号或修改服务')
    args = parser.parse_args()
    try:
        if not args.check and os.geteuid() != 0:
            raise ActivationError('先在本机执行 sudo bash scripts/activate-production.sh；未停止任何进程')
        if args.check:
            preflight()
            return
        fd = os.open('/run/lock/sweet-research-agent-activate.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            activate(preflight())
    except (OSError, ValueError, RuntimeError, StopIteration, sqlite3.Error, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        parser.exit(1, f'接管尚未完成：{type(exc).__name__}: {exc}\n核对 systemctl/8010 状态及 journal；不自动重放任务或强杀，不重置 Serve。\n')


if __name__ == '__main__':
    main()
