
import os
import json
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


# ========== 1. 基础配置 ==========

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_ROOT = Path("/home/sweet/Storage/KnowledgeBase")

load_dotenv(PROJECT_ROOT / ".env")

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com"
    ),
    timeout=60.0,
    max_retries=1
)

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")


# ========== 2. 定义本地工具 ==========

def list_knowledge_directories():
    """获取知识库一级目录。"""

    if not KNOWLEDGE_ROOT.is_dir():
        return {"error": "知识库目录不存在"}

    dirs = sorted(
        p.name for p in KNOWLEDGE_ROOT.iterdir()
        if p.is_dir()
    )

    return {
        "directories": dirs,
        "count": len(dirs)
    }


# ========== 3. 向模型注册工具 ==========

tools = [
    {
        "type": "function",
        "function": {
            "name": "list_knowledge_directories",
            "description": (
                "查询 sweetserver 本地知识库"
                "的真实一级目录。"
                "当用户询问知识库目录情况时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False
            }
        }
    }
]


# ========== 4. Agent 单轮执行函数 ==========

def ask_agent(question):

    print(f"\n用户问题：{question}")

    messages = [
        {
            "role": "system",
            "content": (
                "你是 sweetserver 科研助手。"
                "涉及服务器真实状态时，应调用工具。"
                "普通概念问题可以直接回答。"
                "不要编造服务器信息。"
            )
        },
        {
            "role": "user",
            "content": question
        }
    ]

    # 第一次 API 请求：模型自主决策
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        max_tokens=300,
        extra_body={"thinking": {"type": "disabled"}}
    )

    message = response.choices[0].message
    total_tokens = (
        response.usage.total_tokens
        if response.usage else 0
    )

    # 分支 A：模型决定不调用工具
    if not message.tool_calls:
        print("\n[决策] 不调用工具")
        print("[回答]", message.content)
        print(f"[Token] {total_tokens}")
        return

    # 分支 B：模型决定调用工具
    print("\n[决策] 调用工具")
    messages.append(message)

    for call in message.tool_calls:

        name = call.function.name
        print(f"[工具名称] {name}")

        try:
            arguments = json.loads(
                call.function.arguments or "{}"
            )

            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是对象")

            if name == "list_knowledge_directories" and not arguments:
                result = list_knowledge_directories()
            else:
                result = {"error": "未授权的工具或参数"}

        except (ValueError, TypeError) as exc:
            result = {"error": str(exc)}

        print("[工具结果]", result)

        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(
                result,
                ensure_ascii=False
            )
        })

    # 第二次 API 请求：总结工具结果
    final = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
        tool_choice="none",
        max_tokens=450,
        extra_body={"thinking": {"type": "disabled"}}
    )

    total_tokens += (
        final.usage.total_tokens
        if final.usage else 0
    )

    print("\n[最终回答]")
    print(final.choices[0].message.content)
    print(f"[Token] {total_tokens}")


# ========== 5. 运行两个对照实验 ==========

if __name__ == "__main__":

    print("========== 实验 A ==========")
    ask_agent(
        "请查看 sweetserver 知识库有哪些一级目录，"
        "以服务器实际数据为准。"
    )

    print("\n========== 实验 B ==========")
    ask_agent(
        "请用两句话解释什么是 RAG。"
    )
