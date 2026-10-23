
"""
文件：tests/test_request_journal.py

用途：
    验证 API 请求记录、去重和状态转换。

特点：
    1. 使用临时 SQLite。
    2. 不调用 DeepSeek。
    3. 不执行 LangGraph。
    4. 不修改正式会话数据库。
"""

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
            Path(temp_dir) / "test_sessions.sqlite3"
        )

        # 将测试期间的数据库路径临时指向独立文件。
        with patch.object(
            session_store,
            "SESSION_DB_PATH",
            db_path,
        ):

            with closing(session_store.connect_db()) as conn:
                conn.executescript(
                    session_store.CREATE_TABLES_SQL
                )
                conn.commit()

            request_journal.initialize_request_journal()

            thread_id = "research-" + uuid.uuid4().hex[:16]
            request_id = "req-" + uuid.uuid4().hex

            session_store.create_thread(thread_id)

            # =================================
            # 1. 首次登记
            # =================================

            record, created = (
                request_journal.register_request(
                    request_id=request_id,
                    thread_id=thread_id,
                    operation="chat",
                    payload={
                        "message": "你好"
                    },
                )
            )

            assert created is True
            assert record["status"] == "accepted"

            print("PASS：首次请求登记成功")

            # =================================
            # 2. 重复请求
            # =================================

            record, created = (
                request_journal.register_request(
                    request_id=request_id,
                    thread_id=thread_id,
                    operation="chat",
                    payload={
                        "message": "你好"
                    },
                )
            )

            assert created is False

            print("PASS：重复请求未重复登记")

            # =================================
            # 3. 相同 ID 不同内容
            # =================================

            try:

                request_journal.register_request(
                    request_id=request_id,
                    thread_id=thread_id,
                    operation="chat",
                    payload={
                        "message": "不同的内容"
                    },
                )

                raise AssertionError(
                    "不同请求内容没有被拒绝"
                )

            except request_journal.RequestConflictError:
                pass

            print("PASS：请求 ID 冲突被拒绝")

            # =================================
            # 4. 开始执行
            # =================================

            record = request_journal.start_request(
                request_id
            )

            assert record["status"] == "running"

            try:
                request_journal.start_request(request_id)

                raise AssertionError(
                    "重复执行未被拦截"
                )

            except request_journal.RequestStateError:
                pass

            print("PASS：同一请求无法重复开始执行")

            # =================================
            # 5. 保存完成结果
            # =================================

            record = request_journal.finish_request(
                request_id,
                status="completed",
                response={
                    "thread_id": thread_id,
                    "status": "completed",
                    "answer": "你好，有什么可以帮助你？",
                    "pending_approvals": [],
                },
            )

            assert record["status"] == "completed"
            assert record["response"]["answer"].startswith("你好")

            print("PASS：完成结果持久化成功")

            # =================================
            # 6. 完成后不可重新执行
            # =================================

            try:
                request_journal.start_request(request_id)

                raise AssertionError(
                    "已完成请求被重新执行"
                )

            except request_journal.RequestStateError:
                pass

            print("PASS：已完成请求不可再次执行")

            # =================================
            # 7. 不确定状态禁止重新执行
            # =================================

            uncertain_id = "req-" + uuid.uuid4().hex

            request_journal.register_request(
                request_id=uncertain_id,
                thread_id=thread_id,
                operation="review",
                payload={
                    "decisions": {
                        "call_test": "approve"
                    }
                },
            )

            request_journal.start_request(uncertain_id)
            request_journal.mark_uncertain(uncertain_id)

            try:
                request_journal.start_request(uncertain_id)

                raise AssertionError(
                    "不确定请求被重新执行"
                )

            except request_journal.RequestStateError:
                pass

            print("PASS：不确定请求不会自动重试")

    print("\nRequest Journal 测试全部通过")


if __name__ == "__main__":
    main()
