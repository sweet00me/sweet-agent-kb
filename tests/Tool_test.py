
import json

from app.agents.tool_policy import (
    get_tool_policy,
    classify_tool_calls,
)

from app.tools.simulated_write import simulate_save_note


print("========== 1. 单个工具策略 ==========")

for name in [
    "search_knowledge_tool",
    "simulate_save_note",
    "unknown_tool"
]:
    print(name, "→", get_tool_policy(name))


print("\n========== 2. 多工具调用分类 ==========")

tool_calls = [
    {
        "id": "call_001",
        "name": "search_knowledge_tool",
        "args": {
            "question": "RAG 与 Agent 的关系"
        }
    },
    {
        "id": "call_002",
        "name": "simulate_save_note",
        "args": {
            "title": "LangGraph 学习笔记",
            "content": "学习统一审批机制"
        }
    },
    {
        "id": "call_003",
        "name": "unknown_tool",
        "args": {}
    }
]

classified = classify_tool_calls(tool_calls)

for item in classified:
    print(
        item["id"],
        item["name"],
        "→",
        item["policy"]
    )

assert [x["policy"] for x in classified] == [
    "auto", "approve", "deny"
]


print("\n========== 3. 模拟工具参数校验 ==========")

result = simulate_save_note.invoke({
    "title": "LangGraph 学习笔记",
    "content": "学习 Tool Policy 与 Approval Node"
})

data = json.loads(result)

print(json.dumps(
    data,
    ensure_ascii=False,
    indent=2
))

assert data["status"] == "simulated"

print("\n全部测试通过")
