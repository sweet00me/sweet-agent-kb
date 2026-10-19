
"""
文件：scripts/chat.py

用途：
    Sweet Research Agent 的正式命令行入口。

主要功能：
    1. 与 DeepSeek Agent 连续对话。
    2. 自动检索私人知识库。
    3. 使用 SQLite 保存并恢复会话。
    4. 可选启用对话摘要。
    5. 查看和处理 Human-in-the-loop 工具审批。

运行：
    python -m scripts.chat --thread-id research-main

    开启摘要：
    python -m scripts.chat --thread-id research-main --enable-summary

交互命令：
    /help       查看帮助
    /thread     查看当前会话
    /pending    查看待审批工具
    /review     逐项审批并恢复任务
    /new        新建会话
    /exit       退出程序

安全说明：
    目前有写入意图的工具仅支持模拟操作。
    不会实际保存、修改或删除科研文件。
"""

import os
import json
import uuid
import argparse

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langgraph.types import Command

from app.agents.graph import build_graph
from app.memory.checkpoint import (
    open_checkpointer,
    DB_PATH,
)


# =====================================
# 1. 命令行参数
# =====================================

def parse_args():
    """解析会话 ID 与摘要设置。"""

    parser = argparse.ArgumentParser(
        description="Sweet Research Agent"
    )

    parser.add_argument(
        "--thread-id",
        default="research-main",
        help="需要使用或恢复的会话 ID"
    )

    parser.add_argument(
        "--enable-summary",
        action="store_true",
        help="开启自动对话摘要"
    )

    return parser.parse_args()


# =====================================
# 2. 统一 Graph 配置
# =====================================

def make_config(thread_id: str):
    """
    构建当前线程的 LangGraph 配置。

    同一个 thread_id 对应同一份 SQLite 会话状态。
    """

    return {
        "configurable": {
            "thread_id": thread_id
        },
        "recursion_limit": 12
    }


# =====================================
# 3. 读取当前线程
# =====================================

def get_snapshot(graph, thread_id: str):
    """读取 SQLite 中当前线程的最新 StateSnapshot。"""

    return graph.get_state(
        make_config(thread_id)
    )


# =====================================
# 4. 展示待审批请求
# =====================================

def show_pending(graph, thread_id: str):
    """
    展示当前线程等待审批的工具调用。

    注意：
        审批信息来自 SQLite 中的中断快照，
        而不是让 LLM 重新生成工具调用。
    """

    snapshot = get_snapshot(graph, thread_id)

    if not snapshot.interrupts:
        print("\n当前没有待处理的人工审批。")
        return False

    print("\n========== 待审批工具 ==========")
    print("Thread ID：", thread_id)

    for interrupted in snapshot.interrupts:

        payload = interrupted.value

        if not isinstance(payload, dict):
            print("未知中断内容：", payload)
            continue

        print("审批类型：", payload.get("type"))
        print("说明：", payload.get("message"))

        for call in payload.get("calls", []):

            print("\n--------------------------------")
            print("Tool Call ID：", call["id"])
            print("工具名称：", call["name"])
            print("工具参数：")

            print(json.dumps(
                call["args"],
                ensure_ascii=False,
                indent=2
            ))

            print(
                "参数指纹：",
                call["fingerprint"][:16] + "..."
            )

    print("\n使用 /review 逐项审批。")
    print("使用 /exit 可暂时退出，不会自动批准。")

    return True


# =====================================
# 5. 统一处理 Graph 执行结果
# =====================================

