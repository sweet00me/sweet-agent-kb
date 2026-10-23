
"""
文件：app/api/request_journal.py

用途：
    Sweet Research Agent 的 HTTP 请求幂等记录。

主要功能：
    1. 使用 request_id 唯一标识一次用户操作。
    2. 相同 request_id + 相同请求内容，只登记一次。
    3. 相同 request_id + 不同请求内容，拒绝执行。
    4. 记录请求执行状态。
    5. 保存已完成请求的响应。
    6. 对执行结果不确定的请求禁止自动重试。

说明：
    当前仅提供数据库能力。
    下一步才接入 FastAPI 路由。

安全边界：
    request_id 不是用户认证凭证。
    该模块提供请求级去重记录，不保证外部工具
    在所有崩溃场景下恰好执行一次。
"""

import hashlib
import json
import re

from contextlib import closing

from app.api import session_store


CREATE_REQUEST_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_requests (
    request_id TEXT PRIMARY KEY,

    thread_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (
        operation IN ('chat', 'review')
    ),

    payload_sha256 TEXT NOT NULL,

    status TEXT NOT NULL CHECK (
        status IN (
            'accepted',
            'running',
            'pending_approval',
            'completed',
            'uncertain'
        )
    ),

    response_json TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY (thread_id)
        REFERENCES chat_threads(thread_id)
);

CREATE INDEX IF NOT EXISTS idx_api_requests_thread
ON api_requests(thread_id, created_at);
"""


class RequestConflictError(Exception):
    """请求 ID 与已有记录不一致。"""


class RequestStateError(Exception):
    """请求状态不允许当前操作。"""


# =====================================
# 1. 初始化数据库
# =====================================

def initialize_request_journal():
    """
    创建请求表。

    使用 CREATE TABLE IF NOT EXISTS，
    不删除已有 Session 数据。
    """

    with closing(session_store.connect_db()) as conn:
        conn.executescript(CREATE_REQUEST_TABLE_SQL)
        conn.commit()


# =====================================
# 2. 计算请求内容指纹
# =====================================

def request_fingerprint(
    thread_id: str,
    operation: str,
    payload: dict,
) -> str:
    """
    对请求内容进行稳定的 SHA256 计算。

    不直接将原始用户问题保存到请求记录表。

    注意：
        SHA256 用于比较请求内容是否一致，
        不是身份认证或数字签名。
    """

    canonical = json.dumps(
        {
            "thread_id": thread_id,
            "operation": operation,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


# =====================================
# 3. 读取请求记录
# =====================================

def get_request(request_id: str) -> dict | None:
    """按 request_id 查询请求。"""

    with closing(session_store.connect_db()) as conn:

        row = conn.execute(
            """
            SELECT *
            FROM api_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

    if row is None:
        return None

    record = dict(row)

    # 数据库内部保存 JSON 字符串，
    # Python 调用方获得解码后的字典。
    response_json = record.pop("response_json")

    record["response"] = (
        json.loads(response_json)
        if response_json is not None
        else None
    )

    return record


# =====================================
# 4. 请求登记与去重
# =====================================

