
"""
文件：app/agents/graph.py

用途：
    构建 Sweet Research Agent 的 LangGraph 主工作流。

主要功能：
    1. 使用 DeepSeek 生成回答或工具调用。
    2. 所有工具请求统一经过 Approval Node。
    3. 通过 Execution Guard 校验授权并执行工具。
    4. 支持 SQLite Checkpointer 中断与恢复。
    5. 可选启用对话摘要记忆。

执行流程：
    START
      -> agent
      -> approval（存在工具请求时）
      -> controlled_tools
      -> agent
      -> END（每次调用模型前检查上下文 Token 预算）

安全说明：
    本模块不允许工具调用绕过受控执行节点。
    当前写入工具为模拟工具，不操作真实文件。
"""

import os
import json
import uuid
import hashlib
from pathlib import Path
from functools import partial
from app.observability.tracing import trace_node
from langchain_core.runnables import RunnableConfig

from app.observability.llm_metrics import invoke_with_metrics

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.utils.function_calling import convert_to_openai_tool

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
    SystemMessage,
    RemoveMessage,
)

from langgraph.graph import StateGraph, START, END

from app.agents.state import ResearchAgentState
from app.agents.prompts import SYSTEM_PROMPT, SUMMARY_PROMPT, CITATION_CHECK_PROMPT
from app.rag.citations import current_turn, has_knowledge_call, review_plain_answer, verify_answer
from app.tools.document_analysis import analysis_answer, other_tool_history
from app.memory.context_budget import (
    ContextBudgetError, ContextSettings, get_token_counter, prepare_context,
    tool_messages_complete,
)

from app.workflows.tool_approval import approval_node
from app.tools.executor import (
    EXECUTABLE_TOOLS,
    controlled_tools_node,
)


# =====================================
# 1. DeepSeek 配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")
_initial_context_settings = ContextSettings.from_env()

llm = ChatOpenAI(
    model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com"
    ),
    timeout=60,
    max_retries=1,
    max_tokens=_initial_context_settings.output_tokens,
    temperature=0,
    use_responses_api=False,
    extra_body={
        "thinking": {"type": "disabled"}
    },
)

# 模型可以知道有哪些工具，
# 但工具的实际执行权限由 Approval + Executor 决定。
llm_with_tools = llm.bind_tools(EXECUTABLE_TOOLS)
TOOL_SCHEMAS = [convert_to_openai_tool(tool) for tool in EXECUTABLE_TOOLS]


# =====================================
# 2. Agent Node
# =====================================

def _turn_key(history):
    turn=current_turn(history)
    return (turn[0].id or hashlib.sha256(str(turn[0].content).encode()).hexdigest()) if turn else None


def build_agent_prompt(history, summary):
    """预算统计与实际模型调用使用相同的提示词组装。"""
    messages = [
        SystemMessage(
            content=(
                SYSTEM_PROMPT
                + "\n\n补充安全规则："
                "simulate_save_note 只是模拟保存，"
                "不会真正写入科研知识库。"
                "工具被用户拒绝后，不得声称操作成功。"
                "不要要求用户绕过审批机制。"
            )
        )
    ]

    if summary:
        messages.append(
            SystemMessage(
                content=(
                    "以下是较早对话的摘要，仅用于理解上下文。\n"
                    "涉及私人科研事实时，仍需检索原始资料。\n\n"
                    + summary
                )
            )
        )

    messages.extend(history)

    if has_knowledge_call(history):
        messages.append(SystemMessage(content=(
            "本轮知识库回答必须接受严格原文核验。最终回答只保留用户问到的、原文直接支持的事实，"
            "每段必须以【本轮工具返回的真实 chunk_id】结束。不要添加无引用标题、来源列表、检索统计，"
            "也不要添加'仅检索到一条'、'如果你还需要'、'可以告诉我'等范围判断或对话邀约。"
            "检索到一个片段不能证明整个知识库只有这一条记录。不要补充用户未问的事实。"
            "以上是最终 RAG 事实回答的引用格式，不禁止继续选择工具。结果不足时可进一步检索；"
            "若问题确需跨文档汇总或去重统计，可请求 analyze_document_collection 的人工审批。"
            "工具执行错误和来源版本错误必须明确报告，不能当作资料不存在。集合报告由服务器单独发布。"
        )))
    return messages


