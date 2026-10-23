
"""
文件：app/api/session_store.py

用途：
    为 Sweet Research Agent 提供独立的会话与聊天历史存储。

主要功能：
    1. 创建和查询聊天会话。
    2. 保存用户消息和最终助手回答。
    3. 获取按更新时间排序的会话列表。
    4. 分页查询历史消息。

设计说明：
    本数据库独立于 LangGraph Checkpointer。
    即使 Agent 压缩活动消息，已保存的聊天历史仍然保留。

    当前服务仅面向本机单用户开发。
    未来接入多用户认证时，需要加入 owner_id 和访问校验。
"""

import os
import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


STORAGE_ROOT = Path("/home/sweet/Storage")

SESSION_DB_PATH = (
    STORAGE_ROOT
    / "KnowledgeBase"
    / "sqlite"
    / "agent_sessions.sqlite3"
)


# =====================================
# 1. 数据库表结构
# =====================================

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('user', 'assistant')
    ),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,

    FOREIGN KEY (thread_id)
        REFERENCES chat_threads(thread_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_thread
ON chat_messages(thread_id, id);

CREATE TABLE IF NOT EXISTS chat_thread_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES chat_threads(thread_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('renamed', 'deleted')),
    previous_title TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    """生成 UTC 时间，便于客户端按时区展示。"""

    return datetime.now(
        timezone.utc
    ).isoformat(timespec="milliseconds")


def connect_db():
    """创建短生命周期 SQLite 连接。"""

    conn = sqlite3.connect(
        str(SESSION_DB_PATH),
        timeout=10,
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


# =====================================
# 2. 初始化数据库
# =====================================

def initialize_database():
    """验证挂载状态并创建数据表。"""

    if not os.path.ismount(STORAGE_ROOT):
        raise RuntimeError("Storage 未正确挂载")

    SESSION_DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 限制数据库文件的访问权限。
    fd = os.open(
        SESSION_DB_PATH,
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    os.close(fd)

    os.chmod(SESSION_DB_PATH, 0o600)

    with closing(connect_db()) as conn:
        conn.executescript(CREATE_TABLES_SQL)
        with conn:
            # 保留旧版数据库中的会话、消息及 Request Journal。
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(chat_threads)")
            }
            if "deleted_at" not in columns:
                conn.execute("ALTER TABLE chat_threads ADD COLUMN deleted_at TEXT")


# =====================================
# 3. 创建会话
# =====================================

def create_thread(
    thread_id: str,
    title: str = "新会话",
) -> dict:
    """创建一条新的会话元数据记录。"""

    now = utc_now()

    with closing(connect_db()) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO chat_threads (
                    thread_id, title, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, title, now, now)
            )

    return get_thread(thread_id)


def get_thread(thread_id: str) -> dict | None:
    """按 ID 查询未删除的会话。"""

    with closing(connect_db()) as conn:
        row = conn.execute(
            """
            SELECT thread_id, title, created_at, updated_at
            FROM chat_threads
            WHERE thread_id = ? AND deleted_at IS NULL
            """,
            (thread_id,)
        ).fetchone()

    return dict(row) if row else None


def is_thread_deleted(thread_id: str) -> bool:
    """区分已删除的会话与尚未注册的会话，防止聊天入口恢复旧记录。"""
    with closing(connect_db()) as conn:
        row = conn.execute(
            "SELECT deleted_at FROM chat_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return row is not None and row["deleted_at"] is not None


def ensure_thread(
    thread_id: str,
    title: str = "新会话",
) -> dict:
    """
    确保会话已注册。

    用于兼容 /chat 自动生成 thread_id 的行为。
    已存在的会话不会被覆盖标题。
    """

    now = utc_now()

    with closing(connect_db()) as conn:
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO chat_threads (
                    thread_id, title, created_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, title, now, now)
            )

    record = get_thread(thread_id)
    if record is None:
        raise ValueError("会话已删除，不能重新使用此 thread_id")
    return record


def rename_thread(thread_id: str, title: str) -> dict | None:
    """重命名会话，并在同一事务中保存本地变更记录。"""
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
        raise ValueError("会话标题必须为 1 到 80 个字符")
    title = title.strip()
    now = utc_now()
    with closing(connect_db()) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT thread_id, title, created_at, updated_at FROM chat_threads
                   WHERE thread_id = ? AND deleted_at IS NULL""",
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            if record["title"] != title:
                conn.execute(
                    "UPDATE chat_threads SET title = ?, updated_at = ? WHERE thread_id = ?",
                    (title, now, thread_id),
                )
                conn.execute(
                    """INSERT INTO chat_thread_events
                       (thread_id, event_type, previous_title, title, created_at)
                       VALUES (?, 'renamed', ?, ?, ?)""",
                    (thread_id, record["title"], title, now),
                )
                record.update(title=title, updated_at=now)
    return record


def delete_thread(thread_id: str) -> bool:
    """软删除会话；保留消息、Checkpoint、请求日志和本地变更记录。"""
    now = utc_now()
    with closing(connect_db()) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT title FROM chat_threads WHERE thread_id = ? AND deleted_at IS NULL",
                (thread_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE chat_threads SET deleted_at = ?, updated_at = ? WHERE thread_id = ?",
                (now, now, thread_id),
            )
            conn.execute(
                """INSERT INTO chat_thread_events
                   (thread_id, event_type, previous_title, title, created_at)
                   VALUES (?, 'deleted', ?, ?, ?)""",
                (thread_id, row["title"], row["title"], now),
            )
    return True


# =====================================
# 4. 保存用户或助手消息
# =====================================

def append_message(
    thread_id: str,
    role: str,
    content: str,
) -> int:
    """
    追加一条完整的聊天记录。

    role 只允许 user 或 assistant。

    消息写入与更新时间更新放在同一 SQLite 事务中。
    """

    if role not in ("user", "assistant"):
        raise ValueError("不支持的消息角色")

    if not isinstance(content, str) or not content.strip():
        raise ValueError("消息内容不能为空")

    now = utc_now()

    with closing(connect_db()) as conn:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_messages (
                    thread_id, role, content, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, role, content, now)
            )

            conn.execute(
                """
                UPDATE chat_threads
                SET updated_at = ?
                WHERE thread_id = ?
                """,
                (now, thread_id)
            )

            message_id = cursor.lastrowid

    return message_id


# =====================================
# 5. 查询会话列表
# =====================================

def list_threads(limit: int = 50) -> list[dict]:
    """按最近活动时间排序。"""

    with closing(connect_db()) as conn:
        rows = conn.execute(
            """
            SELECT
                t.thread_id,
                t.title,
                t.created_at,
                t.updated_at,
                (
                    SELECT COUNT(*)
                    FROM chat_messages m
                    WHERE m.thread_id = t.thread_id
                ) AS message_count
            FROM chat_threads t
            WHERE t.deleted_at IS NULL
            ORDER BY t.updated_at DESC, t.thread_id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

    return [dict(row) for row in rows]


# =====================================
# 6. 分页查询聊天历史
# =====================================

def list_messages(
    thread_id: str,
    limit: int = 100,
    before_id: int | None = None,
) -> dict:
    """
    查询最近的聊天记录。

    before_id：
        只读取 ID 小于指定值的较早消息。

    返回：
        items：按时间正序排列。
        next_before_id：下一页游标，None 表示没有更早消息。
    """

    with closing(connect_db()) as conn:
        rows = conn.execute(
            """
            SELECT id, thread_id, role, content, created_at
            FROM chat_messages
            WHERE thread_id = ?
              AND (? IS NULL OR id < ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (thread_id, before_id, before_id, limit + 1)
        ).fetchall()

    has_more = len(rows) > limit
    selected = rows[:limit]

    # SQL 使用降序取最近消息，
    # 返回给前端时恢复正常阅读顺序。
    items = [
        dict(row)
        for row in reversed(selected)
    ]

    next_before_id = (
        items[0]["id"]
        if has_more and items
        else None
    )

    return {
        "items": items,
        "next_before_id": next_before_id,
    }
