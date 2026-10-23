
"""
文件：app/api/main.py

用途：
    Sweet Research Agent 的 FastAPI 服务入口。

当前功能：
    1. 启动和关闭时管理 LangGraph Checkpointer。
    2. 提供健康检查 API。
    3. 将聊天请求转发给正式 Agent。
    4. 识别 Human-in-the-loop 中断状态。
    5. 查询指定线程的运行状态。

设计说明：
    本阶段使用同步 LangGraph 和 SQLite。
    采用单进程、单执行锁的简化方案。

    暂不支持：
        - HTTP 人工审批恢复
        - SSE 流式输出
        - 多用户认证
        - 并行执行多个 Agent 请求

安全要求：
    目前只允许绑定到 127.0.0.1。
    不应直接开放到公网。
"""

import os
import uuid
import logging
import threading
from fastapi import Query
from app.api import session_store

from contextlib import asynccontextmanager
from typing import Annotated, Literal
from app.api.recovery import diagnose_request
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    Path,
)
from pydantic import BaseModel, Field, StringConstraints

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
)

from app.agents.graph import build_graph
from app.memory.checkpoint import open_checkpointer
from app.rag.auto_sync import AutoSyncService, settings_from_env

from langgraph.types import Command
from app.api.files import router as files_router

from app.api import request_journal
from app.api.request_journal import (
    RequestConflictError,
)

from app.api.approval_service import (
    validate_review_request,
    ApprovalValidationError,
)

from concurrent.futures import ThreadPoolExecutor

from fastapi.responses import StreamingResponse


from app.api.streaming import (
    StreamJob,
    encode_sse,
    iter_sse_events,
    run_chat_stream,
    run_review_stream,
)









logger = logging.getLogger(__name__)

THREAD_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"


def reject_deleted_thread(thread_id: str):
    """已软删除的会话不能读取 Checkpoint、重放响应或重新执行。"""
    if session_store.is_thread_deleted(thread_id):
        raise HTTPException(status_code=404, detail="会话不存在")


# =====================================
# 1. API 请求与响应模型
# =====================================

class ChatRequest(BaseModel):
    """
    Agent 聊天请求。

    request_id：
        可选请求幂等标识。

    兼容性：
        不提供 request_id 时，保留旧版聊天行为。

        提供 request_id 时，必须同时提供 thread_id。
    """

    message: str = Field(
        min_length=1,
        max_length=10000,
    )

    thread_id: str | None = Field(
        default=None,
        pattern=THREAD_PATTERN,
        max_length=64,
    )

    request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$",
    )

class ChatResponse(BaseModel):
    """Agent 的非流式响应。"""

    thread_id: str

    status: Literal[
        "completed",
        "pending_approval",
    ]

    answer: str | None = None

    pending_approvals: list[dict] = Field(
        default_factory=list
    )

    citation_report: dict | None = None

class ReviewRequest(BaseModel):
    """
    HTTP 工具审批请求。

    request_id 用于避免网络重试造成重复审批。
    不传时保留旧版接口的行为。
    """

    decisions: dict[
        str,
        Literal["approve", "reject"]
    ] = Field(min_length=1)

    expected_fingerprints: dict[str, str] = Field(
        min_length=1
    )

    request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$",
    )

