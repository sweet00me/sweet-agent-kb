
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# 1. 定位项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 2. 读取项目目录下的 .env
load_dotenv(PROJECT_ROOT / ".env")

# 3. 获取 API 配置
api_key = os.getenv("DEEPSEEK_API_KEY")
base_url = os.getenv(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com"
)
model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

if not api_key or api_key == "replace_with_your_key":
    raise RuntimeError("请先在 .env 中配置真实 API Key")

# 4. 创建 API 客户端
client = OpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=60.0,
    max_retries=1
)

# 5. 准备消息
messages = [
    {
        "role": "system",
        "content": "你是一名严谨的人工智能研究助手，请使用中文回答。"
    },
    {
        "role": "user",
        "content": "请用三句话解释 RAG 与 Agent 的区别。"
    }
]

print("正在测试 DeepSeek API...")
print(f"使用模型：{model}")

# 6. 发送请求
try:
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=256,
        extra_body={"thinking": {"type": "disabled"}}
    )

    # 7. 获取模型回答
    answer = response.choices[0].message.content

    print("\n========== 模型回答 ==========")
    print(answer or "(没有返回文本内容)")

    # 8. 显示 Token 使用量
    if response.usage:
        print("\n========== Token 用量 ==========")
        print(f"输入：{response.usage.prompt_tokens}")
        print(f"输出：{response.usage.completion_tokens}")
        print(f"总计：{response.usage.total_tokens}")

    print("\nAPI 测试成功！")

except Exception as exc:
    print(f"\nAPI 调用失败：{type(exc).__name__}")
    print(f"HTTP 状态：{getattr(exc, 'status_code', '未获得')}")
    print("请检查 API 地址、模型 ID、余额和网络连接。")
    raise SystemExit(1)
