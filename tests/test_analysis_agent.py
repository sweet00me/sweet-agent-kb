import json
import uuid
import socket
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage,HumanMessage,ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents import graph as graph_module
from app.agents.tool_policy import get_tool_policy
from app.api import main,session_store,request_journal,streaming
from app.memory import checkpoint
from app.observability import audit,llm_metrics,tracing
from app.tools import executor,document_analysis as tool_module
from app.tools.document_analysis import analyze_document_collection,analysis_answer
from app.document_analysis.workflow import AnalysisWorkflow
from app.document_analysis.synthesis_store import SynthesisStore
from tests.test_analysis_workflow import Extractor
from tests.test_evidence_verification import Validator
from tests.synthesis_support import SynthesisFixture
from tests.test_citation_api import CitationApiFixture

_SOCKET_CONNECT=socket.socket.connect

class Actor:
    def __init__(self,scope_query): self.scope_query=scope_query; self.calls=0
    def invoke(self,messages):
        self.calls+=1
        if self.calls>1: raise AssertionError("analysis report must not be rewritten by a cloud actor")
        return AIMessage(content="",tool_calls=[{"id":"analyze-one","name":"analyze_document_collection",
            "args":{"question":"整理事件","scope_query":self.scope_query,"template_id":"people_events"},"type":"tool_call"}])