# =====================================
# 2. 管理 FastAPI 生命周期
# =====================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期管理。

    管理 LangGraph Checkpointer 和 SSE 后台执行器。
    """

    os.umask(0o077)

    session_store.initialize_database()
    request_journal.initialize_request_journal()

    with open_checkpointer() as saver:

        app.state.graph = build_graph(
            checkpointer=saver,
            enable_summarization=True,
        )

        app.state.graph_lock = threading.Lock()

        # 当前只允许一个 Agent 执行任务。
        # 不使用多 Worker，也不并行写入相同
        # SQLite Checkpoint。
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sweet-agent-sse",
        )

        app.state.sse_executor = executor
        # 同一进程更新 Chroma，避免独立定时脚本使检索缓存停留在旧版本。
        auto_sync = AutoSyncService(graph_lock=app.state.graph_lock, **settings_from_env())
        app.state.auto_sync = auto_sync
        # 无法获得所有权时启动失败，避免在外部 CLI 仍写入时打开旧的查询缓存。
        auto_sync.start(wait_for_ownership=True)

        logger.info(
            "Sweet Research Agent API started"
        )

        try:
            yield

        finally:

            # 在关闭 Checkpointer 之前等待任务结束。
            executor.shutdown(wait=True)
            auto_sync.stop()

            logger.info(
                "Sweet Research Agent API stopped"
            )


app = FastAPI(
    title="Sweet Research Agent API",
    version="0.2.0-dev",
    description="科研 Agent 的 HTTP 服务接口",
    lifespan=lifespan,
)

# =====================================
# 注册文件管理 API
# =====================================

app.include_router(files_router)

# =====================================
# 3. 创建 LangGraph 配置
# =====================================

def make_config(thread_id: str) -> dict:
    """所有请求统一使用相同的线程配置结构。"""

    return {
        "configurable": {
            "thread_id": thread_id
        },
        "recursion_limit": 12,
    }


# =====================================
# 4. 提取待审批工具调用
# =====================================

def extract_pending_approvals(snapshot) -> list[dict]:
    """
    从 Graph 的中断快照提取待审批信息。

    目前只处理统一 Approval Node 的工具审批，
    不接受其他未知类型的中断作为执行授权。
    """

    pending = []

    for item in snapshot.interrupts:

        payload = item.value

        if not isinstance(payload, dict):
            continue

        if payload.get("type") != "tool_approval":
            continue

        for call in payload.get("calls", []):

            pending.append({
                "tool_call_id": call["id"],
                "tool_name": call["name"],
                "arguments": call["args"],
                "fingerprint": call["fingerprint"],
            })

    return pending


# =====================================
# 5. 获取 Agent 当前结果
# =====================================

def make_chat_response(graph, thread_id: str):
    """
    从持久化 State 构造响应。

    先检查是否中断，
    再检查是否仍有待执行节点，
    最后才读取模型最终回答。
    """

    snapshot = graph.get_state(
        make_config(thread_id)
    )

    # 优先识别 HITL 暂停。
    if snapshot.interrupts:

        return ChatResponse(
            thread_id=thread_id,
            status="pending_approval",
            answer=None,
            pending_approvals=extract_pending_approvals(
                snapshot
            ),
        )

    # 非审批原因导致工作流尚未结束。
    # 不允许把它错误地解释为正常回答。
    if snapshot.next:

        raise HTTPException(
            status_code=409,
            detail="当前工作流尚未完成，请检查线程状态",
        )

    messages = snapshot.values.get(
        "messages", []
    )

    if not messages:
        raise HTTPException(
            status_code=500,
            detail="Agent 没有返回消息",
        )

    last = messages[-1]

    if (
        not isinstance(last, AIMessage)
        or last.tool_calls
        or not isinstance(last.content, str)
    ):
        raise HTTPException(
            status_code=500,
            detail="没有获得有效的最终 AI 回答",
        )

    return ChatResponse(
        thread_id=thread_id,
        status="completed",
        answer=last.content,
        citation_report=snapshot.values.get("citation_report"),
    )


# =====================================
# 6. 健康检查
# =====================================

@app.get("/api/v1/health")
def health():
    """检查 HTTP 服务是否能够正常响应。"""

    return {
        "status": "ok",
        "service": "sweet-research-agent",
        "version": "0.2.0-dev",
    }


# =====================================
# 7. 聊天接口
# =====================================


@app.post(
    "/api/v1/chat",
    response_model=ChatResponse,
)
def chat(
    payload: ChatRequest,
    request: Request,
):
    """
    非流式聊天接口，支持 request_id 幂等保护。

    处理流程：
        1. 校验请求。
        2. 检查是否为已经处理过的请求。
        3. 检查 LangGraph 线程状态。
        4. 登记并声明请求执行权。
        5. 记录用户消息。
        6. 执行正式 LangGraph。
        7. 保存最终回答。
        8. 保存本次 HTTP 响应。

    说明：
        目前 request_id 可选。
        未提供时保留原有聊天行为。
    """

    # =====================================
    # 1. 基础参数校验
    # =====================================

    message = payload.message.strip()

    if not message:
        raise HTTPException(
            status_code=422,
            detail="消息不能为空",
        )

    request_id = payload.request_id

    if request_id and not payload.thread_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "使用 request_id 时，"
                "必须同时提供 thread_id"
            ),
        )

    thread_id = (
        payload.thread_id
        or "research-" + uuid.uuid4().hex[:16]
    )

    graph = request.app.state.graph
    lock = request.app.state.graph_lock

    config = make_config(thread_id)

    # =====================================
    # 2. 使用现有全局执行锁
    # =====================================

    # 当前单进程方案下，使用与 SSE、
    # Review API 相同的锁。
    #
    # 保证检查重复请求、读取 Checkpoint、
    # 登记请求和执行 Agent 的流程不会
    # 被本服务内其他请求插入。
    with lock:

        reject_deleted_thread(thread_id)

        # =================================
        # 3. 优先处理重复请求
        # =================================

        if request_id:

            existing = request_journal.get_request(
                request_id
            )

            if existing is not None:

                # register_request 在发现旧请求时，
                # 会重新核对 thread_id、
                # operation 和 payload_sha256。
                #
                # 不能仅根据 request_id 相同
                # 就直接返回其他请求的结果。
                try:

                    record, created = (
                        request_journal.register_request(
                            request_id=request_id,
                            thread_id=thread_id,
                            operation="chat",
                            payload={
                                "message": message,
                            },
                        )
                    )

                except RequestConflictError as exc:

                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "REQUEST_ID_CONFLICT",
                            "message": (
                                "该 request_id 已用于"
                                "其他不同的请求"
                            ),
                        },
                    ) from exc

                if created:
                    raise HTTPException(
                        status_code=500,
                        detail="请求登记状态异常",
                    )

                # 已完成或已暂停的请求：
                # 直接返回首次保存的 HTTP 响应。
                if (
                    record["status"]
                    in ("completed", "pending_approval")
                    and isinstance(
                        record["response"], dict
                    )
                ):

                    return ChatResponse.model_validate(
                        record["response"]
                    )

                # accepted、running、uncertain：
                # 全部禁止自动重新执行。
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "REQUEST_NOT_REPLAYABLE",
                        "request_id": request_id,
                        "status": record["status"],
                        "message": (
                            "该请求已经登记，"
                            "但当前不能安全地重新执行。"
                            "请检查会话状态。"
                        ),
                    },
                )

        # =================================
        # 4. 检查 LangGraph 当前状态
        # =================================

        snapshot = graph.get_state(config)

        if snapshot.interrupts:

            raise HTTPException(
                status_code=409,
                detail=(
                    "当前会话存在待审批工具调用，"
                    "请先完成审批"
                ),
            )

        if snapshot.next:

            raise HTTPException(
                status_code=409,
                detail="当前会话工作流尚未完成",
            )

        # =================================
        # 5. 确保会话已经注册
        # =================================

        session_store.ensure_thread(
            thread_id,
            title=message[:50].replace("\n", " "),
        )

        journal_created = False

        # =================================
        # 6. 登记新的请求
        # =================================

        if request_id:

            try:

                record, created = (
                    request_journal.register_request(
                        request_id=request_id,
                        thread_id=thread_id,
                        operation="chat",
                        payload={
                            "message": message,
                        },
                    )
                )

            except RequestConflictError as exc:

                raise HTTPException(
                    status_code=409,
                    detail="request_id 已用于其他请求",
                ) from exc

            if not created:

                # 防御性检查：
                # 正常单进程锁下，重复请求应该
                # 已经在前面的分支中返回。
                raise HTTPException(
                    status_code=409,
                    detail="请求已被其他执行流程登记",
                )

            journal_created = True

        # =================================
        # 7. 执行 Agent
        # =================================

        try:

            if request_id:

                # 状态 accepted -> running。
                # 如果已经被声明执行，则会拒绝。
                request_journal.start_request(
                    request_id
                )

            # 独立保存完整用户消息。
            session_store.append_message(
                thread_id,
                "user",
                message,
            )

            # 执行正式 LangGraph。
            graph.invoke(
                {
                    "messages": [
                        HumanMessage(
                            content=message
                        )
                    ]
                },
                config=config,
            )

            # 从 Checkpoint 中读取最终状态。
            response = make_chat_response(
                graph,
                thread_id,
            )

            # =====================================
            # 8. 保存最终回答和请求结果
            # =====================================

            if response.status == "completed":

                if response.answer is None:
                    raise RuntimeError(
                        "已完成的请求缺少最终回答"
                    )

                if request_id:

                    # 新客户端：
                    # 助手消息与请求完成状态原子提交。
                    request_journal.complete_request_with_message(
                        request_id,
                        thread_id=thread_id,
                        answer=response.answer,
                        response=response.model_dump(),
                    )

                else:

                    # 旧客户端：
                    # 未提供 request_id，保留原有消息保存方式。
                    session_store.append_message(
                        thread_id,
                        "assistant",
                        response.answer,
                    )

            elif response.status == "pending_approval":

                # 等待审批时还没有最终回答，
                # 只更新请求状态，不插入助手消息。
                if request_id:

                    request_journal.finish_request(
                        request_id,
                        status="pending_approval",
                        response=response.model_dump(),
                    )

            return response

        except Exception as exc:

            # =================================
            # 10. 异常时标记结果不确定
            # =================================

            # 一旦登记请求，就不应在网络重试时
            # 盲目再次调用 Graph。
            #
            # 即使本次异常发生在实际模型调用前，
            # 这里仍保守地标记为 uncertain。
            if request_id and journal_created:

                try:

                    record = request_journal.get_request(
                        request_id
                    )

                    if (
                        record is not None
                        and record["status"]
                        in ("accepted", "running")
                    ):

                        request_journal.mark_uncertain(
                            request_id
                        )

                except Exception as journal_exc:

                    logger.error(
                        "Failed to mark uncertain: %s",
                        type(journal_exc).__name__,
                    )

            logger.error(
                "Agent chat failed: %s",
                type(exc).__name__,
            )

            if isinstance(exc, HTTPException):
                raise

            # 不向 HTTP 客户端泄露内部异常详情。
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "AGENT_EXECUTION_FAILED",
                    "request_id": request_id,
                    "message": (
                        "Agent 执行未正常结束。"
                        "请先查询请求状态，"
                        "不要自动重新提交。"
                    ),
                },
            ) from exc


# =====================================
# 8. 查询指定会话状态
# =====================================

@app.get(
    "/api/v1/threads/{thread_id}/status"
)
def get_thread_status(
    request: Request,
    thread_id: str = Path(
        min_length=1,
        max_length=64,
        pattern=THREAD_PATTERN,
    ),
):
    """
    查询指定线程的最新状态。

    当前只返回运行状态和待审批请求，
    不返回完整聊天历史。

    后续会单独提供历史聊天接口。
    """

    graph = request.app.state.graph
    lock = request.app.state.graph_lock

    with lock:

        reject_deleted_thread(thread_id)

        snapshot = graph.get_state(
            make_config(thread_id)
        )

        messages = snapshot.values.get(
            "messages", []
        )

        if not messages and not snapshot.next:

            # 已在 Session 数据库注册，但尚未开始聊天。
            if session_store.get_thread(thread_id) is not None:
                return {
                    "thread_id": thread_id,
                    "status": "empty",
                    "message_count": 0,
                    "pending_approvals": [],
                }

            raise HTTPException(
                status_code=404,
                detail="会话不存在",
            )

        if snapshot.interrupts:
            status = "pending_approval"

        elif snapshot.next:
            status = "incomplete"

        else:
            status = "completed"

        return {
            "thread_id": thread_id,
            "status": status,
            "message_count": len(messages),
            "pending_approvals": (
                extract_pending_approvals(snapshot)
                if snapshot.interrupts
                else []
            ),
        }


# =====================================
# 会话管理 API
# =====================================

class CreateThreadRequest(BaseModel):
    """新建聊天会话的请求模型。"""

    title: str = Field(
        default="新会话",
        min_length=1,
        max_length=80,
    )


class RenameThreadRequest(BaseModel):
    """重命名会话；先去除首尾空白，再校验标题长度。"""

    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    ]


@app.post("/api/v1/threads", status_code=201)
def create_chat_thread(payload: CreateThreadRequest):
    """显式创建一个新会话。"""

    thread_id = (
        "research-" + uuid.uuid4().hex[:16]
    )

    return session_store.create_thread(
        thread_id=thread_id,
        title=payload.title.strip() or "新会话",
    )


@app.get("/api/v1/threads")
def get_chat_threads(
    limit: int = Query(default=50, ge=1, le=100)
):
    """获取最近使用的会话列表。"""

    return {
        "items": session_store.list_threads(limit)
    }


@app.get("/api/v1/threads/{thread_id}")
def get_chat_thread(
    thread_id: str = Path(
        min_length=1,
        max_length=64,
        pattern=THREAD_PATTERN,
    )
):
    """获取一条会话的基本信息。"""

    record = session_store.get_thread(thread_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="会话不存在",
        )

    return record


@app.patch("/api/v1/threads/{thread_id}")
def rename_chat_thread(
    payload: RenameThreadRequest,
    thread_id: str = Path(min_length=1, max_length=64, pattern=THREAD_PATTERN),
):
    """重命名历史会话，返回更新后的会话元数据。"""
    record = session_store.rename_thread(thread_id, payload.title)
    if record is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return record


@app.delete("/api/v1/threads/{thread_id}", status_code=204)
def delete_chat_thread(
    request: Request,
    thread_id: str = Path(min_length=1, max_length=64, pattern=THREAD_PATTERN),
):
    """从界面和会话接口隐藏记录，保留本地历史及审计数据。"""
    lock = request.app.state.graph_lock
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Agent 正在执行，请稍后删除会话")
    try:
        if not session_store.delete_thread(thread_id):
            raise HTTPException(status_code=404, detail="会话不存在")
    finally:
        lock.release()
    return Response(status_code=204)


@app.get("/api/v1/threads/{thread_id}/messages")
def get_chat_messages(
    thread_id: str = Path(
        min_length=1,
        max_length=64,
        pattern=THREAD_PATTERN,
    ),
    limit: int = Query(default=100, ge=1, le=200),
    before_id: int | None = Query(default=None, ge=1),
):
    """分页获取用户与助手的完整聊天历史。"""

    if session_store.get_thread(thread_id) is None:
        raise HTTPException(
            status_code=404,
            detail="会话不存在",
        )

    return session_store.list_messages(
        thread_id=thread_id,
        limit=limit,
        before_id=before_id,
    )


# =====================================
# HTTP 工具审批与恢复 API
# =====================================


@app.post(
    "/api/v1/threads/{thread_id}/review",
    response_model=ChatResponse,
)
def review_thread(
    payload: ReviewRequest,
    request: Request,
    thread_id: str = Path(
        min_length=1,
        max_length=64,
        pattern=THREAD_PATTERN,
    ),
):
    """
    文件：app/api/main.py
    接口：POST /api/v1/threads/{thread_id}/review

    用途：
        在原有 HITL 审批基础上加入 request_id 去重。

    执行顺序：
        1. 检查会话。
        2. 检查相同 request_id 是否已有结果。
        3. 验证当前 Checkpoint 的审批信息。
        4. 登记并声明请求执行权。
        5. 恢复 LangGraph。
        6. 保存最终聊天记录及请求结果。

    异常原则：
        已登记请求发生异常后标记 uncertain。
        不自动重复执行审批。
    """

    graph = request.app.state.graph
    lock = request.app.state.graph_lock

    request_id = payload.request_id

    # 排除 request_id 自身后，计算审批内容指纹。
    # HTTP 和未来 SSE 审批必须采用相同的 payload。
    journal_payload = {
        "decisions": dict(payload.decisions),
        "expected_fingerprints": dict(
            payload.expected_fingerprints
        ),
    }

    with lock:

        # =====================================
        # 1. 检查会话
        # =====================================

        if session_store.get_thread(thread_id) is None:
            raise HTTPException(
                status_code=404,
                detail="会话不存在",
            )

        # =====================================
        # 2. 检查是否为重复请求
        # =====================================

        if request_id:

            existing = request_journal.get_request(
                request_id
            )

            if existing is not None:

                expected_sha = (
                    request_journal.request_fingerprint(
                        thread_id,
                        "review",
                        journal_payload,
                    )
                )

                if (
                    existing["thread_id"] != thread_id
                    or existing["operation"] != "review"
                    or existing["payload_sha256"]
                    != expected_sha
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "REQUEST_ID_CONFLICT",
                            "message": (
                                "该 request_id 已用于"
                                "其他不同的操作"
                            ),
                        },
                    )

                # 已完成或暂停的请求：
                # 直接返回之前持久化的 HTTP 结果。
                if (
                    existing["status"]
                    in ("completed", "pending_approval")
                    and isinstance(
                        existing["response"], dict
                    )
                ):
                    return ChatResponse.model_validate(
                        existing["response"]
                    )

                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "REQUEST_NOT_REPLAYABLE",
                        "request_id": request_id,
                        "status": existing["status"],
                    },
                )

        # =====================================
        # 3. 读取当前待审批状态
        # =====================================

        config = make_config(thread_id)

        snapshot = graph.get_state(config)

        if not snapshot.interrupts:
            raise HTTPException(
                status_code=409,
                detail="当前会话没有待审批操作",
            )

        # =====================================
        # 4. 校验审批工具与参数指纹
        # =====================================

        try:

            resume_payload = validate_review_request(
                snapshot=snapshot,
                decisions=payload.decisions,
                expected_fingerprints=(
                    payload.expected_fingerprints
                ),
            )

        except ApprovalValidationError as exc:

            raise HTTPException(
                status_code=409,
                detail=str(exc),
            ) from exc

        # =====================================
        # 5. 登记新的审批请求
        # =====================================

        journal_registered = False

        if request_id:

            try:

                record, created = (
                    request_journal.register_request(
                        request_id=request_id,
                        thread_id=thread_id,
                        operation="review",
                        payload=journal_payload,
                    )
                )

            except RequestConflictError as exc:

                raise HTTPException(
                    status_code=409,
                    detail="request_id 已被其他请求使用",
                ) from exc

            if not created:
                raise HTTPException(
                    status_code=409,
                    detail="审批请求已被其他流程登记",
                )

            journal_registered = True

        # =====================================
        # 6. 执行审批恢复
        # =====================================

        try:

            if request_id:
                request_journal.start_request(
                    request_id
                )

            # 这里不会生成新的用户消息，
            # 而是恢复 SQLite 中保存的 Graph。
            graph.invoke(
                Command(resume=resume_payload),
                config=config,
            )

            response = make_chat_response(
                graph,
                thread_id,
            )

            # =====================================
            # 7. 保存审批恢复后的结果
            # =====================================

            if response.status == "completed":

                if response.answer is None:
                    raise RuntimeError(
                        "审批恢复后缺少最终回答"
                    )

                if request_id:

                    # 原子保存审批后的最终回答和请求状态。
                    request_journal.complete_request_with_message(
                        request_id,
                        thread_id=thread_id,
                        answer=response.answer,
                        response=response.model_dump(),
                    )

                else:

                    session_store.append_message(
                        thread_id,
                        "assistant",
                        response.answer,
                    )

            elif response.status == "pending_approval":

                # 如果审批恢复后又产生新的审批请求，
                # 不保存最终回答，只保存暂停结果。
                if request_id:

                    request_journal.finish_request(
                        request_id,
                        status="pending_approval",
                        response=response.model_dump(),
                    )

            return response

        except Exception as exc:

            # =================================
            # 9. 标记结果不确定
            # =================================

            if request_id and journal_registered:

                try:

                    record = request_journal.get_request(
                        request_id
                    )

                    if (
                        record is not None
                        and record["status"]
                        in ("accepted", "running")
                    ):
                        request_journal.mark_uncertain(
                            request_id
                        )

                except Exception as journal_exc:

                    logger.error(
                        "Failed to mark review uncertain: %s",
                        type(journal_exc).__name__,
                    )

            logger.error(
                "HTTP review failed: %s",
                type(exc).__name__,
            )

            raise HTTPException(
                status_code=500,
                detail={
                    "code": "REVIEW_EXECUTION_FAILED",
                    "request_id": request_id,
                    "message": (
                        "审批执行结果未能可靠确认。"
                        "请查询线程和请求记录，"
                        "不要自动重复审批。"
                    ),
                },
            ) from exc


# =====================================
# SSE 流式聊天接口
# =====================================


@app.post("/api/v1/chat/stream")
def chat_stream(
    payload: ChatRequest,
    request: Request,
):
    """
    文件：app/api/main.py
    接口：POST /api/v1/chat/stream

    用途：
        提供带 request_id 去重的 SSE 聊天。

    请求原则：
        - 首次请求：登记并执行 Agent。
        - 已完成的重复请求：返回缓存的 completed。
        - 已暂停的重复请求：返回原审批响应。
        - running / uncertain：禁止重新执行。
        - 同 ID 不同请求内容：返回 HTTP 409。

    兼容性：
        不提供 request_id 时保留旧 SSE 行为。
    """

    message = payload.message.strip()
    request_id = payload.request_id

    if not message:
        raise HTTPException(
            status_code=422,
            detail="消息不能为空",
        )

    if request_id and not payload.thread_id:
        raise HTTPException(
            status_code=422,
            detail="使用 request_id 时必须提供 thread_id",
        )

    thread_id = (
        payload.thread_id
        or "research-" + uuid.uuid4().hex[:16]
    )

    graph = request.app.state.graph
    lock = request.app.state.graph_lock

    # =====================================
    # 1. 单进程并发保护
    # =====================================

    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AGENT_BUSY",
                "message": "当前已有 Agent 任务正在执行",
            },
        )

    submitted = False
    registered_new = False

    try:

        reject_deleted_thread(thread_id)

        config = make_config(thread_id)

        # =================================
        # 2. 优先检查重复请求
        # =================================

        if request_id:

            existing = request_journal.get_request(
                request_id
            )

            if existing is not None:

                fingerprint = (
                    request_journal.request_fingerprint(
                        thread_id,
                        "chat",
                        {"message": message},
                    )
                )

                if (
                    existing["thread_id"] != thread_id
                    or existing["operation"] != "chat"
                    or existing["payload_sha256"]
                    != fingerprint
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "REQUEST_ID_CONFLICT",
                            "message": (
                                "request_id 已用于"
                                "其他不同的请求"
                            ),
                        },
                    )

                # 请求已经完成或暂停：
                # 发送保存的结果，不运行 Graph。
                if (
                    existing["status"]
                    in ("completed", "pending_approval")
                    and isinstance(
                        existing["response"], dict
                    )
                ):

                    saved_response = existing["response"]

                    return StreamingResponse(
                        iter([
                            encode_sse(
                                existing["status"],
                                saved_response,
                            )
                        ]),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache, no-transform",
                            "X-Accel-Buffering": "no",
                        },
                    )

                # running、uncertain、accepted，
                # 或者结果数据不完整：
                # 均不允许重复执行。
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "REQUEST_NOT_REPLAYABLE",
                        "request_id": request_id,
                        "status": existing["status"],
                        "message": (
                            "请求已有记录，但不能安全重试。"
                            "请查询线程状态。"
                        ),
                    },
                )

        # =================================
        # 3. 检查 LangGraph 状态
        # =================================

        snapshot = graph.get_state(config)

        if snapshot.interrupts:
            raise HTTPException(
                status_code=409,
                detail="当前线程存在待审批工具调用",
            )

        if snapshot.next:
            raise HTTPException(
                status_code=409,
                detail="当前工作流尚未完成",
            )

        # =================================
        # 4. 确保会话存在
        # =================================

        session_store.ensure_thread(
            thread_id,
            title=message[:50].replace("\n", " "),
        )

        # =================================
        # 5. 登记和开始请求
        # =================================

        if request_id:

            record, created = (
                request_journal.register_request(
                    request_id=request_id,
                    thread_id=thread_id,
                    operation="chat",
                    payload={"message": message},
                )
            )

            if not created:
                raise HTTPException(
                    status_code=409,
                    detail="请求已经被其他执行流程登记",
                )

            registered_new = True

            # accepted → running。
            request_journal.start_request(request_id)

        # =================================
        # 6. 持久化原始用户消息
        # =================================

        session_store.append_message(
            thread_id,
            "user",
            message,
        )

        # =================================
        # 7. 创建 SSE 任务
        # =================================

        job = StreamJob()

        response = StreamingResponse(
            iter_sse_events(job),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

        # =================================
        # 8. 提交后台任务
        # =================================

        request.app.state.sse_executor.submit(
            run_chat_stream,
            graph=graph,
            config=config,
            thread_id=thread_id,
            message=message,
            graph_lock=lock,
            job=job,
            response_builder=make_chat_response,
            request_id=request_id,
        )

        submitted = True

        return response

    except Exception:

        # 只有登记成功但未交给后台线程时，
        # 才由 HTTP 路由尝试标记为 uncertain。
        if request_id and registered_new:

            try:
                record = request_journal.get_request(
                    request_id
                )

                if (
                    record is not None
                    and record["status"]
                    in ("accepted", "running")
                ):
                    request_journal.mark_uncertain(
                        request_id
                    )

            except Exception as exc:

                logger.error(
                    "Failed to mark request uncertain: %s",
                    type(exc).__name__,
                )

        raise

    finally:

        # 已交给后台线程：
        # 由 run_chat_stream() 释放锁。
        #
        # 否则由当前 HTTP 请求释放锁。
        if not submitted:
            lock.release()


# =====================================
# SSE 人工审批与恢复接口
# =====================================


@app.post(
    "/api/v1/threads/{thread_id}/review/stream"
)
def review_thread_stream(
    payload: ReviewRequest,
    request: Request,
    thread_id: str = Path(
        min_length=1,
        max_length=64,
        pattern=THREAD_PATTERN,
    ),
):
    """
    文件：app/api/main.py

    接口：
        POST /api/v1/threads/{thread_id}/review/stream

    用途：
        支持 request_id 幂等控制的 SSE 人工审批。

    首次请求：
        验证当前中断，登记请求，后台恢复 Graph。

    相同请求再次到达：
        已完成或再次暂停时，返回之前保存的结果；
        正在运行或状态不确定时，不重新执行。

    安全要求：
        始终通过 validate_review_request() 验证
        原始工具调用、人工决定和参数指纹。
    """

    graph = request.app.state.graph
    lock = request.app.state.graph_lock

    request_id = payload.request_id

    # 与普通 HTTP /review 使用完全相同的指纹格式。
    journal_payload = {
        "decisions": dict(payload.decisions),
        "expected_fingerprints": dict(
            payload.expected_fingerprints
        ),
    }

    # =====================================
    # 1. 获取单进程执行锁
    # =====================================

    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AGENT_BUSY",
                "message": "当前已有 Agent 任务正在执行",
            },
        )

    submitted = False
    registered_new = False

    try:

        # =================================
        # 2. 确认会话存在
        # =================================

        if session_store.get_thread(thread_id) is None:
            raise HTTPException(
                status_code=404,
                detail="会话不存在",
            )

        # =================================
        # 3. 检查同一个 request_id
        # =================================

        if request_id:

            existing = request_journal.get_request(
                request_id
            )

            if existing is not None:

                expected_sha = (
                    request_journal.request_fingerprint(
                        thread_id,
                        "review",
                        journal_payload,
                    )
                )

                if (
                    existing["thread_id"] != thread_id
                    or existing["operation"] != "review"
                    or existing["payload_sha256"]
                    != expected_sha
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "REQUEST_ID_CONFLICT",
                            "message": (
                                "该 request_id 已用于"
                                "其他不同的请求"
                            ),
                        },
                    )

                # completed 或 pending_approval：
                # 从 Request Journal 返回保存的响应。
                if (
                    existing["status"]
                    in ("completed", "pending_approval")
                    and isinstance(
                        existing["response"], dict
                    )
                ):

                    return StreamingResponse(
                        iter([
                            encode_sse(
                                existing["status"],
                                existing["response"],
                            )
                        ]),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache, no-transform",
                            "X-Accel-Buffering": "no",
                        },
                    )

                # accepted / running / uncertain：
                # 不允许客户端触发第二次 Graph 执行。
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "REQUEST_NOT_REPLAYABLE",
                        "request_id": request_id,
                        "status": existing["status"],
                        "message": (
                            "审批请求已登记，"
                            "当前不能安全地重新执行。"
                        ),
                    },
                )

        # =================================
        # 4. 检查当前 LangGraph 中断
        # =================================

        config = make_config(thread_id)

        snapshot = graph.get_state(config)

        if not snapshot.interrupts:
            raise HTTPException(
                status_code=409,
                detail="当前会话没有待审批操作",
            )

        # =================================
        # 5. 严格验证审批请求
        # =================================

        try:

            resume_payload = validate_review_request(
                snapshot=snapshot,
                decisions=payload.decisions,
                expected_fingerprints=(
                    payload.expected_fingerprints
                ),
            )

        except ApprovalValidationError as exc:

            raise HTTPException(
                status_code=409,
                detail=str(exc),
            ) from exc

        # =================================
        # 6. 登记新的审批请求
        # =================================

        if request_id:

            try:

                record, created = (
                    request_journal.register_request(
                        request_id=request_id,
                        thread_id=thread_id,
                        operation="review",
                        payload=journal_payload,
                    )
                )

            except RequestConflictError as exc:

                raise HTTPException(
                    status_code=409,
                    detail="request_id 已被其他请求使用",
                ) from exc

            if not created:
                raise HTTPException(
                    status_code=409,
                    detail="审批请求已被登记",
                )

            registered_new = True

            # accepted -> running
            request_journal.start_request(request_id)

        # =================================
        # 7. 构建 SSE 响应
        # =================================

        job = StreamJob()

        response = StreamingResponse(
            iter_sse_events(job),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

        # =================================
        # 8. 提交后台审批恢复任务
        # =================================

        request.app.state.sse_executor.submit(
            run_review_stream,
            graph=graph,
            config=config,
            thread_id=thread_id,
            resume_payload=resume_payload,
            graph_lock=lock,
            job=job,
            response_builder=make_chat_response,
            request_id=request_id,
        )

        submitted = True

        return response

    except Exception:

        # 如果登记已成功，但后台任务没有提交，
        # 也不能让下次请求自动重新执行。
        if request_id and registered_new:

            try:

                record = request_journal.get_request(
                    request_id
                )

                if (
                    record is not None
                    and record["status"]
                    in ("accepted", "running")
                ):
                    request_journal.mark_uncertain(
                        request_id
                    )

            except Exception as exc:

                logger.error(
                    "Failed to mark request uncertain: %s",
                    type(exc).__name__,
                )

        raise

    finally:

        # 已提交：
        # 由后台 run_review_stream 释放执行锁。
        #
        # 未提交：
        # 当前 HTTP 线程释放执行锁。
        if not submitted:
            lock.release()


# =====================================
# API 请求状态诊断
# =====================================

@app.get("/api/v1/requests/{request_id}")
def get_api_request_diagnostic(
    request: Request,
    request_id: str = Path(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$",
    ),
):
    """
    查询请求的持久化状态与当前 Graph 状态。

    说明：
        本接口是只读接口。
        不执行 Command(resume=...)。
        不调用模型或工具。
        不自动修改 uncertain 状态。
    """

    record = request_journal.get_request(
        request_id
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="请求不存在",
        )

    reject_deleted_thread(record["thread_id"])

    graph = request.app.state.graph
    lock = request.app.state.graph_lock

    # =====================================
    # 1. 非阻塞取得读取机会
    # =====================================

    # 如果 Agent 正在运行，不等待其执行结束。
    # 此时仍然可以返回持久化 Journal 状态。
    if not lock.acquire(blocking=False):

        return diagnose_request(
            record,
            snapshot=None,
        )

    try:

        snapshot = graph.get_state(
            make_config(record["thread_id"])
        )

        # 重新读取请求状态，减少旧 Journal
        # 状态与新 Checkpoint 状态混用的机会。
        record = request_journal.get_request(
            request_id
        )

        return diagnose_request(
            record,
            snapshot=snapshot,
        )

    except HTTPException:
        raise

    except Exception as exc:

        logger.error(
            "Request diagnosis failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=500,
            detail="请求状态诊断失败",
        ) from exc

    finally:
        lock.release()
