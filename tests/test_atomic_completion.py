
"""
文件：tests/test_atomic_completion.py

用途：
    验证助手消息与请求完成状态的 SQLite 原子性。

测试内容：
    1. 模拟 Request Journal 更新失败。
    2. 检查助手消息是否回滚。
    3. 移除故障后重新完成请求。
    4. 检查消息与请求同时成功。
    5. 检查重复完成不会生成额外消息。

安全说明：
    使用临时 SQLite 数据库。
    不访问真实 Agent、DeepSeek 或知识库。
"""

import sqlite3
import uuid

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.api import session_store
from app.api import request_journal


def main():

    with TemporaryDirectory() as temp_dir:

        db_path = (
            Path(temp_dir) / "atomic_test.sqlite3"
        )

        with patch.object(
            session_store,
            "SESSION_DB_PATH",
            db_path,
        ):

            # =================================
            # 1. 准备临时数据库
            # =================================

            with closing(session_store.connect_db()) as conn:
                conn.executescript(
                    session_store.CREATE_TABLES_SQL
                )
                conn.commit()

            request_journal.initialize_request_journal()

            thread_id = (
                "research-" + uuid.uuid4().hex[:16]
            )

            request_id = (
                "req-" + uuid.uuid4().hex
            )

            session_store.create_thread(thread_id)

            session_store.append_message(
                thread_id,
                "user",
                "请解释 RAG。",
            )

            request_journal.register_request(
                request_id=request_id,
                thread_id=thread_id,
                operation="chat",
                payload={
                    "message": "请解释 RAG。"
                },
            )

            request_journal.start_request(request_id)

            answer = "RAG 是检索增强生成。"

            response = {
                "thread_id": thread_id,
                "status": "completed",
                "answer": answer,
                "pending_approvals": [],
            }

            # =================================
            # 2. 制造数据库写入故障
            # =================================

            # 当 api_requests 更新为 completed 时，
            # 强制 SQLite 抛出异常。
            #
            # 这是测试专用 Trigger，
            # 只存在于临时数据库。
            with closing(session_store.connect_db()) as conn:
                conn.executescript(
                    """
                    CREATE TRIGGER fail_completion
                    BEFORE UPDATE OF status
                    ON api_requests
                    WHEN NEW.status = 'completed'
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'simulated database failure'
                        );
                    END;
                    """
                )
                conn.commit()

            print("========== 1. 故障注入 ==========")

            try:
                request_journal.complete_request_with_message(
                    request_id,
                    thread_id=thread_id,
                    answer=answer,
                    response=response,
                )

                raise AssertionError(
                    "预期的数据库异常未出现"
                )

            except sqlite3.IntegrityError:
                print("PASS：成功触发模拟数据库故障")

            # =================================
            # 3. 检查事务回滚
            # =================================

            history = session_store.list_messages(
                thread_id
            )

            record = request_journal.get_request(
                request_id
            )

            assert len(history["items"]) == 1
            assert history["items"][0]["role"] == "user"

            assert record["status"] == "running"
            assert record["response"] is None

            print("PASS：助手消息已回滚")
            print("PASS：请求状态未错误变成 completed")

            # =================================
            # 4. 移除故障
            # =================================

            with closing(session_store.connect_db()) as conn:
                conn.execute(
                    "DROP TRIGGER fail_completion"
                )
                conn.commit()

            # =================================
            # 5. 正常原子提交
            # =================================

            print("\n========== 2. 正常提交 ==========")

            record = (
                request_journal.complete_request_with_message(
                    request_id,
                    thread_id=thread_id,
                    answer=answer,
                    response=response,
                )
            )

            assert record["status"] == "completed"
            assert record["response"] == response

            history = session_store.list_messages(
                thread_id
            )

            assert len(history["items"]) == 2

            assert [
                item["role"]
                for item in history["items"]
            ] == ["user", "assistant"]

            assert history["items"][1]["content"] == answer

            print("PASS：消息和请求结果同时提交成功")

            # =================================
            # 6. 检查重复完成
            # =================================

            print("\n========== 3. 重复完成保护 ==========")

            try:
                request_journal.complete_request_with_message(
                    request_id,
                    thread_id=thread_id,
                    answer=answer,
                    response=response,
                )

                raise AssertionError(
                    "重复完成没有被阻止"
                )

            except request_journal.RequestStateError:
                pass

            history = session_store.list_messages(
                thread_id
            )

            assert len(history["items"]) == 2

            print("PASS：重复完成未生成额外消息")

    print("\n原子提交测试全部通过")


if __name__ == "__main__":
    main()