def show_run_result(graph, thread_id: str, result):
    """
    判断 Graph 是正常结束，还是暂停等待审批。

    不能将带 tool_calls 的 AIMessage
    误认为已经完成的最终回答。
    """

    # SQLite 中的最新 State 比简单检查返回文本
    # 更适合判断当前是否正在等待人工审批。
    snapshot = get_snapshot(graph, thread_id)

    if snapshot.interrupts:
        print("\n[HITL] Agent 已暂停，等待人工审批。")
        show_pending(graph, thread_id)
        return

    if snapshot.next:
        print(
            "\n[状态] 当前 Graph 尚未完成，"
            "需要检查待执行节点：",
            snapshot.next
        )
        return

    messages = result.get("messages", [])

    if not messages:
        print("\n[状态] 没有生成新的回答。")
        return

    last = messages[-1]

    if not isinstance(last, AIMessage) or last.tool_calls:
        print("\n[状态] 没有获得最终 AI 回答。")
        return

    print("\nAgent：")
    print(last.content)

    tool_results = sum(
        isinstance(message, ToolMessage)
        for message in messages
    )

    print(
        f"\n[状态] 活动消息：{len(messages)}"
        f" | 活动工具结果：{tool_results}"
        f" | 摘要：{'有' if result.get('summary') else '无'}"
    )


# =====================================
# 6. 提交一轮用户问题
# =====================================

def ask(graph, thread_id: str, question: str):
    """
    执行一次用户提问。

    如果线程正在等待审批，
    不允许直接输入新问题绕过恢复流程。
    """

    snapshot = get_snapshot(graph, thread_id)

    if snapshot.interrupts:
        print(
            "\n[HITL] 当前会话有待审批操作。"
            "请先使用 /pending 和 /review。"
        )
        return

    if snapshot.next:
        print(
            "\n[状态] 当前工作流尚未完成，"
            "暂不接受新的用户问题。"
        )
        return

    print("\nAgent 正在处理...")

    result = graph.invoke(
        {
            "messages": [
                HumanMessage(content=question)
            ]
        },
        config=make_config(thread_id)
    )

    show_run_result(graph, thread_id, result)


# =====================================
# 7. 收集人工审批决定
# =====================================

def review_pending(graph, thread_id: str):
    """
    从持久化中断请求中读取工具参数，
    让用户逐个批准或拒绝。

    重要：
        所有决定收集完成后才执行一次
        Command(resume=...)。

        中途退出不会提交部分批准结果。
    """

    snapshot = get_snapshot(graph, thread_id)

    if not snapshot.interrupts:
        print("\n当前没有待审批任务。")
        return

    # 当前版本一次只处理一个 Approval Node 中断。
    # 该中断内部可以包含多个 tool_calls。
    if len(snapshot.interrupts) != 1:
        print(
            "[错误] 存在多个并行中断，"
            "当前 CLI 暂不支持直接批量恢复。"
        )
        return

    payload = snapshot.interrupts[0].value

    if (
        not isinstance(payload, dict)
        or payload.get("type") != "tool_approval"
    ):
        print("[错误] 未识别的审批请求类型。")
        return

    calls = payload.get("calls", [])

    if not isinstance(calls, list) or not calls:
        print("[错误] 审批请求中没有工具调用。")
        return

    decisions = {}

    print("\n========== 人工工具审批 ==========")
    print("每个调用必须明确选择批准或拒绝。")
    print("输入 q 可以取消本次审批，任务保持暂停。")

    for call in calls:

        print("\n--------------------------------")
        print("Tool Call ID：", call["id"])
        print("工具名称：", call["name"])
        print("完整参数：")

        print(json.dumps(
            call["args"],
            ensure_ascii=False,
            indent=2
        ))

        while True:

            answer = input(
                "是否批准这个工具调用？[y/N/q]："
            ).strip().lower()

            if answer in ("q", "quit"):
                print(
                    "已退出审批界面，"
                    "原任务继续保持暂停。"
                )
                return

            if answer in ("y", "yes"):
                decisions[call["id"]] = "approve"
                break

            if answer in ("", "n", "no"):
                decisions[call["id"]] = "reject"
                break

            print("请输入 y、n 或 q。")

    # ---------------------------------
    # 8. 恢复被中断的工作流
    # ---------------------------------

    print("\n========== 审批决定 ==========")

    print(json.dumps(
        decisions,
        ensure_ascii=False,
        indent=2
    ))

    print("\n正在恢复 Agent...")

    # 这里必须使用同一个 thread_id。
    # 不重新提交 HumanMessage，也不重新生成工具调用。
    result = graph.invoke(
        Command(
            resume={
                "decisions": decisions
            }
        ),
        config=make_config(thread_id)
    )

    show_run_result(graph, thread_id, result)


