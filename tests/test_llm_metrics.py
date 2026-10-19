
"""
文件：tests/test_llm_metrics.py

用途：
    验证 LLM Token 指标的提取与 SQLite 持久化。

测试内容：
    1. 正确提取 input/output/total Tokens。
    2. 正确读取缓存命中 Token。
    3. 相同响应重复提交不会重复计数。
    4. 缺失 Token 数据时保留 NULL。

说明：
    本测试构造 AIMessage，不调用 DeepSeek API。
"""

import uuid

from langchain_core.messages import AIMessage

from app.observability.llm_metrics import (
    extract_usage,
    record_llm_response,
    list_llm_metrics,
)


thread_id = "metrics-test-" + uuid.uuid4().hex[:12]

config = {
    "configurable": {
        "thread_id": thread_id
    }
}


# =====================================
# 1. 构造带 Token 信息的模型响应
# =====================================

response = AIMessage(
    id="response-" + uuid.uuid4().hex,
    content="模拟模型回答",
    response_metadata={
        "model_name": "deepseek-flash",
        "finish_reason": "stop",
    },
    usage_metadata={
        "input_tokens": 741,
        "output_tokens": 144,
        "total_tokens": 885,
        "input_token_details": {
            "cache_read": 512
        },
    },
)


# =====================================
# 2. 验证 Token 解析
# =====================================

print("========== 1. Token 解析 ==========")

usage = extract_usage(response)

print(usage)

assert usage["input_tokens"] == 741
assert usage["output_tokens"] == 144
assert usage["total_tokens"] == 885
assert usage["cache_read_tokens"] == 512

print("PASS：Token 信息解析正确")


# =====================================
# 3. 验证持久化和重复记录
# =====================================

print("\n========== 2. SQLite 持久化 ==========")

record_llm_response(
    config=config,
    response=response,
    call_kind="agent",
    duration_ms=1106.59,
)

# 再记录一次相同响应，应当去重。
record_llm_response(
    config=config,
    response=response,
    call_kind="agent",
    duration_ms=1106.59,
)

records = list_llm_metrics(thread_id)

assert len(records) == 1
assert records[0]["status"] == "completed"
assert records[0]["total_tokens"] == 885

print("PASS：重复响应没有重复计数")


# =====================================
# 4. 验证 Token 数据缺失
# =====================================

print("\n========== 3. 缺失数据处理 ==========")

response_without_usage = AIMessage(
    id="response-" + uuid.uuid4().hex,
    content="没有 Token 统计信息的响应",
    response_metadata={
        "model_name": "deepseek-flash"
    },
)

record_llm_response(
    config=config,
    response=response_without_usage,
    call_kind="summary",
    duration_ms=520.0,
)

records = list_llm_metrics(thread_id)

assert len(records) == 2

missing_record = records[1]

assert missing_record["call_kind"] == "summary"
assert missing_record["input_tokens"] is None
assert missing_record["output_tokens"] is None
assert missing_record["total_tokens"] is None

print("PASS：缺失 Token 数据保留为 NULL")

print("\nLLM Metrics 测试全部通过")