class AnalysisAgentTests(SynthesisFixture):
    def setUp(self):
        super().setUp(); self.write(self.source,"张三于2024-01-01测试。"); self.sync()
        self.extractor=Extractor(); self.store=SynthesisStore(self.evidence)
        self.workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator())
        self.actor=Actor(f"分析 `{self.source}` 文档")
        directory=self.root.parent
        for module in (audit,llm_metrics,tracing): self.enterContext(patch.object(module,"AUDIT_DB_PATH",directory/"audit.sqlite3"))
        self.enterContext(patch.object(checkpoint,"DB_PATH",directory/"checkpoint.sqlite3"))
        self.enterContext(patch.object(checkpoint,"STORAGE_ROOT",self.root))
        self.enterContext(patch.object(session_store,"SESSION_DB_PATH",directory/"sessions.sqlite3"))
        self.enterContext(patch.object(session_store,"STORAGE_ROOT",self.root))
        self.enterContext(patch.object(session_store.os.path,"ismount",return_value=True))
        self.enterContext(patch.dict("os.environ",{"RAG_AUTO_SYNC_ENABLED":"0"}))
        self.enterContext(patch.object(tool_module,"default_analysis_service",return_value=self.workflow))
        self.enterContext(patch.object(graph_module,"llm_with_tools",self.actor))

    def test_registered_tool_needs_approval_and_does_not_expose_cloud_or_permission_arguments(self):
        self.assertIn("analyze_document_collection",executor.TOOL_NAMES)
        self.assertEqual(get_tool_policy("analyze_document_collection"),"approve")
        schema=convert_to_openai_tool(analyze_document_collection)["function"]["parameters"]["properties"]
        self.assertNotIn("config",schema); self.assertNotIn("tool_call_id",schema)
        self.assertNotIn("cloud_authorized",schema)
        raw=analyze_document_collection.invoke({"name":"analyze_document_collection","id":"unapproved","type":"tool_call",
            "args":{"question":"整理","scope_query":self.actor.scope_query,"template_id":"people_events"}})
        self.assertEqual(json.loads(raw.content)["status"],"blocked")
        self.assertFalse(self.extractor.calls)

    def test_graph_approval_resume_preserves_guard_trace_checkpoint_and_evidence_ids(self):
        graph=graph_module.build_graph(checkpointer=InMemorySaver())
        config={"configurable":{"thread_id":"analysis-graph"}}
        graph.invoke({"messages":[HumanMessage(content="完整分析全部文档")]},config)
        self.assertTrue(graph.get_state(config).tasks)
        self.assertFalse(self.extractor.calls)
        graph.invoke(Command(resume={"decisions":{"analyze-one":"approve"}}),config)
        state=graph.get_state(config).values
        self.assertEqual(state["citation_report"]["mode"],"document_analysis")
        self.assertEqual(state["citation_report"]["analysis_status"],"completed")
        self.assertIn("[1]",state["messages"][-1].content)
        self.assertTrue(state["citation_report"]["sources"])
        self.assertNotIn("【Evidence:",state["messages"][-1].content)
        self.assertEqual(self.actor.calls,1)
        self.assertEqual(state["approval_records"],{})
        nodes={item["node_name"] for item in tracing.list_node_traces("analysis-graph")}
        self.assertIn("analysis_map",nodes)

    def test_rejection_never_calls_workflow_or_claims_completed_analysis(self):
        graph=graph_module.build_graph(checkpointer=InMemorySaver()); config={"configurable":{"thread_id":"analysis-reject"}}
        graph.invoke({"messages":[HumanMessage(content="完整分析")]},config)
        graph.invoke(Command(resume={"decisions":{"analyze-one":"reject"}}),config)
        state=graph.get_state(config).values
        self.assertFalse(self.extractor.calls)
        self.assertIn("未执行",state["messages"][-1].content)
        self.assertNotIn("【Evidence:",state["messages"][-1].content)

    def test_partial_report_does_not_publish_semantically_unchecked_fact_text(self):
        self.workflow.validator=None
        graph=graph_module.build_graph(checkpointer=InMemorySaver()); config={"configurable":{"thread_id":"analysis-partial"}}
        graph.invoke({"messages":[HumanMessage(content="完整分析")]},config)
        graph.invoke(Command(resume={"decisions":{"analyze-one":"approve"}}),config)
        state=graph.get_state(config).values
        self.assertEqual(state["citation_report"]["analysis_status"],"partial")
        self.assertNotIn("张三于2024-01-01测试",state["messages"][-1].content)

    def test_changed_approval_arguments_cannot_reach_new_tool(self):
        from app.workflows.tool_approval import tool_fingerprint
        call={"id":"tampered","name":"analyze_document_collection","args":{"question":"整理","scope_query":self.actor.scope_query}}
        record={"name":call["name"],"fingerprint":tool_fingerprint(call),"decision":"approve"}
        call["args"]["scope_query"]="读取 Diary/24.7.16.md"
        allowed,_=executor.check_execution_permission(call,{"tampered":record})
        self.assertFalse(allowed); self.assertFalse(self.extractor.calls)

    def test_old_tool_results_or_unmatched_call_ids_cannot_be_reused_for_a_new_turn(self):
        call=AIMessage(content="",tool_calls=[{"name":"analyze_document_collection","args":{},"id":"old"}])
        tool=ToolMessage(content=json.dumps({"kind":"document_analysis","status":"completed","answer":"OLD"}),name="analyze_document_collection",tool_call_id="different")
        self.assertIsNone(analysis_answer([HumanMessage(content="旧"),call,tool]))
        self.assertIsNone(analysis_answer([HumanMessage(content="旧"),call,tool,HumanMessage(content="新问题")]))

    def test_two_analysis_tool_results_do_not_silently_drop_a_failed_second_job(self):
        call=AIMessage(content="",tool_calls=[{"name":"analyze_document_collection","args":{},"id":key} for key in ("one","two")])
        first=ToolMessage(content=json.dumps({"kind":"document_analysis","status":"completed","answer":"FIRST","analysis_id":"first"}),
                          name="analyze_document_collection",tool_call_id="one")
        second=ToolMessage(content=json.dumps({"kind":"document_analysis","status":"failed","answer":"SECOND","analysis_id":"second"}),
                           name="analyze_document_collection",tool_call_id="two")
        answer,report=analysis_answer([HumanMessage(content="两个范围"),call,first,second])
        self.assertIn("FIRST",answer); self.assertIn("SECOND",answer)
        self.assertEqual(report["analysis_status"],"partial")

    def test_real_http_sse_review_and_request_replay_share_persisted_analysis_report(self):
        client=self.enterContext(TestClient(main.app)); thread="analysis-api"; request="req-"+uuid.uuid4().hex
        pending=client.post("/api/v1/chat/stream",json={"thread_id":thread,"request_id":request,"message":"完整分析文档"})
        self.assertEqual(pending.status_code,200,pending.text)
        initial=next(value for name,value in CitationApiFixture.events(pending.text) if name=="pending_approval")
        call=initial["pending_approvals"][0]
        payload={"decisions":{call["tool_call_id"]:"approve"},"expected_fingerprints":{call["tool_call_id"]:call["fingerprint"]},"request_id":"req-"+uuid.uuid4().hex}
        path=f"/api/v1/threads/{thread}/review/stream"
        reviewed=client.post(path,json=payload); self.assertEqual(reviewed.status_code,200,reviewed.text)
        events=CitationApiFixture.events(reviewed.text)
        result=next(value for name,value in events if name=="completed")
        self.assertEqual(result["citation_report"]["mode"],"document_analysis")
        self.assertIn("[1]",result["answer"])
        self.assertTrue(result["citation_report"]["sources"])
        self.assertTrue(any(name=="token" for name,value in events))
        self.assertEqual(session_store.list_messages(thread)["items"][-1]["content"],result["answer"])
        self.assertEqual(request_journal.get_request(payload["request_id"])["response"]["answer"],result["answer"])
        replay=client.post(path,json=payload)
        repeated=next(value for name,value in CitationApiFixture.events(replay.text) if name=="completed")
        self.assertEqual(repeated["answer"],result["answer"])
        self.assertEqual(len(self.extractor.calls),1)
        with checkpoint.open_checkpointer() as saver:
            reopened=graph_module.build_graph(checkpointer=saver)
            state=reopened.get_state({"configurable":{"thread_id":thread}}).values
            self.assertEqual(state["citation_report"]["mode"],"document_analysis")

    def begin_review(self,client,thread):
        pending=client.post("/api/v1/chat",json={"thread_id":thread,"message":"完整分析文档"}).json()
        self.assertEqual(pending["status"],"pending_approval")
        call=pending["pending_approvals"][0]
        return {"decisions":{call["tool_call_id"]:"approve"},"expected_fingerprints":{call["tool_call_id"]:call["fingerprint"]},
                "request_id":"req-"+uuid.uuid4().hex}

    def test_disabled_cloud_review_is_a_confirmed_tool_error_not_a_pending_approval(self):
        from app.document_analysis.evidence_schema import ExtractorIdentity
        self.extractor.identity=ExtractorIdentity(name="mock-cloud",version="1",provider="deepseek")
        self.extractor.authorized=True; self.workflow.cloud_authorized=True
        with patch.dict("os.environ",{"DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM":"0","DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"0",
                                       "DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"0"}):
            client=self.enterContext(TestClient(main.app)); thread="cloud-disabled-review"
            payload=self.begin_review(client,thread)
            response=client.post(f"/api/v1/threads/{thread}/review/stream",json=payload)
        result=next(value for name,value in CitationApiFixture.events(response.text) if name=="completed")
        state=client.app.state.graph.get_state(main.make_config(thread))
        message=next(message for message in reversed(state.values["messages"]) if isinstance(message,ToolMessage))
        self.assertEqual(json.loads(message.content)["error_code"],"workflow_cloud_not_authorized")
        self.assertEqual(message.status,"error")
        self.assertFalse(state.interrupts); self.assertFalse(state.next)
        self.assertEqual(result["pending_approvals"],[])
        self.assertIn("云端分析未获独立授权",result["answer"])
        self.assertNotIn("证据不足",result["answer"])
        report=result["citation_report"]
        self.assertEqual(report["analysis_status"],"blocked")
        self.assertEqual(report["analysis_error_code"],"workflow_cloud_not_authorized")
        self.assertIsInstance(report["reason"],str)
        self.assertEqual((report["claim_count"],report["citations"]),(0,[]))
        parsed=subprocess.run(["node","--input-type=module","-e", """
            import { runAgentStream } from './tests/load-services.mjs';
            let body=''; for await (const chunk of process.stdin) body+=chunk;
            const input=JSON.parse(body);
            globalThis.fetch=async()=>new Response(input.stream,{headers:{'Content-Type':'text/event-stream'}});
            const result=await runAgentStream('/review/stream',{},input.thread,()=>{});
            if(result.citation_report.analysis_status!=='blocked') throw Error('lost analysis failure');
        """],input=json.dumps({"stream":response.text,"thread":thread}),cwd=Path(__file__).resolve().parents[1]/"frontend",
            text=True,capture_output=True,timeout=15)
        self.assertEqual(parsed.returncode,0,parsed.stderr[-2000:])
        self.assertEqual(request_journal.get_request(payload["request_id"])["status"],"completed")
        diagnostic=client.get("/api/v1/requests/"+payload["request_id"]).json()
        self.assertEqual(diagnostic["response"],result)
        self.assertFalse(self.extractor.calls)
        replay=client.post(f"/api/v1/threads/{thread}/review/stream",json=payload)
        self.assertEqual(next(value for name,value in CitationApiFixture.events(replay.text) if name=="completed"),result)
        stale=client.post(f"/api/v1/threads/{thread}/review",json={**payload,"request_id":"req-"+uuid.uuid4().hex})
        self.assertEqual(stale.status_code,409)

    def test_failed_analysis_is_reported_without_model_rewrite_or_private_error_details(self):
        client=self.enterContext(TestClient(main.app)); thread="failed-analysis-review"
        payload=self.begin_review(client,thread)
        with patch.object(self.workflow,"run",side_effect=RuntimeError("PRIVATE_FAILURE")):
            response=client.post(f"/api/v1/threads/{thread}/review/stream",json=payload)
        result=next(value for name,value in CitationApiFixture.events(response.text) if name=="completed")
        state=client.app.state.graph.get_state(main.make_config(thread))
        message=next(message for message in reversed(state.values["messages"]) if isinstance(message,ToolMessage))
        self.assertEqual(message.status,"error")
        self.assertEqual(result["citation_report"]["analysis_error_code"],"analysis_tool_failed")
        self.assertNotIn("PRIVATE_FAILURE",response.text)
        self.assertNotIn("证据不足",result["answer"])
        self.assertEqual(self.actor.calls,1)

    def test_review_disconnect_recovers_saved_result_without_executing_the_tool_again(self):
        import httpx
        import uvicorn
        def local_connect(instance,address):
            if instance.family!=socket.AF_UNIX and (not isinstance(address,tuple) or address[0] not in {"127.0.0.1","::1"}):
                raise AssertionError("offline review test blocked external network")
            return _SOCKET_CONNECT(instance,address)
        self.enterContext(patch.object(socket.socket,"connect",new=local_connect))
        client=self.enterContext(TestClient(main.app)); thread="disconnect-analysis-review"
        payload=self.begin_review(client,thread)
        gate,entered=threading.Event(),threading.Event(); executions=[]; original=self.workflow.run
        def delayed(**kwargs):
            executions.append(thread); entered.set()
            if not gate.wait(5): raise TimeoutError("test gate not released")
            return original(**kwargs)
        listener=socket.socket(); listener.bind(("127.0.0.1",0)); listener.listen(16)
        port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(main.app,lifespan="off",access_log=False,log_level="critical"))
        worker=threading.Thread(target=server.run,kwargs={"sockets":[listener]},daemon=True)
        worker.start()
        try:
            deadline=time.monotonic()+5
            while not server.started and time.monotonic()<deadline: time.sleep(0.01)
            self.assertTrue(server.started)
            with patch.object(self.workflow,"run",side_effect=delayed),httpx.Client(base_url=f"http://127.0.0.1:{port}",timeout=5,trust_env=False) as remote:
                path=f"/api/v1/threads/{thread}/review/stream"
                with remote.stream("POST",path,json=payload) as response:
                    for line in response.iter_lines():
                        if line=="event: started": break
                    self.assertTrue(entered.wait(2))
                    running=remote.get("/api/v1/requests/"+payload["request_id"]).json()
                    self.assertEqual(running["journal_status"],"running")
                    self.assertIsNone(running["response"])
                gate.set()
                deadline=time.monotonic()+5
                while request_journal.get_request(payload["request_id"])["status"]!="completed" and time.monotonic()<deadline: time.sleep(0.01)
                diagnostic=remote.get("/api/v1/requests/"+payload["request_id"]).json()
                self.assertEqual(diagnostic["journal_status"],"completed",diagnostic)
                self.assertEqual(diagnostic["response"]["citation_report"]["analysis_status"],"completed")
                self.assertFalse(diagnostic["automatic_replay_allowed"])
                self.assertEqual(remote.get(f"/api/v1/threads/{thread}/status").json()["pending_approvals"],[])
                replay=remote.post(path,json=payload)
                repeated=next(value for name,value in CitationApiFixture.events(replay.text) if name=="completed")
                self.assertEqual(repeated,diagnostic["response"])
                self.assertEqual(executions,[thread])
                self.assertEqual(len(session_store.list_messages(thread)["items"]),2)
        finally:
            gate.set(); server.should_exit=True; worker.join(5); listener.close()

    def test_uncommitted_review_is_uncertain_even_when_checkpoint_is_idle(self):
        client=self.enterContext(TestClient(main.app)); thread="uncertain-analysis-review"
        payload=self.begin_review(client,thread)
        with patch.object(streaming.request_journal,"complete_request_with_message",side_effect=RuntimeError("PRIVATE_COMMIT_ERROR")):
            response=client.post(f"/api/v1/threads/{thread}/review/stream",json=payload)
        events=CitationApiFixture.events(response.text)
        self.assertTrue(any(name=="error" for name,value in events))
        self.assertFalse(any(name=="completed" for name,value in events))
        diagnostic=client.get("/api/v1/requests/"+payload["request_id"]).json()
        self.assertEqual((diagnostic["journal_status"],diagnostic["checkpoint_status"]),("uncertain","idle"))
        self.assertIsNone(diagnostic["response"])
        self.assertEqual(client.post(f"/api/v1/threads/{thread}/review/stream",json=payload).status_code,409)
        self.assertNotIn("PRIVATE_COMMIT_ERROR",response.text)

    def test_invalid_tool_arguments_end_as_explicit_failure_without_model_rewrite(self):
        original=self.actor.invoke
        def invalid(messages):
            result=original(messages)
            result.tool_calls[0]["args"]["question"]=42
            return result
        with patch.object(self.actor,"invoke",side_effect=invalid):
            client=self.enterContext(TestClient(main.app)); thread="invalid-analysis-arguments"
            payload=self.begin_review(client,thread)
            response=client.post(f"/api/v1/threads/{thread}/review/stream",json=payload)
        events=CitationApiFixture.events(response.text)
        self.assertTrue(any(name=="completed" for name,value in events),"工具参数错误应返回明确失败，不应变成审批状态未知。")
        result=next(value for name,value in events if name=="completed")
        self.assertEqual(result["citation_report"]["analysis_status"],"failed")
        self.assertEqual(result["citation_report"]["analysis_error_code"],"analysis_tool_failed")
        self.assertNotIn("证据不足",result["answer"])
        self.assertEqual(self.actor.calls,1)
        self.assertFalse(self.extractor.calls)


class MixedToolAnswerTests(CitationApiFixture):
    def test_mixed_rag_and_analysis_outputs_both_survive_with_separate_verification(self):
        messages=[HumanMessage(content="实验准确率并整理全部文档"),AIMessage(content="",tool_calls=[
            {"name":"search_knowledge_tool","args":{"question":"实验准确率"},"id":"lookup"},
            {"name":"analyze_document_collection","args":{"question":"整理","scope_query":"notes"},"id":"analysis"}]),
            ToolMessage(name="search_knowledge_tool",tool_call_id="lookup",content=json.dumps({
                "collection":"sweet_kb_storage_v1","count":1,"matches":[self.record]})),
            ToolMessage(name="analyze_document_collection",tool_call_id="analysis",content=json.dumps({
                "kind":"document_analysis","status":"partial","analysis_id":"test-analysis","answer":"集合分析报告：仍有未知内容。","sources":[]}))]
        for draft in (self.draft,f"实验准确率99.9%。【{self.chunk_id}】"):
            self.actor.draft=draft
            result=graph_module.agent_node({"messages":messages},{"configurable":{"thread_id":"mixed"}})
            self.assertIn("集合分析报告：仍有未知内容。",result["messages"][-1].content)
            self.assertEqual(result["citation_report"]["document_analysis"]["analysis_id"],"test-analysis")
            if draft==self.draft:
                self.assertEqual(result["citation_report"]["status"],"verified")
                self.assertIn(self.chunk_id,result["messages"][-1].content)
            else:
                self.assertEqual(result["citation_report"]["status"],"rejected")
                self.assertNotIn("99.9%",result["messages"][-1].content)


class CollectionAgentFixture(SynthesisFixture):
    """真实 Agent/工具/审批/引用链；文档、SQLite、模型与检索均隔离。"""
    def setUp(self):
        super().setUp()
        from functools import partial
        from app.rag import citations,retrieval
        from app.rag.chunk_sync import sync_chunks
        from app.document_analysis.evidence_schema import CandidateEvidence,ExtractionResponse,ExtractorIdentity
        from tests.test_storage_retrieval import FakeCollection
        self.diary_sources=["KnowledgeBase/documents/Diary/24.7.17.md","KnowledgeBase/documents/Diary/24.7.18.md"]
        self.diary_texts=["梦见阿明一起散步。","梦见阿明和他在操场说话。"]
        for source,text in zip(self.diary_sources,self.diary_texts): self.write(source,text)
        self.sync(); fixture=self
        class DiaryExtractor:
            identity=ExtractorIdentity(name="mock-diary",version="1")
            authorized=True
            def __init__(self): self.calls=[]
            def extract(self,request):
                self.calls.append(request.window.source)
                fixture.assertIn(request.window.source,fixture.diary_sources)
                fixture.assertEqual(request.template.template_id,"people_events")
                index=fixture.diary_sources.index(request.window.source); quote=fixture.diary_texts[index]
                return ExtractionResponse(complete=True,candidates=[CandidateEvidence(quote=quote,conclusion=quote,
                    uncertainty="同名和代词不能确认真实身份。",details={"people":["阿明"] if index==0 else ["阿明","他"],"event":quote})])
        class Actor:
            def __init__(self): self.responses=[]; self.calls=0; self.prompts=[]
            def invoke(self,messages):
                self.calls+=1
                self.prompts.append(messages)
                return self.responses.pop(0) if self.responses else AIMessage(content="证据不足：不能从旧聊天确认。")
        self.extractor=DiaryExtractor(); self.actor=Actor(); self.store=SynthesisStore(self.evidence)
        self.workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator())
        directory=self.root.parent
        for module in (audit,llm_metrics,tracing): self.enterContext(patch.object(module,"AUDIT_DB_PATH",directory/"audit.sqlite3"))
        self.enterContext(patch.object(checkpoint,"DB_PATH",directory/"checkpoint.sqlite3"))
        self.enterContext(patch.object(checkpoint,"STORAGE_ROOT",self.root))
        self.enterContext(patch.object(session_store,"SESSION_DB_PATH",directory/"sessions.sqlite3"))
        self.enterContext(patch.object(session_store,"STORAGE_ROOT",self.root))
        self.enterContext(patch.object(session_store.os.path,"ismount",return_value=True))
        self.enterContext(patch.dict("os.environ",{"RAG_AUTO_SYNC_ENABLED":"0","DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM":"0",
            "DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"0","DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"0"}))
        self.enterContext(patch.object(tool_module,"default_analysis_service",return_value=self.workflow))
        self.enterContext(patch.object(graph_module,"llm_with_tools",self.actor))
        self.chunk_db=directory/"chunks.sqlite3"; self.cache=directory/"chunks"
        sync_chunks(source_root=self.root,parsed_root=self.parsed,source_db=self.db,cache_dir=self.cache,chunk_db=self.chunk_db,write=True)
        payload=next(payload for path in self.cache.glob("*.json") if (payload:=json.loads(path.read_text()))["source"]==self.diary_sources[0])
        chunk=payload["chunks"][0]
        self.rag_record={"source":self.diary_sources[0],"chunk_id":chunk["id"],"chunk_index":0,
            "file_sha256":chunk["file_sha256"],"document_id":chunk["document_id"],"file_type":"md","content":chunk["content"],"distance":0.1}
        self.collection=FakeCollection([{"id":chunk["id"],"text":chunk["content"],"distance":0.1,
            "meta":{key:self.rag_record[key] for key in ("source","chunk_index","file_sha256","document_id","file_type")}|{"is_active":True}}])
        self.enterContext(patch.object(retrieval,"_open_real_collection",return_value=self.collection))
        self.enterContext(patch.object(retrieval,"_embed_question",return_value=[0.1]*1024))
        self.enterContext(patch.object(graph_module,"verify_answer",partial(citations.verify_answer,
            source_root=self.root,chunk_db=self.chunk_db,cache_dir=self.cache)))
        self.source_decisions=[]; self.review_payloads=[]
        class Judge:
            def invoke(self,messages):
                payload=json.loads(messages[-1].content)
                fixture.review_payloads.append(payload)
                if payload.get("mode")=="source_dependency":
                    decision=fixture.source_decisions.pop(0) if fixture.source_decisions else "ordinary"
                    return AIMessage(content=json.dumps(decision if isinstance(decision,dict) else {"decision":decision}))
                records={item["chunk_id"]:item for item in payload["evidence"]}
                return AIMessage(content=json.dumps({"checks":[{"claim_id":claim["claim_id"],"supported":True,
                    "evidence":[{"chunk_id":key,"quote":records[key]["content"]} for key in claim["chunk_ids"]]} for claim in payload["claims"]]}))
        self.enterContext(patch.object(graph_module,"llm",Judge()))
        self.client=self.enterContext(TestClient(main.app))

    def begin(self,question,thread="diary-test"):
        response=self.client.post("/api/v1/chat/stream",json={"thread_id":thread,"request_id":"req-"+uuid.uuid4().hex,"message":question})
        self.assertEqual(response.status_code,200,response.text)
        return CitationApiFixture.events(response.text)

    def plan(self,question,scope="分析 `KnowledgeBase/documents/Diary` 目录下全部文档",template="people_events",name="analyze_document_collection"):
        args={"question":question}
        args.update({"scope_query":scope,"template_id":template} if name=="analyze_document_collection" else {"top_k":3})
        return AIMessage(content="",tool_calls=[{"id":"plan-"+uuid.uuid4().hex,"name":name,"args":args,"type":"tool_call"}])

    def approve(self,events,thread="diary-test",decision="approve",*,scope="KnowledgeBase/documents/Diary",template="people_events"):
        pending=next((value for name,value in events if name=="pending_approval"),None)
        self.assertIsNotNone(pending,"集合分析必须先请求人工审批")
        call=pending["pending_approvals"][0]
        self.assertEqual(call["tool_name"],"analyze_document_collection")
        self.assertEqual(call["arguments"]["template_id"],template)
        self.assertIn(scope,call["arguments"]["scope_query"])
        self.assertEqual(self.extractor.calls,[])
        response=self.client.post(f"/api/v1/threads/{thread}/review/stream",json={"decisions":{call["tool_call_id"]:decision},
            "expected_fingerprints":{call["tool_call_id"]:call["fingerprint"]},"request_id":"req-"+uuid.uuid4().hex})
        self.assertEqual(response.status_code,200,response.text)
        return next(value for name,value in CitationApiFixture.events(response.text) if name=="completed")


