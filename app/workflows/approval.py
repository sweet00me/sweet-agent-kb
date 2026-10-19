
"""
文件：app/workflows/approval.py

用途：
    定义科研 Agent 的人工审批工作流。

主要功能：
    1. prepare_node：生成待审批的操作方案。
    2. approval_node：通过 interrupt() 暂停工作流。
    3. execute_node：人工批准后模拟执行操作。
    4. cancel_node：人工拒绝后取消操作。

安全说明：
    本模块只进行模拟操作。
    不会删除文件、修改 ChromaDB 或执行系统命令。
"""

from typing import TypedDict, NotRequired

from langgraph.graph import (
    StateGraph,
    START,
    END,
)
from langgraph.types import interrupt


# =====================================
# 1. 定义审批工作流 State
# =====================================

class ApprovalState(TypedDict):
    request: str
    plan: NotRequired[str]
    approved: NotRequired[bool]
    result: NotRequired[str]


# =====================================
# 2. 生成待审批方案
# =====================================

def prepare_node(state: ApprovalState):
    """
    根据用户请求生成演示方案。

    当前使用固定逻辑，不调用大模型。
    以后可以替换为 DeepSeek 生成的操作计划。
    """

    print("[Node] prepare_node")

    request = state["request"].strip()

    if not request:
        raise ValueError("操作请求不能为空")

    plan = (
        f"模拟操作：{request}\n"
        "说明：仅打印模拟结果，不修改任何真实文件。"
    )

    return {"plan": plan}


# =====================================
# 3. 等待人工审批
# =====================================

def approval_node(state: ApprovalState):
    """
    请求人工审批。

    第一次运行：
        interrupt() 暂停 Graph。

    恢复执行：
        节点从头重新运行。
        interrupt() 返回 Command(resume=...) 提供的值。
    """

    print("[Node] approval_node：进入审批节点")

    # 传递给用户的审批信息必须能够被序列化。
    # 此处不会执行实际操作。
    approved = interrupt({
        "type": "approval",
        "request": state["request"],
        "plan": state["plan"],
        "message": "是否批准上述模拟操作？"
    })

    # 不能盲目信任外部传入的恢复值。
    # 这里要求它必须是 Python bool。
    if type(approved) is not bool:
        raise ValueError("审批结果必须为布尔值")

    print("[Node] 收到审批结果：", approved)

    return {"approved": approved}


# =====================================
# 4. 根据审批结果选择路径
# =====================================

def route_after_approval(state: ApprovalState):
    """同意进入执行节点，否则进入取消节点。"""

    if state["approved"] is True:
        return "execute"

    return "cancel"


# =====================================
# 5. 批准后模拟执行
# =====================================

def execute_node(state: ApprovalState):
    """
    模拟执行已经批准的操作。

    不执行真实文件操作。
    """

    print("[Node] execute_node：模拟执行")

    return {
        "result": (
            "【模拟执行成功】\n"
            f"{state['request']}\n"
            "未修改真实文件或数据库。"
        )
    }


# =====================================
# 6. 拒绝后取消
# =====================================

def cancel_node(state: ApprovalState):
    """用户拒绝审批时，终止操作。"""

    print("[Node] cancel_node：已取消")

    return {
        "result": (
            "【用户已拒绝】\n"
            f"操作已取消：{state['request']}"
        )
    }


# =====================================
# 7. 构建审批 Graph
# =====================================

def build_approval_graph(checkpointer):
    """
    构建可暂停和恢复的审批工作流。

    Args:
        checkpointer:
            持久化状态存储，本课使用 SqliteSaver。

    Returns:
        编译完成的 LangGraph。
    """

    builder = StateGraph(ApprovalState)

    builder.add_node("prepare", prepare_node)
    builder.add_node("approval", approval_node)
    builder.add_node("execute", execute_node)
    builder.add_node("cancel", cancel_node)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approval")

    # 必须在得到人工审批结果后，
    # 才决定进入执行或取消路径。
    builder.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "execute": "execute",
            "cancel": "cancel"
        }
    )

    builder.add_edge("execute", END)
    builder.add_edge("cancel", END)

    return builder.compile(
        checkpointer=checkpointer
    )
