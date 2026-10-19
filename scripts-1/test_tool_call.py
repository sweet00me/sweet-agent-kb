
import os
import json
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


# =====================================
# 1. 基础配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_ROOT = Path("/home/sweet/Storage/KnowledgeBase")

load_dotenv(PROJECT_ROOT / ".env")

api_key = os.getenv("DEEPSEEK_API_KEY")

if not api_key or api_key == "replace_with_your_key":
    raise RuntimeError("请先配置 DEEPSEEK_API_KEY")

client = OpenAI(
    api_key=api_key,
    base_url=os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com"
    ),
    timeout=60.0,
    max_retries=1
)

model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")


# =====================================
# 2. 定义一个真实的 Python 工具
# =====================================

def list_knowledge_directories():
    """读取私人知识库的一级目录。"""

    if not KNOWLEDGE_ROOT.is_dir():
        return {
            "error": "知识库根目录不存在"
        }

    directories = sorted(
        p.name
        for p in KNOWLEDGE_ROOT.iterdir()
        if p.is_dir()
    )

    return {
        "root": str(KNOWLEDGE_ROOT),
        "directories": directories,
        "count": len(directories)
    }


# =====================================
# 3. 将工具描述提供给大模型
# =====================================
"""
name：告诉大模型工具叫什么。
description：告诉大模型什么时候应该使用它。
parameters：告诉大模型调用时需要提供什么参数。
properties：定义有哪些参数以及参数类型。
required：定义哪些参数必填。
additionalProperties：控制是否允许额外参数。
Python 函数：实际执行工具的业务逻辑。
"""

tools = [
    {
        "type": "function",
        "function": {
            "name": "list_knowledge_directories",
            "description": (
                "读取 sweetserver 私人知识库的"
                "一级子目录列表。"
                "查询知识库目录结构时使用。"
            ),
            "parameters": {
                "type": "object",  # 表示工具调用参数的整体结构是一个 JSON 对象，即使用 {} 表示。
                "properties": {},  # 用于声明工具可以接受哪些参数。空字典 {}，表示这个函数没有定义任何可传入参数。
                "required": [],    # 用于列出必须提供的参数名。空列表 [] 表示没有必填参数。
                "additionalProperties": False   
            }
        }
    }
]


# =====================================
# 4. 第一次请求：让模型选择工具
# =====================================

messages = [
    {
        "role": "system",
        "content": (
            "你是运行在 sweetserver 上的科研助手。"
            "回答服务器信息时必须依据工具返回的真实数据。"
        )
    },
    {
        "role": "user",
        "content": "帮我查看当前知识库有哪些一级目录。"
    }
]

print("========== 第一次 API 请求 ==========")

response = client.chat.completions.create(
    model=model,
    messages=messages,
    tools=tools,
    tool_choice="required",
    max_tokens=300,
    extra_body={"thinking": {"type": "disabled"}}
)

message = response.choices[0].message

if not message.tool_calls:
    raise RuntimeError("模型没有返回工具调用请求")

# 保留模型返回的工具调用消息
messages.append(message)


# =====================================
# 5. 本地执行模型请求的工具
# =====================================

for call in message.tool_calls:

    name = call.function.name
    arguments = json.loads(
        call.function.arguments or "{}"
    )

    print(f"模型请求调用：{name}")
    print(f"调用参数：{arguments}")

    # 只允许调用明确授权的工具
    if name == "list_knowledge_directories" and not arguments:
        result = list_knowledge_directories()
    else:
        result = {"error": "不支持的工具或参数"}

    print("\n========== 本地工具执行结果 ==========")
    print(json.dumps(
        result,
        ensure_ascii=False,
        indent=2
    ))

    # 将工具结果放回对话历史
    messages.append({
        "role": "tool",
        "tool_call_id": call.id,
        "content": json.dumps(
            result,
            ensure_ascii=False
        )
    })


# =====================================
# 6. 第二次请求：让模型解释结果
# =====================================

print("\n========== 第二次 API 请求 ==========")

final_response = client.chat.completions.create(
    model=model,
    messages=messages,
    tools=tools,
    tool_choice="none",
    max_tokens=500,
    extra_body={"thinking": {"type": "disabled"}}
)

answer = final_response.choices[0].message.content

print("\n========== DeepSeek 最终回答 ==========")
print(answer or "(模型未返回文本内容)")

# 显示两次 API 请求的 Token 用量
first_tokens = (
    response.usage.total_tokens
    if response.usage else 0
)

second_tokens = (
    final_response.usage.total_tokens
    if final_response.usage else 0
)

print("\n========== API Token 统计 ==========")
print(f"第一次请求：{first_tokens}")
print(f"第二次请求：{second_tokens}")
print(f"总计：{first_tokens + second_tokens}")