# =====================================
# 9. 查看当前会话信息
# =====================================

def show_thread_info(graph, thread_id: str):
    """展示活动消息、摘要及中断状态。"""

    snapshot = get_snapshot(graph, thread_id)

    state = snapshot.values

    messages = state.get("messages", [])
    summary = state.get("summary", "")

    turns = sum(
        isinstance(m, HumanMessage)
        for m in messages
    )

    print("\n========== 会话信息 ==========")
    print("Thread ID：", thread_id)
    print("活动消息数：", len(messages))
    print("活动用户轮次：", turns)
    print("历史摘要：", "有" if summary else "无")
    print(
        "等待审批：",
        "是" if snapshot.interrupts else "否"
    )
    print("下一节点：", snapshot.next)
    print("==============================\n")


# =====================================
# 10. 帮助菜单
# =====================================

def show_help():
    """打印可用命令。"""

    print("""
========== Sweet Research Agent ==========

/help       查看帮助
/thread     查看当前线程信息
/pending    查看待审批工具调用
/review     逐项批准或拒绝
/new        创建新会话
/exit       退出程序

直接输入问题即可开始聊天。

说明：
    模拟写入工具不会修改真实文件。

==========================================
""")


# =====================================
# 11. 程序主入口
# =====================================

def main():

    args = parse_args()

    thread_id = args.thread_id.strip()

    if not thread_id:
        raise ValueError("thread_id 不能为空")

    # SQLite 可能保存私人科研对话。
    os.umask(0o077)

    print("=" * 55)
    print("        Sweet Research Agent")
    print("=" * 55)

    print("当前会话：", thread_id)
    print("SQLite：", DB_PATH)
    print(
        "自动摘要：",
        "已开启" if args.enable_summary else "已关闭"
    )

    print("输入 /help 查看命令")
    print("输入 /exit 退出程序")

    # 数据库在整个 CLI 生命周期中保持打开。
    with open_checkpointer() as saver:

        graph = build_graph(
            checkpointer=saver,
            enable_summarization=args.enable_summary
        )

        show_thread_info(graph, thread_id)

        # ---------------------------------
        # 12. CLI 交互循环
        # ---------------------------------

        while True:

            try:
                question = input("你：").strip()

            except (KeyboardInterrupt, EOFError):
                print("\n正在退出...")
                break

            if not question:
                continue

            if question in ("/exit", "/quit"):
                print("正在退出...")
                break

            if question == "/help":
                show_help()
                continue

            if question == "/thread":
                show_thread_info(graph, thread_id)
                continue

            if question == "/pending":
                show_pending(graph, thread_id)
                continue

            if question == "/new":

                old_thread_id = thread_id

                thread_id = (
                    "research-"
                    + uuid.uuid4().hex[:12]
                )

                print("\n已创建新会话。")
                print("旧会话：", old_thread_id)
                print("新会话：", thread_id)

                continue

            if question == "/review":

                try:
                    review_pending(graph, thread_id)

                except (KeyboardInterrupt, EOFError):
                    print(
                        "\n审批输入已中断。"
                        "未提交审批决定。"
                    )

                except Exception as exc:
                    print(
                        "\n[审批错误]",
                        type(exc).__name__,
                        str(exc)
                    )

                continue

            if question.startswith("/"):
                print("未知命令，请输入 /help。")
                continue

            # ---------------------------------
            # 13. 正常用户问答
            # ---------------------------------

            try:
                ask(
                    graph,
                    thread_id,
                    question
                )

            except Exception as exc:
                print(
                    "\n[Agent 错误]",
                    type(exc).__name__,
                    str(exc)
                )

                print(
                    "请使用 /thread 检查任务状态。"
                )

    print("\nSweet Research Agent 已退出。")


if __name__ == "__main__":
    main()