class DiaryAgentTests(CollectionAgentFixture):
    def test_dream_summary_passes_approval_execution_and_verified_source_publication(self):
        question="汇总 `KnowledgeBase/documents/Diary` 目录里梦见的事件。"
        self.actor.responses=[self.plan(question)]
        result=self.approve(self.begin(question))
        report=result["citation_report"]
        self.assertEqual(report["mode"],"document_analysis")
        self.assertIn("梦见",result["answer"])
        self.assertIn("[1]",result["answer"])
        self.assertEqual(set(self.extractor.calls),set(self.diary_sources))
        self.assertTrue(all(item["source"] in self.diary_sources for item in report["sources"]))
        self.assertEqual(self.actor.calls,1)
        self.assertEqual(session_store.list_messages("diary-test")["items"][-1]["content"],result["answer"])

    def test_dream_people_count_preserves_identity_uncertainty(self):
        question="统计 `KnowledgeBase/documents/Diary` 中我梦到了几个人？"
        self.actor.responses=[self.plan(question)]
        result=self.approve(self.begin(question))
        self.assertIn("提及次数",result["answer"])
        self.assertIn("不同对象数量",result["answer"])
        self.assertIn("无法确定",result["answer"])
        self.assertNotIn("不同人物数量：3",result["answer"])
        self.assertNotIn("梦到了3个人",result["answer"])

    def test_exact_date_question_uses_rag_and_does_not_require_full_diary_authorization(self):
        question="2024年7月17日的日记里，我梦到了什么？"
        self.actor.responses=[self.plan(question,name="search_knowledge_tool"),
            AIMessage(content=self.diary_texts[0]+"【"+self.rag_record["chunk_id"]+"】")]
        events=self.begin(question)
        self.assertFalse(any(name=="pending_approval" for name,value in events))
        result=next(value for name,value in events if name=="completed")
        self.assertEqual(result["citation_report"]["mode"],"rag")
        self.assertEqual(result["citation_report"]["status"],"verified")
        self.assertEqual(self.extractor.calls,[])

    def test_capability_and_general_dream_questions_do_not_trigger_full_reading(self):
        for index,question in enumerate(("你能检索日记么？","人为什么会做梦？","我梦见迷路，这代表什么？")):
            self.actor.responses=[AIMessage(content="可以查询资料；完整日记分析仍须审批和独立授权。")]
            events=self.begin(question,"diary-plain-"+str(index))
            self.assertFalse(any(name=="pending_approval" for name,value in events))
            result=next(value for name,value in events if name=="completed")
            self.assertEqual(result["citation_report"]["mode"],"plain")
        self.assertEqual(self.extractor.calls,[])

    def test_qualified_dates_and_subdirectories_are_not_expanded_to_all_diary(self):
        for index,question in enumerate(("汇总2024年7月日记中的梦境", "汇总 Diary/2024 目录下的梦境")):
            self.actor.responses=[AIMessage(content="请确认限定目录或日期范围后再申请集合分析。")]
            events=self.begin(question,"diary-qualified-"+str(index))
            self.assertFalse(any(name=="pending_approval" for name,value in events))
        self.assertEqual(self.extractor.calls,[])

    def test_rejection_or_disabled_cloud_never_reads_full_diary(self):
        from app.document_analysis.evidence_schema import ExtractorIdentity
        self.extractor.identity=ExtractorIdentity(name="blocked-diary-cloud",version="1",provider="deepseek")
        self.workflow.cloud_authorized=True
        for index,decision in enumerate(("reject","approve")):
            thread="diary-blocked-"+str(index)
            question="汇总 `KnowledgeBase/documents/Diary` 目录中的梦境。"
            self.actor.responses=[self.plan(question)]
            with patch.object(self.service,"read_verified",side_effect=AssertionError("must not read full diary")):
                result=self.approve(self.begin(question,thread),thread,decision)
            self.assertEqual(self.extractor.calls,[])
            self.assertNotIn("证据不足",result["answer"])
            if decision=="approve":
                self.assertEqual(result["citation_report"]["analysis_error_code"],"workflow_cloud_not_authorized")

    def legacy_rag_messages(self,error=False):
        return [HumanMessage(id="diary-fallback-turn",content="汇总 `KnowledgeBase/documents/Diary` 目录下梦见的事件"),AIMessage(content="",tool_calls=[{
            "id":"legacy-rag","name":"search_knowledge_tool","args":{"question":"梦境"},"type":"tool_call"}]),
            ToolMessage(name="search_knowledge_tool",tool_call_id="legacy-rag",status="error" if error else "success",
                content="private upstream failure" if error else json.dumps({"collection":"sweet_kb_storage_v1","count":0,"matches":[]}))]

    def test_insufficient_rag_can_replan_to_approved_analysis_without_a_second_refusal(self):
        self.actor.responses=[AIMessage(content="证据不足。"),AIMessage(content="",tool_calls=[{
            "id":"fallback-analysis","name":"analyze_document_collection","args":{"question":"汇总 `KnowledgeBase/documents/Diary` 目录下梦见的事件",
                "scope_query":"分析 `KnowledgeBase/documents/Diary` 目录下全部文档","template_id":"people_events"},"type":"tool_call"}])]
        graph=graph_module.build_graph(checkpointer=InMemorySaver()); config={"configurable":{"thread_id":"diary-fallback"}}
        graph.invoke({"messages":self.legacy_rag_messages()},config)
        self.assertTrue(graph.get_state(config).tasks)
        self.assertEqual(self.extractor.calls,[])
        graph.invoke(Command(resume={"decisions":{"fallback-analysis":"approve"}}),config)
        state=graph.get_state(config).values
        self.assertEqual(state["citation_report"]["mode"],"document_analysis")
        self.assertEqual(state["citation_report"]["rag_attempt"]["reason"],"no_evidence")
        self.assertIn("梦见",state["messages"][-1].content)
        self.assertFalse(state["messages"][-1].content.startswith("证据不足"))
        self.assertEqual(self.actor.calls,2)

    def test_replanning_is_bounded_and_does_not_hide_tool_errors(self):
        graph=graph_module.build_graph(checkpointer=InMemorySaver()); config={"configurable":{"thread_id":"diary-bounded"}}
        graph.invoke({"messages":self.legacy_rag_messages()},config)
        self.assertEqual(self.actor.calls,2)
        self.assertFalse(graph.get_state(config).tasks)
        self.actor.calls=0
        result=graph_module.agent_node({"messages":self.legacy_rag_messages(error=True)},config)
        self.assertEqual(self.actor.calls,1)
        self.assertEqual(result["citation_report"]["reason"],"retrieval_tool_error")
        self.assertIn("工具执行失败",result["messages"][-1].content)
        self.assertEqual(self.extractor.calls,[])


if __name__=="__main__":
    import unittest
    unittest.main()