def agent_node(state: ResearchAgentState, config: RunnableConfig, *, enable_summarization=False):
    """每次 Agent/工具循环调用前计数，达到阈值才压缩完整旧轮次。"""
    analysis=analysis_answer(state["messages"])
    history=state["messages"]
    turn_key=_turn_key(history)
    replan=state.get("citation_replan") or {}
    if analysis is not None:
        others=other_tool_history(history)
        other_names={call["name"] for message in current_turn(history) if isinstance(message,AIMessage)
            for call in message.tool_calls if call["name"]!="analyze_document_collection"}
        replaces_rag=replan.get("turn_id")==turn_key and other_names<={"search_knowledge_tool"}
        if others is None or replaces_rag:
            answer,report=analysis
            if replaces_rag:
                key="rag_attempt" if replan["report"].get("mode")=="rag" else "source_review"
                report=report|{key:replan["report"]}
            return {"messages":[AIMessage(content=answer)],"citation_report":report}
        history=others
    settings = ContextSettings.from_env()
    counter = get_token_counter()

    def render_prompt(messages,summary):
        prompt=build_agent_prompt(messages,summary)
        if analysis is not None:
            prompt.append(SystemMessage(content="集合分析报告由服务器单独附加。现在只处理本轮其余工具的结果；不要改写或引用集合分析报告。"))
        return prompt

    def summarize(messages):
        if counter.messages(messages) > settings.summary_batch_tokens:
            raise ContextBudgetError("summary_input_exceeds_budget")
        response = invoke_with_metrics(
            model=llm.bind(max_tokens=settings.summary_tokens), messages=messages,
            config=config, call_kind="summary",
        )
        if response.response_metadata.get("finish_reason") != "stop" or response.tool_calls:
            raise ContextBudgetError("summary_incomplete")
        return response.content

    prepared = prepare_context(
        history, state.get("summary", ""),
        render_prompt=render_prompt, tools=TOOL_SCHEMAS, settings=settings,
        counter=counter, summarize=summarize if enable_summarization else None,
        summary_prompt=SUMMARY_PROMPT + f"\n摘要必须在 {settings.summary_tokens} Token 内完整结束，"
                       "尽量使用预算的四分之三，以便完整保留末尾的未完成事项。",
    )
    messages = render_prompt(prepared.messages, prepared.summary)
    print(f"[Context] estimated_input_tokens={prepared.after_tokens} trigger_tokens={settings.trigger_tokens} "
          f"removed_messages={len(prepared.removed_ids)} summary_calls={prepared.summary_calls} "
          f"target_reached={prepared.target_reached}")

    response = invoke_with_metrics(
        model=llm_with_tools, messages=messages, config=config, call_kind="agent",
    )

    print(
        "[Agent] 请求工具数量：",
        len(response.tool_calls)
    )

    removals = [RemoveMessage(id=identifier) for identifier in prepared.removed_ids]
    update = {"messages": removals + [response], "citation_report": None}
    if removals:
        update["summary"] = prepared.summary
    if response.tool_calls:
        return update

    def judge(payload):
        judgment_messages = [SystemMessage(content=CITATION_CHECK_PROMPT),
                             HumanMessage(content=json.dumps(payload, ensure_ascii=False))]
        if counter.messages(judgment_messages) >= settings.trigger_tokens:
            raise ContextBudgetError("citation_check_input_exceeds_budget")
        return invoke_with_metrics(
            model=llm, messages=judgment_messages,
            config=config,
            call_kind="citation_check",
        ).content

    answer, report = verify_answer(prepared.messages, response.content, judge=judge)
    if (analysis is None and report["reason"]=="private_lookup_required" and report.get("task_type")=="private_lookup"
            and not has_knowledge_call(prepared.messages) and replan.get("turn_id")!=turn_key):
        # 来源依赖检查已识别为普通私人查询：无需路径也可发起受控 RAG。
        # 这是任务类型兜底；不按主题猜目录，不提升为全文读取，不直接执行工具。
        question=current_turn(prepared.messages)[0].content
        update["messages"][-1]=AIMessage(id=response.id,content="",tool_calls=[{
            "id":"lookup-"+uuid.uuid4().hex,"name":"search_knowledge_tool",
            "args":{"question":question,"top_k":3},"type":"tool_call"}])
        update["citation_replan"]={"turn_id":turn_key,"report":report,"lookup_fallback":True}
        return update
    replanned=False
    if (analysis is None and report["status"]=="rejected"
            and report["reason"] in {"no_evidence","unsupported_claim","missing_citation","private_lookup_required","no_current_evidence"}
            and replan.get("turn_id")!=turn_key):
        replanning_messages=messages+[SystemMessage(content=(
            "本轮草稿尚不能发布，核验原因："+report["reason"]+"。这不代表整个知识库没有资料。"
            "现在按任务性质重新判断一次：私人资料的精确事实或片段使用 search_knowledge_tool；"
            "跨文档汇总、统计、比较或事件整理可请求 analyze_document_collection，按所需证据结构选现有模板。"
            "普通 RAG 无需用户事先提供文件路径。完整集合分析先复用本轮及同会话已明确的相关范围，"
            "仅缺少必要集合范围时澄清；保留日期、目录、文件和排除条件，不擅自扩大。"
            "不得编造私人事实、照抄旧拒答或自行声称整个知识库没有资料。所有工具仍须经过 Approval 和 Execution Guard，"
            "集合全文云端分析还需独立授权和预算。若仍不足，明确说明当前无法确认，不重复旧拒答或引用旧来源。"
        ))]
        if counter.messages(replanning_messages,TOOL_SCHEMAS)<settings.trigger_tokens:
            update["citation_replan"]={"turn_id":turn_key,"report":report}
            replanned=True
            response=invoke_with_metrics(model=llm_with_tools,messages=replanning_messages,config=config,call_kind="agent")
            update["messages"][-1]=response
            if response.tool_calls: return update
            answer,report=verify_answer(prepared.messages,response.content,judge=judge)
        else:
            report=report|{"replan_skipped":"context_budget_exceeded"}
    if (analysis is None and (replanned or replan.get("turn_id")==turn_key)
            and has_knowledge_call(prepared.messages) and report["status"]=="rejected"
            and report["reason"] in {"no_evidence","unsupported_claim","missing_citation"}):
        clarification,plain=review_plain_answer(prepared.messages,response.content,judge=judge)
        # 仅允许经过独立分类的澄清，不能把 RAG 草稿改标普通回答来绕过来源核验。
        if plain.get("source_dependency")=="clarify_scope":
            answer,report=clarification,plain|{"reason":"scope_clarification","rag_attempt":report}
    if analysis is not None:
        answer+="\n\n"+analysis[0]
        report=report | {"mode":"rag_and_document_analysis","document_analysis":analysis[1]}
    update["messages"][-1] = response.model_copy(update={"content": answer})
    update["citation_report"] = report
    return update


