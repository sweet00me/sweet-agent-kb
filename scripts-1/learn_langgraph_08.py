
import os
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.checkpoint.sqlite import SqliteSaver

from learn_langgraph_04 import builder


# =====================================
# 1. 读取现有 SQLite
# =====================================

STORAGE = Path("/home/sweet/Storage")

if not os.path.ismount(STORAGE):
    raise RuntimeError("Storage 未挂载")

DB_PATH = (
    STORAGE
    / "KnowledgeBase/sqlite/"
    "langgraph_checkpoints_demo.sqlite3"
)

if not DB_PATH.is_file():
    raise FileNotFoundError(DB_PATH)

config = {
    "configurable": {
        "thread_id": "research-sqlite-001"
    }
}


# =====================================
# 2. 按对话轮次选择上下文
# =====================================

def select_recent_turns(messages, turns=1):
    """
    根据 HumanMessage 划分对话轮次。

    保留最近 N 轮完整对话，
    不修改数据库中的原始消息。
    """

    if turns < 1:
        raise ValueError("turns 必须 >= 1")

    starts = [
        i for i, msg in enumerate(messages)
        if isinstance(msg, HumanMessage)
    ]

    if not starts:
        return []

    start = starts[-turns]

    return messages[start:]


# =====================================
# 3. 读取并比较
# =====================================

with SqliteSaver.from_conn_string(
    str(DB_PATH)
) as checkpointer:

    graph = builder.compile(
        checkpointer=checkpointer
    )

    snapshot = graph.get_state(config)

    all_messages = snapshot.values.get(
        "messages", []
    )

    print("========== 完整历史 ==========")
    print("消息数量：", len(all_messages))
    print(
        "估算 Token：",
        count_tokens_approximately(all_messages)
    )

    for turns in (2, 1):

        selected = select_recent_turns(
            all_messages,
            turns=turns
        )

        print(
            f"\n========== 最近 {turns} 轮 =========="
        )

        print("消息数量：", len(selected))

        print(
            "估算 Token：",
            count_tokens_approximately(selected)
        )

        for msg in selected:

            if isinstance(msg, HumanMessage):
                print("用户：", msg.content)
            else:
                print("消息类型：", type(msg).__name__)

    # 再次检查：数据库中的 State 没有变化
    after = graph.get_state(config)

    print("\n========== 数据完整性检查 ==========")
    print(
        "数据库原始消息数：",
        len(after.values["messages"])
    )

print("\n测试完成：未修改 SQLite 对话历史。")
