
"""
文件：app/observability/audit.py

用途：
    为 Sweet Research Agent 提供持久化工具审计日志。

主要功能：
    1. 使用独立 SQLite 数据库存储审计事件。
    2. 记录工具审批决定和执行结果。
    3. 将事件关联到 thread_id 和 tool_call_id。
    4. 利用唯一约束避免重复写入相同事件。
    5. 提供按会话查询审计记录的方法。

隐私说明：
    不保存用户问题、工具参数正文或模型回复。
    只保存工具名称、参数哈希及操作状态。

注意：
    审计数据库与 LangGraph Checkpointer 相互独立，
    当前尚不具备跨数据库事务的一致性保证。
"""

import os
import sqlite3

from datetime import datetime, timezone
from pathlib import Path

from langchain_core.runnables import RunnableConfig


STORAGE_ROOT = Path("/home/sweet/Storage")

AUDIT_DB_PATH = (
    STORAGE_ROOT
    / "KnowledgeBase"
    / "sqlite"
    / "agent_audit.sqlite3"
)


# =====================================
# 1. 创建数据库表
# =====================================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tool_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    created_at TEXT NOT NULL,

    thread_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,

    tool_name TEXT NOT NULL,
    args_sha256 TEXT NOT NULL,

    policy TEXT NOT NULL,
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL,

    UNIQUE (
        thread_id,
        tool_call_id,
        event_type
    )
)
"""


# =====================================
# 2. 获取线程 ID
# =====================================

def get_thread_id(config: RunnableConfig) -> str:
    """
    从 LangGraph 的运行配置中获取 thread_id。

    不允许缺少 thread_id 的执行写入审计日志，
    否则后面无法对应到具体会话。
    """

    thread_id = config.get(
        "configurable", {}
    ).get("thread_id")

    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Audit 缺少有效的 thread_id")

    return thread_id


# =====================================
# 3. 数据库存储检查
# =====================================

def prepare_database():
    """
    检查 Storage 挂载，并确保数据库文件可访问。

    以 0600 创建数据库文件，防止普通用户读取。
    """

    if not os.path.ismount(STORAGE_ROOT):
        raise RuntimeError(
            "Storage 未正确挂载，拒绝写入审计日志"
        )

    AUDIT_DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # 显式限制新建文件权限，不依赖用户默认 umask。
    fd = os.open(
        AUDIT_DB_PATH,
        os.O_CREAT | os.O_RDWR,
        0o600
    )
    os.close(fd)

    os.chmod(AUDIT_DB_PATH, 0o600)


# =====================================
# 4. 写入审计事件
# =====================================

def write_audit_events(
    config: RunnableConfig,
    events: list[dict]
):
    """
    批量保存审批或执行事件。

    唯一键：
        thread_id + tool_call_id + event_type

    相同事件重试时不重复写入。
    若相同唯一键对应不同内容，则抛出异常，
    避免静默覆盖已有审计结果。
    """

    if not events:
        return

    thread_id = get_thread_id(config)

    # 先校验所有事件，再开始数据库事务。
    allowed_outcomes = {
        "decision": {
            "auto", "approve", "reject", "deny"
        },
        "execution": {
            "completed", "tool_error", "blocked"
        },
    }

    required_fields = {
        "tool_call_id",
        "tool_name",
        "args_sha256",
        "policy",
        "event_type",
        "outcome",
    }

    for event in events:

        if not required_fields.issubset(event):
            raise ValueError("审计事件缺少必要字段")

        event_type = event["event_type"]
        outcome = event["outcome"]

        if (
            event_type not in allowed_outcomes
            or outcome not in allowed_outcomes[event_type]
        ):
            raise ValueError("审计事件类型或结果无效")

    prepare_database()

    conn = sqlite3.connect(
        str(AUDIT_DB_PATH),
        timeout=10
    )

    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()

        # 先获得写锁，再检查重复事件。
        # 避免并发写入时出现简单的检查-插入竞态。
        conn.execute("BEGIN IMMEDIATE")

        for event in events:

            event_key = (
                thread_id,
                event["tool_call_id"],
                event["event_type"],
            )

            existing = conn.execute(
                """
                SELECT
                    tool_name,
                    args_sha256,
                    policy,
                    outcome
                FROM tool_audit
                WHERE thread_id = ?
                  AND tool_call_id = ?
                  AND event_type = ?
                """,
                event_key
            ).fetchone()

            expected = (
                event["tool_name"],
                event["args_sha256"],
                event["policy"],
                event["outcome"],
            )

            if existing is not None:

                if tuple(existing) != expected:
                    raise RuntimeError(
                        "发现冲突的审计事件："
                        f"{event_key}"
                    )

                # 内容完全相同的重试事件直接跳过。
                continue

            created_at = datetime.now(
                timezone.utc
            ).isoformat(timespec="seconds")

            conn.execute(
                """
                INSERT INTO tool_audit (
                    created_at,
                    thread_id,
                    tool_call_id,
                    tool_name,
                    args_sha256,
                    policy,
                    event_type,
                    outcome
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    thread_id,
                    event["tool_call_id"],
                    event["tool_name"],
                    event["args_sha256"],
                    event["policy"],
                    event["event_type"],
                    event["outcome"],
                )
            )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# 5. 查询指定会话的审计事件
# =====================================

def list_audit_events(thread_id: str) -> list[dict]:
    """
    按 thread_id 查询审计记录。

    返回事件字典列表，按照写入顺序排序。
    """

    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id 不能为空")

    if not AUDIT_DB_PATH.is_file():
        return []

    conn = sqlite3.connect(
        str(AUDIT_DB_PATH),
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            """
            SELECT
                created_at,
                thread_id,
                tool_call_id,
                tool_name,
                args_sha256,
                policy,
                event_type,
                outcome
            FROM tool_audit
            WHERE thread_id = ?
            ORDER BY id ASC
            """,
            (thread_id,)
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        conn.close()
