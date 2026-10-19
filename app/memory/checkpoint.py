
"""
文件：app/memory/checkpoint.py

用途：
    为科研 Agent 提供 SQLite 持久化 Checkpointer。

主要功能：
    1. 检查 Storage 磁盘是否正确挂载。
    2. 创建并打开 SQLite Checkpointer。
    3. 在使用结束后自动关闭数据库连接。

注意：
    本模块保存的是 Agent 的对话 State，
    不是 ChromaDB 向量知识库，也不是实验结果数据库。
"""

import os
from pathlib import Path
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver


# =====================================
# 1. 数据库存储配置
# =====================================

STORAGE_ROOT = Path("/home/sweet/Storage")

DB_PATH = (
    STORAGE_ROOT
    / "KnowledgeBase"
    / "sqlite"
    / "langgraph_agent.sqlite3"
)


# =====================================
# 2. SQLite Checkpointer 管理
# =====================================

@contextmanager
def open_checkpointer():
    """
    打开 SQLite Checkpointer，并管理其生命周期。

    使用方法：
        with open_checkpointer() as saver:
            graph = build_graph(checkpointer=saver)
            graph.invoke(...)

    退出 with 后自动关闭数据库连接。
    """

    # 防止 Storage 意外未挂载时，
    # 将数据库写入系统盘上的同名目录。
    if not os.path.ismount(STORAGE_ROOT):
        raise RuntimeError(
            f"Storage 未正确挂载：{STORAGE_ROOT}"
        )

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # Checkpointer 必须在整个 Agent 会话期间保持打开。
    # 不能在创建 Graph 后立即关闭数据库连接。
    with SqliteSaver.from_conn_string(
        str(DB_PATH)
    ) as saver:

        yield saver
