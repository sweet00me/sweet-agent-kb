
import os
import json
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# 复用之前已经实现的知识库检索函数
from search_knowledge import search_knowledge


# =====================================
# 1. 基础配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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


# =====================================
# 2. 向 DeepSeek 注册 RAG 工具
# =====================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "检索 sweetserver 私人知识库中的"
                "科研笔记、论文和实验资料。"
                "当用户询问私人知识库内容、"
                "自己的实验或文档时使用。"
                "返回文档片段、来源及相似度。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "需要检索的具体问题"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "最多检索多少个文档片段",
                        "minimum": 1,
                        "maximum": 3
                    }
                },
                "required": ["question"],
                "additionalProperties": False
            }
        }
    }
]


# =====================================
# 3. 本地工具执行器
# =====================================

def execute_tool(name, arguments):
    """
    根据模型请求，执行授权的 Python 工具。

    注意：
    大模型只负责提出工具调用请求。
    Python 程序负责验证参数并真正执行。
    """

    if name != "search_knowledge":
        return {"error": "未授权的工具"}

    try:
        args = json.loads(arguments or "{}")

        if not isinstance(args, dict): #建立 Python 程序的执行权限边界，而不是让模型自行决定什么是安全操作
            raise ValueError("工具参数必须是 JSON 对象")

        if set(args) - {"question", "top_k"}: #检查是否存在未授权参数
            raise ValueError("存在未授权的参数")

        question = args.get("question")
        top_k = args.get("top_k", 2)

        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 必须是非空字符串")

        if type(top_k) is not int or not 1 <= top_k <= 3:
            raise ValueError("top_k 必须是 1 到 3 的整数")

        records = search_knowledge(
            question=question,
            top_k=top_k
        )

        # 将检索结果整理为结构化数据 并存起来
        matches = []

        for record in records:
            matches.append({
                "source": record["source"],
                "chunk_index": record["chunk_index"],
                "similarity": round(
                    record["similarity"], 4
                ),
                "content": record["content"]
            })

        print(f"[知识库检索] {question}")
        print(f"[找到片段] {len(matches)}")

        for item in matches:
            print(
                f"  {item['source']} "
                f"chunk={item['chunk_index']} "
                f"similarity={item['similarity']}"
            )

        return {"matches": matches}
    
    #异常捕获与处理 如果前面的 try 代码块中发生了 ValueError 或 TypeError 异常，就捕获异常，将错误信息转换成字符串，再以字典形式返回，而不是让这个异常直接中断当前函数
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}


# =====================================
# 4. Agent 问答函数
# =====================================

def ask_agent(question):

    print("\n" + "=" * 60)
    print("用户问题：", question)
    print("=" * 60)

    messages = [
        {
            "role": "system",
            "content": (
                "你是 sweetserver 的私人科研助手。"
                "当问题涉及私人知识库、用户自己的实验"
                "或本地文档时，必须先调用知识库检索工具。"
                "普通通用知识问题可以直接回答。"
                "对于私人资料，只能依据工具返回的内容回答。"
                "引用资料时，请标注来源文件和 Chunk 编号。"
                "相似度仅用于判断相关性，不能证明事实正确。"
                "如果检索结果不足以支持结论，"
                "必须明确说明没有找到足够信息，"
                "不要编造实验结果或具体数值。"
            )
        },
        {
            "role": "user",
            "content": question
        }
    ]

    total_tokens = 0

    # ---------------------------------
    # 第一次请求：模型选择工具
    # ---------------------------------

    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=500,
        extra_body={
            "thinking": {"type": "disabled"}
        }
    )

    message = response.choices[0].message

    if response.usage:
        total_tokens += response.usage.total_tokens

    # ---------------------------------
    # 情况 A：模型直接回答
    # ---------------------------------

    if not message.tool_calls:
        print("\n[Agent 决策] 不调用知识库")
        print("\n[最终回答]")
        print(message.content)
        print("\n[Token]", total_tokens)
        return

    # ---------------------------------
    # 情况 B：模型调用知识库
    # ---------------------------------

    print("\n[Agent 决策] 调用知识库")

    messages.append(message)

    for call in message.tool_calls:

        name = call.function.name
        arguments = call.function.arguments

        print(f"\n[工具调用] {name}")

        # 真正执行本地 Python 函数
        result = execute_tool(name, arguments)

        # 将结果返回给大模型
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(
                result,
                ensure_ascii=False
            )
        })

    # ---------------------------------
    # 第二次请求：基于知识生成回答
    # ---------------------------------

    final = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="none",
        max_tokens=800,
        extra_body={
            "thinking": {"type": "disabled"}
        }
    )

    if final.usage:
        total_tokens += final.usage.total_tokens

    print("\n========== Agent 最终回答 ==========")
    print(final.choices[0].message.content)

    print("\n========== Token 统计 ==========")
    print("总 Token：", total_tokens)


# =====================================
# 5. 测试入口
# =====================================

if __name__ == "__main__":

    ask_agent(
        "根据我的知识库，RAG 与 Agent "
        "有什么关系？请给出来源。"
    )

    ask_agent(
        "根据我的知识库，ADE20K 用于什么任务？"
        "语义分割有哪些评价指标？请引用来源。"
    )

    ask_agent(
        "我的 SD-LoRA 在 ADE20K 上的最终 "
        "mIoU 具体是多少？只根据知识库回答。"
    )
