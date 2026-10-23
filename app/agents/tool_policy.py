
"""
文件：app/agents/tool_policy.py

用途：
    集中管理 Sweet Research Agent 的工具执行策略。

主要功能：
    1. 定义工具的审批级别。
    2. 根据工具名称查询策略。
    3. 对 DeepSeek 生成的多个 tool_calls 进行分类。

策略：
    auto：
        无需人工审批，但执行时仍需参数校验。

    approve：
        必须经过统一 Approval Node 批准。

    deny：
        禁止执行，即使用户普通审批同意也不能放行。
  
安全原则：  
    未注册策略的工具一律 deny。  
"""  
  
from typing import Literal


# =====================================
# 1. 工具策略定义
# =====================================

ToolPolicy = Literal[
    "auto",
    "approve",
    "deny"
]


# 工具名必须和 @tool 注册后的名称一致。
TOOL_POLICIES: dict[str, ToolPolicy] = {

    # 只读知识检索工具
    "search_knowledge_tool": "auto",

    # 模拟写入工具，必须人工审批
    "simulate_save_note": "approve",
    "analyze_document_collection": "approve",
}


# =====================================
# 2. 查询单个工具的策略
# =====================================

def get_tool_policy(tool_name: str) -> ToolPolicy:
    """
    返回指定工具的执行策略。

    Args:
        tool_name: 工具注册名称。

    Returns:
        auto、approve 或 deny。

    未知工具默认 deny，避免新工具意外获得执行权限。
    """

    return TOOL_POLICIES.get(
        tool_name,
        "deny"
    )


# =====================================
# 3. 对多个 Tool Call 分类
# =====================================

def classify_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """
    对模型生成的 tool_calls 进行分类。

    输入：
        LangChain AIMessage.tool_calls 中的工具调用列表。

    返回：
        每个工具调用的 ID、名称、参数和审批策略。

    这里只做策略判断，不执行任何工具。
    """

    classified = []
    seen_ids = set()

    for call in tool_calls:

        call_id = call.get("id")
        tool_name = call.get("name")
        arguments = call.get("args")

        # 每个工具调用必须具有有效 ID，
        # 用于后续审批结果与 ToolMessage 的准确匹配。
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("工具调用缺少有效 ID")

        if call_id in seen_ids:
            raise ValueError(
                f"工具调用 ID 重复：{call_id}"
            )

        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("工具名称无效")

        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是字典")

        seen_ids.add(call_id)

        classified.append({
            "id": call_id,
            "name": tool_name,
            "args": arguments,
            "policy": get_tool_policy(tool_name),
        })

    return classified
