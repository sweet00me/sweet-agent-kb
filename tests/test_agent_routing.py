"""Task-based routing contracts; isolated models, real graph/approval/tools/publication."""
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from app.agents import graph as graph_module
from app.rag.citations import verify_answer
from app.document_analysis.evidence_schema import CandidateEvidence, ExtractionResponse, ExtractorIdentity
from tests.test_analysis_agent import CollectionAgentFixture


class TaskRoutingEndToEndTests(CollectionAgentFixture):
    def setUp(self):
        super().setUp()
        self.rows={}
        experiment="实验甲（UUID: 11111111-1111-4111-8111-111111111111）"
        for index,value in enumerate(("0.71","0.76")):
            self.rows[f"KnowledgeBase/documents/Experiments/run-{index}.md"]=(f"{experiment}：mIoU={value}。",
                {"experiment":"实验甲","parameters":[{"name":"mIoU","value":value,"unit":None}]},"research_parameters")
        for name,method in (("paper-a","稀疏检索"),("paper-b","稠密检索")):
            self.rows[f"KnowledgeBase/documents/Papers/{name}.md"]=(f"{name} 的方法是{method}。",
                {"subject":name,"predicate":"方法","object":method},"general_facts")
        for index,action in enumerate(("布置传感器","回收传感器")):
            self.rows[f"KnowledgeBase/documents/Fieldwork/record-{index}.md"]=(f"李明{action}。",
                {"people":["李明"],"event":f"李明{action}"},"people_events")
        for source,(text,_,_) in self.rows.items(): self.write(source,text)
        self.sync(); fixture=self
        diary=self.extractor
        class Extractor:
            identity=ExtractorIdentity(name="task-routing-fixture",version="1")
            def __init__(self): self.calls=[]
            def extract(self,request):
                source=request.window.source
                self.calls.append(source)
                if source in fixture.diary_sources:
                    return diary.extract(request)
                text,details,template=fixture.rows[source]
                fixture.assertEqual(request.template.template_id,template)
                return ExtractionResponse(complete=True,candidates=[CandidateEvidence(quote=text,conclusion=text,
                    uncertainty="仅模拟原文记录，不代表现实真实性。",details=details)])
        self.extractor=Extractor(); self.workflow.extractor=self.extractor

    @staticmethod
    def completed(events):
        return next(value for name,value in events if name=="completed")

    def analyze_collection(self,question,directory,template,thread):
        self.actor.responses=[self.plan(question,f"分析 `{directory}` 目录下全部文档",template)]
        events=self.begin(question,thread)
        self.assertEqual(self.actor.calls,1)
        self.assertFalse(self.extractor.calls)
        result=self.approve(events,thread,scope=directory,template=template)
        self.assertEqual(result["citation_report"]["mode"],"document_analysis")
        self.assertTrue(result["citation_report"]["sources"])
        self.assertTrue(all(source.startswith(directory+"/") for source in self.extractor.calls))
        self.assertEqual(len(self.extractor.calls),2)
        self.assertFalse(any("24.7.16.md" in item["source"] for item in result["citation_report"]["sources"]))
        self.assertEqual(result["pending_approvals"],[])
        return result

    def test_diary_summary_and_unrelated_event_collection_use_identical_controlled_route(self):
        for directory,question,expected in (("Diary","梦见的事件","梦见"),("Fieldwork","布置或回收传感器的事件","传感器")):
            with self.subTest(directory=directory):
                self.actor.calls=0; self.extractor.calls.clear()
                path="KnowledgeBase/documents/"+directory
                result=self.analyze_collection(f"汇总 `{path}` 目录下全部记录中{question}",path,"people_events","summary-"+directory)
                self.assertIn("文档处理：已分析 2/2",result["answer"])
                self.assertIn("[1]",result["answer"])
                self.assertIn(expected,result["answer"].split("文档处理：")[0])

    def test_experiment_highest_metric_uses_collection_and_existing_python_maximum(self):
        directory="KnowledgeBase/documents/Experiments"
        result=self.analyze_collection(f"统计 `{directory}` 中实验甲的最高 mIoU",directory,"research_parameters","metric-maximum")
        text=result["answer"].split("文档处理：")[0]
        self.assertIn("mIoU",text)
        self.assertIn("最高值 0.76",text)
        self.assertNotIn("最高值 2",text)

    def test_multiple_paper_comparison_retains_exact_file_scope(self):
        sources=[source for source in self.rows if "/Papers/" in source]
        scope="分析 "+" 和 ".join(f"`{source}`" for source in sources)+" 文档"
        question="比较 "+" 和 ".join(f"`{source}`" for source in sources)+" 的方法"
        self.actor.responses=[self.plan(question,scope,"general_facts")]
        result=self.approve(self.begin(question,"paper-comparison"),"paper-comparison",scope=sources[0],template="general_facts")
        self.assertEqual(set(self.extractor.calls),set(sources))
        self.assertIn("稀疏检索",result["answer"])
        self.assertIn("稠密检索",result["answer"])
        self.assertIn("[1]",result["answer"]); self.assertIn("[2]",result["answer"])

    def test_collection_keeps_modified_date_subdirectory_and_exclusion(self):
        directory="KnowledgeBase/documents/Experiments"
        sources=sorted(source for source in self.rows if source.startswith(directory+"/"))
        for source in sources:
            stamp=datetime(2024,7,17,12,tzinfo=timezone.utc).timestamp()
            os.utime(self.root/source,(stamp,stamp))
        self.sync()
        scope=f"分析 `{directory}` 目录下2024年7月修改的文档，排除 `{sources[1]}` 文件"
        question=f"统计 `{directory}` 目录下2024年7月修改的文档中的最高 mIoU，排除 `{sources[1]}`"
        self.actor.responses=[self.plan(question,scope,"research_parameters")]
        result=self.approve(self.begin(question,"limited-scope"),"limited-scope",scope=directory,template="research_parameters")
        self.assertEqual(self.extractor.calls,[sources[0]])
        # 单值比较未送语义核验，不能把结构检查升级为已支持的最高值。
        self.assertIn("极值无法确定",result["answer"])
        self.assertIn("已分析 1/1",result["answer"])
        self.assertNotIn("最高值 0.76",result["answer"])

    def test_sensitive_file_remains_denied_even_after_tool_approval(self):
        source="KnowledgeBase/documents/Diary/24.7.16.md"
        question=f"汇总 `{source}` 中的事件"
        self.actor.responses=[self.plan(question,f"读取 `{source}` 文档")]
        with patch.object(self.service,"read_verified",side_effect=AssertionError("excluded body must not be read")):
            result=self.approve(self.begin(question,"excluded-scope"),"excluded-scope",scope=source)
        self.assertEqual(self.extractor.calls,[])
        self.assertEqual(result["citation_report"]["analysis_error_code"],"access_denied")
        self.assertNotIn("证据不足",result["answer"])

    def test_exact_date_rag_preserves_query_and_verifies_actual_current_source(self):
        question="查询 `KnowledgeBase/documents/Diary` 中 2024年7月17日梦到的片段"
        self.actor.responses=[self.plan(question,name="search_knowledge_tool"),
            AIMessage(content=self.diary_texts[0]+"【"+self.rag_record["chunk_id"]+"】")]
        result=self.completed(self.begin(question,"precise-date"))
        self.assertEqual(result["citation_report"]["status"],"verified")
        self.assertEqual(result["citation_report"]["mode"],"rag")
        state=self.client.app.state.graph.get_state({"configurable":{"thread_id":"precise-date"}}).values
        call=next(call for message in state["messages"] if isinstance(message,AIMessage) for call in message.tool_calls)
        self.assertEqual(call["args"]["question"],question)
        self.assertFalse(self.extractor.calls)

    def test_general_knowledge_and_user_provided_content_need_no_tools(self):
        for index,(question,draft,decision) in enumerate((("稀疏检索是什么意思？","稀疏检索按离散词项匹配。","ordinary"),
                ("我提供的数据是 0.71 和 0.76，最大值是多少？","就你提供的数据，最大值是 0.76。","provided"))):
            self.actor.responses=[AIMessage(content=draft)]; self.source_decisions=[decision]
            events=self.begin(question,"public-"+str(index)); result=self.completed(events)
            self.assertFalse(any(name=="pending_approval" for name,_ in events))
            self.assertEqual(result["answer"],draft)
            self.assertEqual(result["citation_report"]["source_dependency"],decision)
            self.assertEqual(result["citation_report"]["status"],"not_applicable")
        self.assertFalse(self.extractor.calls)

    def test_no_tool_private_draft_replans_then_requires_real_approval(self):
        directory="KnowledgeBase/documents/Experiments"
        question=f"统计 `{directory}` 中实验甲的最高 mIoU"
        self.actor.responses=[AIMessage(content="我记得最高是99.9%。"),
            self.plan(question,f"分析 `{directory}` 目录下全部文档","research_parameters")]
        self.source_decisions=["requires_lookup"]
        pending=self.begin(question,"private-missing-tool")
        self.assertEqual(self.actor.calls,2)
        self.assertNotIn("99.9%",json.dumps(pending,ensure_ascii=False))
        self.assertFalse(self.extractor.calls)
        result=self.approve(pending,"private-missing-tool",scope=directory,template="research_parameters")
        self.assertNotIn("99.9%",result["answer"])
        self.assertIn("最高值 0.76",result["answer"])
        self.assertEqual(result["citation_report"]["source_review"]["reason"],"private_lookup_required")

    def test_unscoped_private_request_clarifies_without_defaulting_to_all_documents(self):
        self.actor.responses=[AIMessage(content="证据不足。"),AIMessage(content="请指定要分析的文件、目录或日期范围。")]
        self.source_decisions=["requires_lookup",{"decision":"clarify_scope","task_type":"collection_analysis","scope_status":"unknown"}]
        events=self.begin("请完整遍历我的文档并汇总共同观点，我尚未确定具体范围。","scope-clarification")
        result=self.completed(events)
        self.assertEqual(self.actor.calls,2)
        self.assertFalse(any(name=="pending_approval" for name,_ in events))
        self.assertIn("范围",result["answer"])
        self.assertNotIn("证据不足",result["answer"])
        self.assertFalse(self.extractor.calls)

    def test_repeated_private_guess_is_blocked_and_replanning_is_bounded(self):
        self.actor.responses=[AIMessage(content="最高是99.9%。"),AIMessage(content="证据不足。")]
        self.source_decisions=["requires_lookup","requires_lookup"]
        events=self.begin("我的记录中最高的指标是什么？","bounded-private")
        result=self.completed(events)
        self.assertEqual(self.actor.calls,2)
        self.assertEqual(result["citation_report"]["reason"],"private_lookup_required")
        self.assertNotIn("99.9%",json.dumps(events,ensure_ascii=False))
        self.assertNotIn("证据不足",result["answer"])
        self.assertFalse(self.extractor.calls)

    def test_empty_rag_can_publish_scope_clarification_without_scanning_everything(self):
        self.collection.rows=[]
        question="我的记录里有哪些相同的事件？"
        clarification="请指定要汇总的目录、文件或日期范围，完整分析仍需要审批。"
        self.actor.responses=[self.plan(question,name="search_knowledge_tool"),
            AIMessage(content="全部资料都没有相关内容。"),AIMessage(content=clarification)]
        self.source_decisions=[{"decision":"clarify_scope","task_type":"private_lookup","scope_status":"not_required"}]
        events=self.begin(question,"empty-rag-scope")
        result=self.completed(events)
        self.assertEqual(result["answer"],clarification)
        self.assertEqual(result["citation_report"]["status"],"not_applicable")
        self.assertEqual(result["citation_report"]["rag_attempt"]["reason"],"no_evidence")
        self.assertFalse(any(name=="pending_approval" for name,_ in events))
        self.assertNotIn("全部资料都没有",json.dumps(events,ensure_ascii=False))
        self.assertFalse(self.extractor.calls)

    def test_private_lookup_does_not_require_user_to_supply_a_path(self):
        # 不按主题判定；同一模型任务分类触发同一受控 RAG 路径。
        from app.rag.chunk_sync import sync_chunks
        sync_chunks(source_root=self.root,parsed_root=self.parsed,source_db=self.db,cache_dir=self.cache,chunk_db=self.chunk_db,write=True)
        payloads={payload["source"]:payload for path in self.cache.glob('*.json') if (payload:=json.loads(path.read_text()))}
        cases=(("我梦到过什么？",self.diary_sources[0]),("我记得我日记中有做梦的内容",self.diary_sources[1]),
            ("我的实验记录里有什么结果？","KnowledgeBase/documents/Experiments/run-0.md"),
            ("我保存的论文讨论了什么方法？","KnowledgeBase/documents/Papers/paper-a.md"))
        for index,(question,source) in enumerate(cases):
            with self.subTest(question=question):
                chunk=payloads[source]["chunks"][0]
                self.collection.rows=[{"id":chunk["id"],"text":chunk["content"],"distance":0.1,"meta":{
                    key:chunk[key] for key in ("source","chunk_index","file_sha256","document_id","file_type")}|{"is_active":True}}]
                self.actor.responses=[AIMessage(content="请先提供文件或目录路径。"),
                    AIMessage(content=chunk["content"].strip()+"【"+chunk["id"]+"】")]
                self.source_decisions=[{"decision":"clarify_scope","task_type":"private_lookup","scope_status":"not_required"}]
                events=self.begin(question,"private-rag-"+str(index)); result=self.completed(events)
                self.assertEqual(result["citation_report"]["mode"],"rag")
                self.assertEqual(result["citation_report"]["status"],"verified")
                self.assertEqual(result["citation_report"]["citations"][0]["source"],source)
                self.assertFalse(any(name=="pending_approval" for name,_ in events))
                state=self.client.app.state.graph.get_state({"configurable":{"thread_id":"private-rag-"+str(index)}}).values
                calls=[call for message in state["messages"] if isinstance(message,AIMessage) for call in message.tool_calls]
                self.assertEqual([(call["name"],call["args"]["question"]) for call in calls],[("search_knowledge_tool",question)])
                self.assertFalse(self.extractor.calls)

    def test_complete_analysis_reuses_explicit_scope_from_the_same_conversation(self):
        directory="KnowledgeBase/documents/Fieldwork"; thread="reuse-known-scope"
        self.actor.responses=[AIMessage(content="已记录这个范围。")]; self.source_decisions=["ordinary"]
        self.completed(self.begin(f"后续完整分析只允许 `{directory}` 目录。",thread))
        question="现在完整遍历上述目录，汇总传感器相关事件。"
        self.actor.responses=[AIMessage(content="请重新提供目录。"),self.plan(question,f"分析 `{directory}` 目录下全部文档","people_events")]
        self.source_decisions=[{"decision":"clarify_scope","task_type":"collection_analysis","scope_status":"known"}]
        result=self.approve(self.begin(question,thread),thread,scope=directory)
        self.assertTrue(all(source.startswith(directory+"/") for source in self.extractor.calls))
        self.assertEqual(len(self.extractor.calls),2)
        self.assertIn("传感器",result["answer"])

    def test_unknown_collection_scope_still_requires_clarification(self):
        question="请完整遍历我的一组文档，我还没确定要分析哪个范围。"
        clarification="请确认需要完整遍历的目录、文件集合或修改日期范围。"
        self.actor.responses=[AIMessage(content=clarification)]
        self.source_decisions=[{"decision":"clarify_scope","task_type":"collection_analysis","scope_status":"unknown"}]
        events=self.begin(question,"collection-unknown"); result=self.completed(events)
        self.assertEqual(result["answer"],clarification)
        self.assertFalse(self.extractor.calls)
        self.assertFalse(any(name=="pending_approval" for name,_ in events))


