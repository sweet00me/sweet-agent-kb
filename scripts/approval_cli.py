
"""
文件：scripts/approval_cli.py

用途：
    Human-in-the-loop 审批工作流的命令行入口。

主要功能：
    start：启动任务并等待人工审批。
    status：检查 SQLite 中的任务状态。
    approve：人工批准，恢复 Graph。
    reject：人工拒绝，恢复 Graph。

运行示例：
    python -m scripts.approval_cli start --thread-id hitl-001
    python -m scripts.approval_cli status --thread-id hitl-001
    python -m scripts.approval_cli approve --thread-id hitl-001

安全说明：
    所有执行操作均为模拟，不修改真实科研数据。
"""

import os
import argparse
from pathlib import Path

from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver

from app.workflows.approval import build_approval_graph


# =====================================
# 1. SQLite 路径
# =====================================

STORAGE_ROOT = Path("/home/sweet/Storage")

DB_PATH = (
    STORAGE_ROOT
    / "KnowledgeBase"
    / "sqlite"
    / "langgraph_hitl_demo.sqlite3"
)


# =====================================
# 2. 解析命令行参数
# =====================================

def parse_args():

    parser = argparse.ArgumentParser(
        description="LangGraph 人工审批实验"
    )

    parser.add_argument(
        "mode",
        choices=["start", "status", "approve", "reject"]
    )

    parser.add_argument(
        "--thread-id",
        required=True,
        help="指定审批任务所属的线程"
    )

    parser.add_argument(
        "--request",
        default="模拟将论文加入科研知识库",
        help="本次模拟操作的说明"
    )

    return parser.parse_args()


# =====================================
# 3. 查看保存的任务状态
# =====================================

def show_status(graph, config):
    """
    从 SQLite 加载最新 StateSnapshot。

    snapshot.values：
        已经保存的状态。

    snapshot.next：
        等待执行的节点。

    snapshot.interrupts：
        当前尚未得到回复的中断请求。
    """

    snapshot = graph.get_state(config)

    state = snapshot.values

    if not state:
        print("没有找到该线程的任务记录")
        return

    print("\n========== 任务状态 ==========")
    print("请求：", state.get("request"))
    print("方案：", state.get("plan"))
    print("下一节点：", snapshot.next)

    if snapshot.interrupts:

        print("\n任务状态：等待人工审批")

        for item in snapshot.interrupts:
            print("审批信息：", item.value)

    elif snapshot.next:

        print("\n任务状态：尚有待执行节点")

    else:

        print("\n任务状态：执行已结束")
        print("批准结果：", state.get("approved"))
        print("执行结果：", state.get("result"))


# =====================================
# 4. 主程序
# =====================================

def main():

    args = parse_args()

    thread_id = args.thread_id.strip()

    if not thread_id:
        raise ValueError("thread_id 不能为空")

    if not os.path.ismount(STORAGE_ROOT):
        raise RuntimeError("Storage 未正确挂载")

    os.umask(0o077)

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    config = {
        "configurable": {
            "thread_id": thread_id
        }
    }

    print("Thread ID：", thread_id)
    print("SQLite：", DB_PATH)

    # 每次运行程序都会重新打开 SQLite，
    # Graph 根据 thread_id 恢复原有状态。
    with SqliteSaver.from_conn_string(
        str(DB_PATH)
    ) as saver:

        graph = build_approval_graph(
            checkpointer=saver
        )

        snapshot = graph.get_state(config)

        # =================================
        # 模式一：启动任务
        # =================================

        if args.mode == "start":

            # 避免重复使用已经存在的 thread_id，
            # 意外覆盖或继续旧任务。
            if snapshot.values or snapshot.next:
                raise SystemExit(
                    "当前 thread_id 已有记录，请更换编号"
                )

            print("\n========== 启动模拟任务 ==========")

            result = graph.invoke(
                {"request": args.request},
                config=config
            )

            # interrupt() 不会让 Graph 直接完成。
            # 默认 invoke() 返回的 __interrupt__
            # 中包含需要展示给用户的审批请求。
            if "__interrupt__" in result:

                print("\nGraph 已暂停，等待人工审批。")

                for item in result["__interrupt__"]:
                    print("审批信息：", item.value)

            else:
                print("Graph 返回：", result)

        # =================================
        # 模式二：检查任务
        # =================================

        elif args.mode == "status":

            show_status(graph, config)

        # =================================
        # 模式三 / 四：批准或者拒绝
        # =================================

        else:

            # 没有等待审批的 interrupt，
            # 就不能随意提交 Command(resume=...)。
            if not snapshot.interrupts:
                raise SystemExit(
                    "当前任务没有待处理的人工审批"
                )

            approved = (args.mode == "approve")

            print("\n========== 恢复 Graph ==========")
            print(
                "人工决定：",
                "同意" if approved else "拒绝"
            )

            # 关键：
            # 恢复同一个 thread_id 的中断任务。
            result = graph.invoke(
                Command(resume=approved),
                config=config
            )

            print("\n========== 执行结果 ==========")
            print(result.get("result", "未生成结果"))

        # 查看这次运行后的最新状态。
        show_status(graph, config)


if __name__ == "__main__":
    main()