# =====================================
# 5. 构建正式 Graph
# =====================================

def build_graph(
    checkpointer=None,
    enable_summarization=False
):
    """
    创建 Sweet Research Agent。

    Args:
        checkpointer:
            LangGraph 状态存储。
            启用人工审批时必须提供 Checkpointer。

        enable_summarization:
            是否启用历史对话自动摘要。

    Returns:
        编译后的 LangGraph。
    """

    builder = StateGraph(ResearchAgentState)
    settings = ContextSettings.from_env()
    get_token_counter()  # 启动时校验本地词表，缺失时明确失败，不降级为字符数。
    print(f"[Context] configured trigger_tokens={settings.trigger_tokens} target_tokens={settings.target_tokens} "
          f"output_tokens={settings.output_tokens}")

    # ---------------------------------
    # 注册节点
    # ---------------------------------

    builder.add_node(
        "agent",
        trace_node(
            "agent",
            partial(agent_node, enable_summarization=enable_summarization),
            accepts_config=True,
        )
    )

    builder.add_node(
        "approval",
        trace_node(
            "approval",
            approval_node,
            accepts_config=True,
        )
    )

    builder.add_node(
        "controlled_tools",
        trace_node(
            "controlled_tools",
            controlled_tools_node,
            accepts_config=True,
        )
    )

    # ---------------------------------
    # 固定执行关系
    # ---------------------------------

    builder.add_edge(START, "agent")

    # 审批结束后，必须经过执行守卫。
    # 不能直接把工具请求交给普通 ToolNode。
    builder.add_edge(
        "approval",
        "controlled_tools"
    )

    # 工具执行完成后，再次交给 DeepSeek。
    builder.add_edge(
        "controlled_tools",
        "agent"
    )

    # ---------------------------------
    # Agent 的条件路由
    # ---------------------------------

    def route_after_agent(state: ResearchAgentState):
        """
        决定 Agent 下一步执行什么。

        优先级：
            1. 有工具调用 -> approval
            2. 否则 -> END；上下文预算已在模型调用前处理
        """

        last = state["messages"][-1]

        if isinstance(last, AIMessage) and last.tool_calls:
            return "approval"

        return "end"

    routes = {
        "approval": "approval",
        "end": END,
    }

    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        routes
    )

    return builder.compile(
        checkpointer=checkpointer
    )
