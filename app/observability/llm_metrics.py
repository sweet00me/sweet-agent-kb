
"""
文件：app/observability/llm_metrics.py

用途：
    记录 Sweet Research Agent 的大模型调用指标。

主要功能：
    1. 统计 DeepSeek API 调用耗时。
    2. 保存真实 Token 使用量。
    3. 记录缓存命中 Token 数量。
    4. 区分普通 Agent 调用和摘要调用。
    5. 保存模型调用失败记录。
    6. 按 thread_id 查询历史指标。

隐私说明：
    不保存 Prompt、用户问题、模型回答或工具参数。

设计原则：
    Metrics 属于辅助观测数据。
    记录失败时打印警告，但不重新执行模型调用。
"""

import sqlite3
import time
import uuid

from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.observability.audit import (
    AUDIT_DB_PATH,
    get_thread_id,
    prepare_database,
)


# =====================================
# 1. 创建 LLM 使用量表
# =====================================

CREATE_LLM_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    created_at TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    response_id TEXT NOT NULL,

    call_kind TEXT NOT NULL,
    model_name TEXT,

    duration_ms REAL NOT NULL,

    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_read_tokens INTEGER,

    status TEXT NOT NULL,
    error_type TEXT,

    UNIQUE(thread_id, response_id)
)
"""


# =====================================
# 2. Token 字段校验
# =====================================

def valid_token_count(value):
    """
    只接受非负整数。

    未返回的 Token 数据保留为 None，
    不错误地当作零。
    """

    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value

    return None


# =====================================
# 3. 从 AIMessage 提取使用量
# =====================================

def extract_usage(response) -> dict:
    """
    优先读取 LangChain 标准化 usage_metadata。

    如果标准化数据缺失，再尝试提供商返回的
    response_metadata.token_usage。

    cache_read_tokens 是 input_tokens 的一部分，
    不能重复加入总 Token 数。
    """

    usage = response.usage_metadata or {}
    metadata = response.response_metadata or {}

    raw = metadata.get("token_usage") or {}

    if not isinstance(raw, dict):
        raw = {}

    input_tokens = valid_token_count(
        usage.get("input_tokens")
    )

    output_tokens = valid_token_count(
        usage.get("output_tokens")
    )

    total_tokens = valid_token_count(
        usage.get("total_tokens")
    )

    # OpenAI 兼容接口可能使用原始字段名。
    if input_tokens is None:
        input_tokens = valid_token_count(
            raw.get("prompt_tokens")
        )

    if output_tokens is None:
        output_tokens = valid_token_count(
            raw.get("completion_tokens")
        )

    if total_tokens is None:
        total_tokens = valid_token_count(
            raw.get("total_tokens")
        )

    # 只有输入和输出 Token 都已知，
    # 才允许计算缺失的 total_tokens。
    if (
        total_tokens is None
        and input_tokens is not None
        and output_tokens is not None
    ):
        total_tokens = input_tokens + output_tokens

    details = usage.get("input_token_details") or {}

    if not isinstance(details, dict):
        details = {}

    cache_read = valid_token_count(
        details.get("cache_read")
    )

    # 部分兼容接口使用原始缓存字段。
    if cache_read is None:
        cache_read = valid_token_count(
            raw.get("prompt_cache_hit_tokens")
        )

    if cache_read is None:
        prompt_details = (
            raw.get("prompt_tokens_details") or {}
        )

        if isinstance(prompt_details, dict):
            cache_read = valid_token_count(
                prompt_details.get("cached_tokens")
            )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read,
    }


# =====================================
# 4. 保存一条指标
# =====================================

def save_metric(metric: dict):
    """
    将模型指标持久化到 SQLite。

    使用 thread_id + response_id 去重。
    同一条模型响应重复记录时不会重复计数。

    注意：
        如果节点崩溃后重新调用了 API，
        并产生新的 response_id，那是一次新的模型调用，
        会记录为新的指标。
    """

    prepare_database()

    conn = sqlite3.connect(
        str(AUDIT_DB_PATH),
        timeout=10
    )

    try:
        conn.execute(CREATE_LLM_USAGE_SQL)

        conn.execute(
            """
            INSERT INTO llm_usage (
                created_at,
                thread_id,
                response_id,
                call_kind,
                model_name,
                duration_ms,
                input_tokens,
                output_tokens,
                total_tokens,
                cache_read_tokens,
                status,
                error_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, response_id)
            DO NOTHING
            """,
            (
                metric["created_at"],
                metric["thread_id"],
                metric["response_id"],
                metric["call_kind"],
                metric["model_name"],
                metric["duration_ms"],
                metric["input_tokens"],
                metric["output_tokens"],
                metric["total_tokens"],
                metric["cache_read_tokens"],
                metric["status"],
                metric["error_type"],
            )
        )

        conn.commit()

    finally:
        conn.close()


# =====================================
# 5. 记录成功的模型响应
# =====================================

def record_llm_response(
    config: RunnableConfig,
    response,
    call_kind: str,
    duration_ms: float,
):
    """
    记录模型成功返回的响应。

    response_id 优先使用模型响应自身的 ID，
    便于与 LangChain 的消息关联。
    """

    if call_kind not in ("agent", "summary", "citation_check"):
        raise ValueError("未知的模型调用类型")

    metadata = response.response_metadata or {}

    response_id = getattr(
        response, "id", None
    )

    if not isinstance(response_id, str) or not response_id:
        response_id = "local-" + uuid.uuid4().hex

    usage = extract_usage(response)

    save_metric({
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(timespec="milliseconds"),

        "thread_id": get_thread_id(config),
        "response_id": response_id,
        "call_kind": call_kind,
        "model_name": metadata.get("model_name"),

        "duration_ms": round(duration_ms, 2),

        **usage,

        "status": "completed",
        "error_type": None,
    })


# =====================================
# 6. 模型调用失败时记录错误
# =====================================

def record_llm_error(
    config: RunnableConfig,
    call_kind: str,
    duration_ms: float,
    error_type: str,
):
    """
    API 调用失败时记录一条错误指标。

    由于没有成功响应，Token 数据保持为 NULL。
    不假设失败调用没有产生计费。
    """

    save_metric({
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(timespec="milliseconds"),

        "thread_id": get_thread_id(config),
        "response_id": "error-" + uuid.uuid4().hex,
        "call_kind": call_kind,
        "model_name": None,
        "duration_ms": round(duration_ms, 2),

        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cache_read_tokens": None,

        "status": "error",
        "error_type": error_type,
    })


# =====================================
# 7. 带指标采集的模型调用
# =====================================

def invoke_with_metrics(
    model: Any,
    messages: list,
    config: RunnableConfig,
    call_kind: str,
):
    """
    调用模型并自动记录 Token 和耗时。

    重要：
        先获得模型结果，再记录指标。

        指标写入失败不能触发模型重新调用，
        否则可能造成重复 API 费用。
    """

    # 在消耗 API 费用前验证基本配置。
    get_thread_id(config)

    if call_kind not in ("agent", "summary", "citation_check"):
        raise ValueError("未知的模型调用类型")

    started = time.perf_counter()

    try:
        response = model.invoke(messages)

    except Exception as exc:

        duration_ms = (
            time.perf_counter() - started
        ) * 1000

        try:
            record_llm_error(
                config,
                call_kind,
                duration_ms,
                type(exc).__name__,
            )

        except Exception as metric_exc:
            print(
                "[Metrics] 错误指标保存失败：",
                type(metric_exc).__name__
            )

        # 不吞掉原始模型调用异常。
        raise

    duration_ms = (
        time.perf_counter() - started
    ) * 1000

    try:
        record_llm_response(
            config,
            response,
            call_kind,
            duration_ms,
        )

    except Exception as exc:
        # API 已经返回成功，不应因为指标数据库故障
        # 再次调用大模型。
        print(
            "[Metrics] 使用量保存失败：",
            type(exc).__name__
        )

    return response


# =====================================
# 8. 查询 LLM 使用量
# =====================================

def list_llm_metrics(
    thread_id: str,
) -> list[dict]:
    """返回指定线程的模型调用记录。"""

    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id 不能为空")

    if not AUDIT_DB_PATH.is_file():
        return []

    conn = sqlite3.connect(
        str(AUDIT_DB_PATH),
        timeout=10
    )

    conn.row_factory = sqlite3.Row

    try:
        exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'llm_usage'
            """
        ).fetchone()

        if not exists:
            return []

        rows = conn.execute(
            """
            SELECT
                created_at,
                thread_id,
                response_id,
                call_kind,
                model_name,
                duration_ms,
                input_tokens,
                output_tokens,
                total_tokens,
                cache_read_tokens,
                status,
                error_type
            FROM llm_usage
            WHERE thread_id = ?
            ORDER BY id ASC
            """,
            (thread_id,)
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        conn.close()