def register_request(
    *,
    request_id: str,
    thread_id: str,
    operation: str,
    payload: dict,
) -> tuple[dict, bool]:
    """
    登记请求。

    Returns:
        (请求记录, 是否首次创建)

    首次登记：
        返回 created=True。

    重复但内容一致：
        返回 created=False，不重新执行。

    重复但内容不同：
        抛出 RequestConflictError。

    要求：
        thread_id 必须已经注册到 chat_threads。
    """

    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}",
        request_id,
    ):
        raise ValueError("request_id 格式无效")

    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
        thread_id,
    ):
        raise ValueError("thread_id 格式无效")

    if operation not in ("chat", "review"):
        raise ValueError("未知的请求操作类型")

    if not isinstance(payload, dict):
        raise ValueError("payload 必须是字典")

    fingerprint = request_fingerprint(
        thread_id,
        operation,
        payload,
    )

    now = session_store.utc_now()

    with closing(session_store.connect_db()) as conn:

        with conn:

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO api_requests (
                    request_id,
                    thread_id,
                    operation,
                    payload_sha256,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    request_id,
                    thread_id,
                    operation,
                    fingerprint,
                    now,
                    now,
                ),
            )

            created = cursor.rowcount == 1

            row = conn.execute(
                """
                SELECT
                    thread_id,
                    operation,
                    payload_sha256
                FROM api_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

            if row is None:
                raise RuntimeError("请求登记失败")

            if (
                row["thread_id"] != thread_id
                or row["operation"] != operation
                or row["payload_sha256"] != fingerprint
            ):
                raise RequestConflictError(
                    "request_id 已用于另一项不同的请求"
                )

    record = get_request(request_id)

    return record, created


# =====================================
# 5. 原子化状态转换
# =====================================

def transition_request(
    request_id: str,
    *,
    expected_statuses: tuple[str, ...],
    new_status: str,
    response: dict | None = None,
):
    """
    仅允许从指定的旧状态转换到新状态。

    使用 SQLite 事务保证状态检查和更新
    在同一连接中完成。

    已完成的请求不能再次变成 running。
    """

    valid_statuses = {
        "accepted",
        "running",
        "pending_approval",
        "completed",
        "uncertain",
    }

    if new_status not in valid_statuses:
        raise ValueError("未知的目标状态")

    if (
        not expected_statuses
        or any(
            value not in valid_statuses
            for value in expected_statuses
        )
    ):
        raise ValueError("原状态集合无效")

    response_json = (
        json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if response is not None
        else None
    )

    now = session_store.utc_now()

    with closing(session_store.connect_db()) as conn:

        # BEGIN IMMEDIATE 防止多个写入者同时完成
        # “读取旧状态 → 更新状态”的流程。
        conn.execute("BEGIN IMMEDIATE")

        try:

            row = conn.execute(
                """
                SELECT status
                FROM api_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

            if row is None:
                raise RequestStateError(
                    "请求不存在"
                )

            if row["status"] not in expected_statuses:
                raise RequestStateError(
                    "当前请求状态不允许此操作："
                    + row["status"]
                )

            conn.execute(
                """
                UPDATE api_requests
                SET status = ?,
                    response_json = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    new_status,
                    response_json,
                    now,
                    request_id,
                ),
            )

            conn.commit()

        except Exception:
            conn.rollback()
            raise

    return get_request(request_id)


# =====================================
# 6. 常用状态操作
# =====================================

def start_request(request_id: str):
    """只允许首次登记的请求开始执行。"""

    return transition_request(
        request_id,
        expected_statuses=("accepted",),
        new_status="running",
    )


def finish_request(
    request_id: str,
    *,
    status: str,
    response: dict,
):
    """
    记录一次 Agent 调用的最终结果。

    status：
        completed 或 pending_approval
    """

    if status not in (
        "completed",
        "pending_approval",
    ):
        raise ValueError("不支持的完成状态")

    return transition_request(
        request_id,
        expected_statuses=("running",),
        new_status=status,
        response=response,
    )


def mark_uncertain(request_id: str):
    """
    将可能已经产生副作用的请求标记为不确定。

    不自动重新执行。
    """

    return transition_request(
        request_id,
        expected_statuses=("accepted", "running"),
        new_status="uncertain",
    )


# =====================================
# 7. 原子保存助手回答与请求完成状态
# =====================================

def complete_request_with_message(
    request_id: str,
    *,
    thread_id: str,
    answer: str,
    response: dict,
):
    """
    文件：app/api/request_journal.py

    用途：
        在同一个 SQLite 事务中完成两项操作：

        1. 将 Agent 最终回答写入 chat_messages。
        2. 将对应请求标记为 completed。

    保证：
        两项操作要么一起成功，要么一起回滚。

    不保证：
        LangGraph Checkpoint 与 Session SQLite
        之间的跨数据库原子性。

    前置条件：
        - request_id 已登记。
        - 请求状态为 running。
        - response 是完整的 completed 响应。
    """

    # =====================================
    # 1. 严格检查输入
    # =====================================

    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("最终回答不能为空")

    if not isinstance(response, dict):
        raise ValueError("response 必须是字典")

    if (
        response.get("thread_id") != thread_id
        or response.get("status") != "completed"
        or response.get("answer") != answer
        or response.get("pending_approvals") != []
    ):
        raise ValueError(
            "最终响应与当前线程或回答不一致"
        )

    response_json = json.dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    now = session_store.utc_now()

    # =====================================
    # 2. 在同一个数据库连接中开启事务
    # =====================================

    with closing(session_store.connect_db()) as conn:

        conn.execute("BEGIN IMMEDIATE")

        try:

            # =================================
            # 3. 验证请求状态
            # =================================

            row = conn.execute(
                """
                SELECT thread_id, operation, status
                FROM api_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

            if row is None:
                raise RequestStateError(
                    "请求不存在"
                )

            if row["thread_id"] != thread_id:
                raise RequestConflictError(
                    "请求所属线程不匹配"
                )

            if row["status"] != "running":
                raise RequestStateError(
                    "请求不处于 running 状态："
                    + row["status"]
                )

            # =================================
            # 4. 保存完整助手消息
            # =================================

            conn.execute(
                """
                INSERT INTO chat_messages (
                    thread_id,
                    role,
                    content,
                    created_at
                )
                VALUES (?, 'assistant', ?, ?)
                """,
                (
                    thread_id,
                    answer,
                    now,
                ),
            )

            # =================================
            # 5. 更新会话活动时间
            # =================================

            cursor = conn.execute(
                """
                UPDATE chat_threads
                SET updated_at = ?
                WHERE thread_id = ?
                """,
                (now, thread_id),
            )

            if cursor.rowcount != 1:
                raise RequestStateError(
                    "会话不存在"
                )

            # =================================
            # 6. 完成请求记录
            # =================================

            cursor = conn.execute(
                """
                UPDATE api_requests
                SET
                    status = 'completed',
                    response_json = ?,
                    updated_at = ?
                WHERE request_id = ?
                  AND thread_id = ?
                  AND status = 'running'
                """,
                (
                    response_json,
                    now,
                    request_id,
                    thread_id,
                ),
            )

            if cursor.rowcount != 1:
                raise RequestStateError(
                    "请求完成状态更新失败"
                )

            # =================================
            # 7. 统一提交
            # =================================

            conn.commit()

        except Exception:

            # 上述任一 SQL 失败，
            # 包括已经执行的消息 INSERT，
            # 都会在这里回滚。
            conn.rollback()
            raise

    return get_request(request_id)
