
"""
文件：app/api/streaming.py

用途：
    将正式 LangGraph Agent 的执行事件转换为 SSE。

主要功能：
    1. 接收 LangGraph 消息与节点更新。
    2. 将文本片段、节点状态、审批结果转换为 SSE。
    3. 在独立工作线程中完成 Agent 执行。
    4. SSE 客户端断开后，仍继续执行和持久化结果。

设计说明：
    当前面向单进程、单任务执行的开发环境。
    不修改 LangGraph 节点和 Execution Guard。

    SSE 事件不是永久消息队列。
    客户端断开后应通过会话 API 查询最终结果。
"""

import json
import logging
import queue
import threading

from app.api import request_journal
from dataclasses import dataclass, field
from typing import Callable
from langgraph.types import Command

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
)

from app.api import session_store


logger = logging.getLogger(__name__)





# =====================================
# 1. 一次 SSE 请求的事件队列
# =====================================

@dataclass
class StreamJob:
    """保存本次 HTTP 流式请求的临时事件。"""

    events: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=2048)
    )

    detached: threading.Event = field(
        default_factory=threading.Event
    )

    def emit(self, event: str, data: dict):
        """
        向 SSE 消费端发送事件。

        不允许慢客户端反向阻塞 Agent。
        队列满时淘汰旧事件，最终结果仍以
        SQLite 中持久化的会话为准。
        """

        if self.detached.is_set():
            return

        item = (event, data)

        try:
            self.events.put_nowait(item)

        except queue.Full:

            try:
                self.events.get_nowait()

            except queue.Empty:
                pass

            try:
                self.events.put_nowait(item)

            except queue.Full:
                pass


# =====================================
# 2. SSE 编码
# =====================================

def encode_sse(event: str, data: dict) -> str:
    """将事件转换为标准 text/event-stream 格式。"""

    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return (
        f"event: {event}\n"
        f"data: {payload}\n\n"
    )


# =====================================
# 3. HTTP 输出事件生成器
# =====================================

def iter_sse_events(job: StreamJob):
    """
    持续读取事件队列并向客户端发送。

    客户端断开只会让该消费者停止。
    不会调用 LangGraph 的取消函数。
    """

    try:

        while True:

            try:
                event, data = job.events.get(
                    timeout=10
                )

            except queue.Empty:
                # 心跳不包含敏感信息。
                yield ": keep-alive\n\n"
                continue

            if event == "__end__":
                break

            yield encode_sse(event, data)

    finally:
        # 关闭事件消费端，但不终止后台执行。
        job.detached.set()


# =====================================
# 4. 处理 LangGraph 流式事件
# =====================================

def forward_graph_event(job: StreamJob, item):
    """
    将 LangGraph 的 messages / updates 转换为
    前端可理解的有限事件类型。

    不直接把整个 Graph State 发送给客户端。
    """

    mode, data = item

    if mode == "messages":
        # 模型可能先输出事实再决定调用工具，核验模型也位于 agent 节点内。
        # 因此任何原始 token 都必须等 agent 完成核验后才能展示。
        return

    elif mode == "updates":

        if not isinstance(data, dict):
            return

        allowed_nodes = {
            "agent",
            "approval",
            "controlled_tools",
            "summarize",
        }

        for node_name, update in data.items():

            if node_name not in allowed_nodes:
                continue

            # 只展示节点名称，不返回完整节点 State。
            job.emit(
                "node",
                {"name": node_name}
            )

            # 如果这一轮模型选择了调用工具，
            # 前端应清除本次临时生成的文本。
            if (
                node_name == "agent"
                and isinstance(update, dict)
            ):

                messages = update.get(
                    "messages", []
                )

                report = update.get("citation_report")
                if isinstance(report, dict) and report.get("status") in {
                    "verified", "rejected", "not_applicable",
                }:
                    # 草稿及核验模型的 JSON 均不能提前显示给客户端。
                    job.emit("draft_reset", {"reason": "citation_check"})
                    for message in messages:
                        if isinstance(message, AIMessage) and not message.tool_calls:
                            job.emit("token", {"text": message.content})

                for message in messages:

                    if (
                        isinstance(message, AIMessage)
                        and message.tool_calls
                    ):
                        job.emit(
                            "draft_reset",
                            {"reason": "tool_calls"}
                        )
                        break


# =====================================
# 5. 在后台执行完整 Agent
# =====================================


