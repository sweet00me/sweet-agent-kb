
import os
import argparse
from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver

# 复用此前已经定义好的 Graph
from learn_langgraph_04 import builder


# =====================================
# 1. SQLite 数据库存储路径
# =====================================

STORAGE_ROOT = Path("/home/sweet/Storage")

DB_DIR = STORAGE_ROOT / "KnowledgeBase/sqlite"

DB_PATH = DB_DIR / "langgraph_checkpoints_demo.sqlite3"

if not os.path.ismount(STORAGE_ROOT):
    raise RuntimeError("Storage 未正确挂载")

DB_DIR.mkdir(parents=True, exist_ok=True)

# 限制本进程新建文件的默认权限
os.umask(0o077)


# =====================================
# 2. 命令行参数
# =====================================

parser = argparse.ArgumentParser()

parser.add_argument(
    "mode",
    choices=["start", "resume", "inspect"],
    help="开始对话、继续对话或检查状态"
)

parser.add_argument(
    "--thread-id",
    default="research-sqlite-001",
    help="对话线程编号"
)

args = parser.parse_args()

config = {
    "configurable": {
        "thread_id": args.thread_id
    },
    "recursion_limit": 10 #最多只执行10个节点
}


# =====================================
# 3. SQLite Checkpointer
# =====================================

with SqliteSaver.from_conn_string(
    str(DB_PATH)
) as checkpointer:

    # 重新编译原来的 Graph
    graph = builder.compile(
        checkpointer=checkpointer
    )

    # 读取已有状态
    snapshot = graph.get_state(config)

    old_messages = snapshot.values.get(
        "messages", []
    )

    print("========== SQLite Checkpointer ==========")
    print("数据库：", DB_PATH)
    print("Thread ID：", args.thread_id)
    print("已有消息数量：", len(old_messages))

    # =================================
    # 4. 仅检查历史状态
    # =================================

    if args.mode == "inspect":

        print("\n========== 已保存的对话 ==========")

        for message in old_messages:

            if isinstance(message, HumanMessage):
                print("用户：", message.content)

        print("\n工具消息数量：", sum(
            isinstance(m, ToolMessage)
            for m in old_messages
        ))

    else:

        # =============================
        # 5. 首轮对话
        # =============================

        if args.mode == "start":

            if old_messages:
                raise SystemExit(
                    "当前 thread_id 已有历史记录，"
                    "请使用 resume 或更换 thread_id"
                )

            question = (
                "根据我的知识库，RAG 与 Agent "
                "有什么关系？请引用来源。"
            )

        # =============================
        # 6. 恢复历史并继续对话
        # =============================

        else:

            if not old_messages:
                raise SystemExit(
                    "没有找到历史对话，"
                    "请先执行 start"
                )

            question = (
                "那它们分别是什么？"
                "请继续依据之前检索的资料回答。"
            )

        print("\n========== 本轮用户问题 ==========")
        print(question)

        # 只提交新消息，旧消息由 SQLite 恢复
        result = graph.invoke(
            {
                "messages": [
                    HumanMessage(content=question)
                ]
            },
            config=config
        )

        print("\n========== Agent 回答 ==========")
        print(result["messages"][-1].content)

        print("\n========== 当前状态 ==========")

        messages = result["messages"]

        print("消息总数：", len(messages))

        print("用户消息数量：", sum(
            isinstance(m, HumanMessage)
            for m in messages
        ))

        print("工具消息数量：", sum(
            isinstance(m, ToolMessage)
            for m in messages
        ))

print("\n程序已退出 SQLite 上下文，数据库连接关闭。")
