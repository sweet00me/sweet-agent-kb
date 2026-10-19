
"""
文件：app/observability/tracing.py

用途：
    为 Sweet Research Agent 记录节点级执行轨迹。

主要功能：
    1. 记录节点开始与结束时间。
    2. 测量节点执行耗时。
    3. 区分完成、中断和异常。
    4. 持久化到 SQLite 的 node_trace 表。
    5. 通过包装函数复用追踪逻辑。

隐私说明：
    不保存用户问题、Prompt、工具参数或模型回答。

设计说明：
    采用同步 SQLite 写入。
    本版本适合当前单机 Agent 开发。
    尚不是分布式 Tracing 系统。
"""

import sqlite3
import time
import uuid

from datetime import datetime, timezone
from typing import Any, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt

from app.observability.audit import (
    AUDIT_DB_PATH,
    prepare_database,
    get_thread_id,
)


# =====================================
# 1. 创建节点追踪表
# =====================================

CREATE_TRACE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS node_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    span_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    node_name TEXT NOT NULL,

    started_at TEXT NOT NULL,
    finished_at TEXT,

    duration_ms REAL,

    status TEXT NOT NULL,
    error_type TEXT
)
"""


def utc_now() -> str:
    """返回带 UTC 时区信息的时间字符串。"""

    return datetime.now(
        timezone.utc
    ).isoformat(timespec="milliseconds")


# =====================================
# 2. 开始一次 Span
# =====================================

def start_span(
    config: RunnableConfig,
    node_name: str,
) -> str:
    """
    创建一条 running 状态的节点记录。

    Returns:
        span_id：本次节点执行的唯一 ID。
    """

    thread_id = get_thread_id(config)

    if not node_name.strip():
        raise ValueError("node_name 不能为空")

    span_id = uuid.uuid4().hex

    prepare_database()

    conn = sqlite3.connect(
        str(AUDIT_DB_PATH),
        timeout=10
    )

    try:
        conn.execute(CREATE_TRACE_TABLE_SQL)

        conn.execute(
            """
            INSERT INTO node_trace (
                span_id,
                thread_id,
                node_name,
                started_at,
                status
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                span_id,
                thread_id,
                node_name,
                utc_now(),
                "running",
            )
        )

        conn.commit()

    finally:
        conn.close()

    return span_id


# =====================================
# 3. 结束一次 Span
# =====================================

def finish_span(
    span_id: str,
    status: str,
    duration_ms: float,
    error_type: str | None = None,
):
    """
    更新节点的最终执行状态。

    completed：
        正常返回。

    interrupted：
        LangGraph 的 interrupt() 暂停。

    error：
        节点出现异常。

    注意：
        记录失败时抛出异常，不静默伪造执行成功。
    """

    valid_statuses = {
        "completed",
        "interrupted",
        "error",
    }

    if status not in valid_statuses:
        raise ValueError("无效的 Span 状态")

    conn = sqlite3.connect(
        str(AUDIT_DB_PATH),
        timeout=10
    )

    try:
        cursor = conn.execute(
            """
            UPDATE node_trace
            SET
                finished_at = ?,
                duration_ms = ?,
                status = ?,
                error_type = ?
            WHERE span_id = ?
              AND status = 'running'
            """,
            (
                utc_now(),
                round(duration_ms, 2),
                status,
                error_type,
                span_id,
            )
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Span 不存在或已经结束"
            )

        conn.commit()

    finally:
        conn.close()


# =====================================
# 4. 创建可复用的节点追踪包装器
# =====================================

def trace_node(
    node_name: str,
    node_func: Callable[..., Any],
    *,
    accepts_config: bool = False,
):
    """
    为现有 LangGraph Node 添加执行追踪。

    这里不改变原 Node 的业务逻辑，
    而是在它的前后增加追踪功能。

    accepts_config：
        True：
            原节点接收 (state, config)。

        False：
            原节点只接收 state。

    重要：
        使用 RunnableConfig 参数，让 LangGraph
        自动注入当前线程的运行配置。
    """

    def traced_func(
        state,
        config: RunnableConfig,
    ):
        span_id = start_span(
            config,
            node_name
        )

        start_time = time.perf_counter()

        print(
            f"[Trace] {node_name} started"
        )

        try:
            if accepts_config:
                result = node_func(state, config)
            else:
                result = node_func(state)

        except GraphInterrupt:
            # interrupt() 是正常的工作流暂停，
            # 不能把它当成普通执行错误。
            duration_ms = (
                time.perf_counter() - start_time
            ) * 1000

            finish_span(
                span_id,
                "interrupted",
                duration_ms,
            )

            print(
                f"[Trace] {node_name} interrupted"
            )

            # 重新抛出，让 LangGraph 正常暂停。
            raise

        except Exception as exc:
            duration_ms = (
                time.perf_counter() - start_time
            ) * 1000

            finish_span(
                span_id,
                "error",
                duration_ms,
                type(exc).__name__,
            )

            print(
                f"[Trace] {node_name} error: "
                f"{type(exc).__name__}"
            )

            # 保持原有异常行为。
            raise

        else:
            duration_ms = (
                time.perf_counter() - start_time
            ) * 1000

            finish_span(
                span_id,
                "completed",
                duration_ms,
            )

            print(
                f"[Trace] {node_name} completed "
                f"({duration_ms:.2f} ms)"
            )

            return result

    return traced_func


# =====================================
# 5. 查询当前会话的执行轨迹
# =====================================

def list_node_traces(
    thread_id: str,
    limit: int = 100,
) -> list[dict]:
    """
    查询指定线程最近的节点轨迹。

    返回结果按照实际写入顺序排列。
    """

    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id 不能为空")

    if not 1 <= limit <= 500:
        raise ValueError("limit 必须在 1 到 500 之间")

    if not AUDIT_DB_PATH.is_file():
        return []

    conn = sqlite3.connect(
        str(AUDIT_DB_PATH),
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    try:
        # 兼容尚未创建追踪表的数据库。
        exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'node_trace'
            """
        ).fetchone()

        if not exists:
            return []

        rows = conn.execute(
            """
            SELECT
                span_id,
                thread_id,
                node_name,
                started_at,
                finished_at,
                duration_ms,
                status,
                error_type
            FROM node_trace
            WHERE thread_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_id, limit)
        ).fetchall()

        return [
            dict(row)
            for row in reversed(rows)
        ]

    finally:
        conn.close()