def run_chat_stream(
    *,
    graph,
    config: dict,
    thread_id: str,
    message: str,
    graph_lock: threading.Lock,
    job: StreamJob,
    response_builder: Callable,
    request_id: str | None = None,
):
    """
    文件：app/api/streaming.py
    函数：run_chat_stream

    用途：
        后台执行 SSE 聊天，记录请求最终状态。

    关键原则：
        1. HTTP 断线不取消已启动的 Agent。
        2. completed 之前先保存助手消息和请求结果。
        3. pending_approval 同样保存到 Request Journal。
        4. 执行异常时标记 uncertain，不自动重试。
        5. 后台任务结束时释放全局 Graph 锁。
    """

    try:
        job.emit(
            "started",
            {
                "thread_id": thread_id,
                "status": "running",
                "request_id": request_id,
            },
        )

        # =====================================
        # 1. 执行 LangGraph 并转发流式事件
        # =====================================

        for item in graph.stream(
            {
                "messages": [
                    HumanMessage(content=message)
                ]
            },
            config=config,
            stream_mode=["messages", "updates"],
        ):
            forward_graph_event(job, item)

        # =====================================
        # 2. 读取 Graph 的最终状态
        # =====================================

        response = response_builder(
            graph,
            thread_id,
        )

        # =====================================
        # 3. 人工审批暂停
        # =====================================

        if response.status == "pending_approval":

            if request_id:
                request_journal.finish_request(
                    request_id,
                    status="pending_approval",
                    response=response.model_dump(),
                )

            job.emit(
                "pending_approval",
                response.model_dump(),
            )

        # =====================================
        # 4. 正常完成
        # =====================================

        elif response.status == "completed":

            if response.answer is None:
                raise RuntimeError(
                    "已完成的请求缺少最终回答"
                )

            if request_id:

                # 消息和请求结果在同一个事务内提交。
                request_journal.complete_request_with_message(
                    request_id,
                    thread_id=thread_id,
                    answer=response.answer,
                    response=response.model_dump(),
                )

            else:

                # 兼容没有 request_id 的旧 SSE 请求。
                session_store.append_message(
                    thread_id,
                    "assistant",
                    response.answer,
                )

            # 只有数据库提交成功，
            # 才向 SSE 客户端发送 completed。
            job.emit(
                "completed",
                response.model_dump(),
            )


        else:
            raise RuntimeError(
                "未知的 Agent 工作流状态"
            )

    except Exception as exc:

        logger.error(
            "SSE chat failed: %s",
            type(exc).__name__,
        )

        # =====================================
        # 5. 异常时禁止盲目重新执行
        # =====================================

        if request_id:

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
                    "Failed to mark SSE request uncertain: %s",
                    type(journal_exc).__name__,
                )

        job.emit(
            "error",
            {
                "thread_id": thread_id,
                "request_id": request_id,
                "code": "AGENT_EXECUTION_FAILED",
                "message": (
                    "Agent 执行状态无法确认，"
                    "请查询请求记录和会话状态，"
                    "不要自动重新提交"
                ),
            },
        )

    finally:

        # 后台任务是锁的最终释放者。
        graph_lock.release()

        job.emit("__end__", {})



def run_review_stream(
    *,
    graph,
    config: dict,
    thread_id: str,
    resume_payload: dict,
    graph_lock: threading.Lock,
    job: StreamJob,
    response_builder: Callable,
    request_id: str | None = None,
):
    """
    文件：app/api/streaming.py

    用途：
        带 request_id 幂等记录的 SSE 审批恢复。

    工作流程：
        1. 通过 Command(resume=...) 恢复 Graph。
        2. 实时转发 LangGraph 事件。
        3. 根据 Checkpoint 确定最终状态。
        4. 保存最终回答和请求结果。
        5. 完成后释放全局执行锁。

    重要：
        HTTP 路由必须先验证审批决定并登记请求。
        此函数不直接接受客户端传入的工具参数。

        SSE 连接断开，不会主动取消 Graph。

        如果已经开始恢复，但后续步骤出现异常，
        将请求标记为 uncertain，禁止自动重试。
    """

    try:
        # =====================================
        # 1. 通知客户端开始恢复
        # =====================================

        job.emit(
            "started",
            {
                "thread_id": thread_id,
                "request_id": request_id,
                "status": "running",
                "source": "review",
            },
        )

        # =====================================
        # 2. 恢复已有 LangGraph 工作流
        # =====================================

        for item in graph.stream(
            Command(resume=resume_payload),
            config=config,
            stream_mode=["messages", "updates"],
        ):
            forward_graph_event(job, item)

        # =====================================
        # 3. 读取实际执行结果
        # =====================================

        response = response_builder(
            graph,
            thread_id,
        )

        # =====================================
        # 4. 可能再次触发人工审批
        # =====================================

        if response.status == "pending_approval":

            if request_id:
                request_journal.finish_request(
                    request_id,
                    status="pending_approval",
                    response=response.model_dump(),
                )

            job.emit(
                "pending_approval",
                response.model_dump(),
            )

        # =====================================
        # 5. Agent 正常完成
        # =====================================

        elif response.status == "completed":

            if response.answer is None:
                raise RuntimeError(
                    "审批恢复成功，但缺少最终回答"
                )

            if request_id:

                # 原子保存 SSE 审批恢复后的回答和请求结果。
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

            job.emit(
                "completed",
                response.model_dump(),
            )

        else:
            raise RuntimeError(
                "审批恢复后出现未知状态"
            )

    except Exception as exc:

        logger.error(
            "SSE review failed: %s",
            type(exc).__name__,
        )

        # =====================================
        # 6. 发生异常时阻止自动重试
        # =====================================

        if request_id:

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

        job.emit(
            "error",
            {
                "thread_id": thread_id,
                "request_id": request_id,
                "code": "REVIEW_EXECUTION_FAILED",
                "message": (
                    "审批恢复结果无法确认。"
                    "请查询会话和请求状态，"
                    "不要自动重新提交。"
                ),
            },
        )

    finally:

        # 审批执行完成后才释放锁。
        graph_lock.release()

        # 结束 SSE 事件流。
        job.emit("__end__", {})