class SourceDependencyTests(unittest.TestCase):
    def test_scope_hints_keep_conversation_paths_without_trusting_old_ai_facts(self):
        seen=[]
        history=[HumanMessage(content="后面再选择范围"),AIMessage(content="目录 `Materials/records/`。AI_PRIVATE_WRONG_FACT 99.9。"),
            HumanMessage(content="确认完整遍历你上面提到的目录。")]
        def judge(payload):
            seen.append(payload)
            return {"decision":"clarify_scope","task_type":"collection_analysis","scope_status":"known"}
        _,report=verify_answer(history,"请重新提供目录。",judge=judge)
        self.assertEqual(report["reason"],"private_lookup_required")
        self.assertIn("Materials/records/",seen[0]["scope_hints"])
        self.assertNotIn("AI_PRIVATE_WRONG_FACT",json.dumps(seen))

    def test_ordinary_private_clarification_is_classified_as_lookup_required(self):
        _,report=verify_answer([HumanMessage(content="我保存的材料里有什么相关实验？")],"请先提供文件路径。",
            judge=lambda _: {"decision":"clarify_scope","task_type":"private_lookup","scope_status":"not_required"})
        self.assertEqual(report["reason"],"private_lookup_required")
        self.assertEqual(report["task_type"],"private_lookup")
    def test_private_answer_without_lookup_is_not_published(self):
        for question in ("我的实验最好的结果是多少？", "我梦到过什么？", "我的收藏有哪些共同观点？"):
            with self.subTest(question=question):
                seen=[]
                def judge(payload):
                    seen.append(payload)
                    return {"decision":"requires_lookup"}
                answer,report=verify_answer([HumanMessage(content=question)],"你记录了三个项目，最好的结果是99.9%。",judge=judge)
                self.assertEqual(report["reason"],"private_lookup_required")
                self.assertEqual(seen[0]["mode"],"source_dependency")
                self.assertNotIn("99.9%",answer)
                self.assertNotIn("证据不足",answer)

    def test_plain_and_user_provided_answers_do_not_claim_source_verification(self):
        for decision in ("ordinary","provided","clarify_scope"):
            with self.subTest(decision=decision):
                answer,report=verify_answer([HumanMessage(content="请帮助分析这个问题")],"请指定文件或目录范围。",
                    judge=lambda _: {"decision":decision,"task_type":"collection_analysis","scope_status":"unknown"})
                self.assertEqual(answer,"请指定文件或目录范围。")
                self.assertEqual(report["status"],"not_applicable")
                self.assertEqual(report["source_dependency"],decision)

    def test_old_refusal_is_not_new_evidence_and_bad_review_fails_closed(self):
        history=[HumanMessage(content="旧问题"),AIMessage(content="证据不足。"),HumanMessage(content="我的记录中有哪些共同事件？")]
        payloads=[]
        def judge(payload):
            payloads.append(payload)
            return {"decision":"requires_lookup"}
        answer,report=verify_answer(history,"证据不足。",judge=judge)
        self.assertEqual(report["reason"],"private_lookup_required")
        self.assertNotIn("证据不足",answer)
        self.assertNotIn("证据不足",json.dumps(payloads[0]["user_context"],ensure_ascii=False))
        for result in ({}, {"decision":"verified"}, "broken", {"decision":"requires_lookup","task_type":[]},
                {"decision":"clarify_scope","task_type":"collection_analysis","scope_status":"forged"}):
            _,report=verify_answer(history,"虚构结论",judge=lambda _,result=result:result)
            self.assertEqual(report["reason"],"verification_unavailable")

    def test_unknown_complete_collection_reaches_model_and_can_clarify(self):
        class Actor:
            calls=0
            def invoke(self,messages):
                self.calls+=1
                return AIMessage(content="请指定要分析的目录或日期范围。")
        actor=Actor()
        def fake_invoke(*,model,messages,**kwargs):
            if model is actor: return actor.invoke(messages)
            return AIMessage(content=json.dumps({"decision":"clarify_scope","task_type":"collection_analysis","scope_status":"unknown"}))
        with patch.object(graph_module,"llm_with_tools",actor),patch.object(graph_module,"invoke_with_metrics",side_effect=fake_invoke):
            result=graph_module.agent_node({"messages":[HumanMessage(content="请完整遍历我的一组文档，尚未确定范围。")]},{})
        self.assertEqual(actor.calls,1,"A topic must not bypass the native model tool planner")
        self.assertFalse(result["messages"][-1].tool_calls)
        self.assertIn("范围",result["messages"][-1].content)


if __name__=="__main__":
    unittest.main()
