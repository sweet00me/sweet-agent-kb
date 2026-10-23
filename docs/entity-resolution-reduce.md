# Entity Resolution 与 Evidence Reduce

提供独立 Python 服务：读取既有 Evidence Store，保存保守的实体关联，再对证据做可追溯的跨文档整理。默认纯离线；不接入现有 RAG、LangGraph 或 Web UI，不实现 Evidence Verification、Coverage Report 或子图。

## 文件清单

新增实现：

| 文件 | 职责 |
| --- | --- |
| `app/document_analysis/synthesis_schema.py` | 提及、四态关联、配置、辅助器和语义归纳的严格类型 |
| `app/document_analysis/evidence_input.py` | 复用 EvidenceStore/Catalog 的授权输入、版本与原文位置检查 |
| `app/document_analysis/synthesis_store.py` | 独立 SQLite、增量检查点、租约、结果与出处查询 |
| `app/document_analysis/synthesis_runtime.py` | 可恢复执行、取消、失败处理及两个独立云端授权门禁 |
| `app/document_analysis/entity_resolution.py` | 结构化提及、身份规则、模型建议与保守关联分组 |
| `app/document_analysis/evidence_reduce.py` | Python 去重、精确数值、比较、日期排序及可注入归纳 |

修改已有文件：

- `app/document_analysis/security.py`：提取共享的安全非阻塞 `exclusive_analysis_lock`。
- `app/document_analysis/evidence_store.py`：Map 改用同一锁 helper，原锁文件名、租约与恢复逻辑保持兼容。
- `.env.example`：增加默认关闭的 Entity/Reduce 授权开关。实际 `.env` 未修改。

新增测试：`tests/synthesis_support.py`、`tests/test_synthesis_input.py`、`tests/test_synthesis_store.py`、`tests/test_entity_resolution.py`、`tests/test_evidence_reduce.py`。

新增文档：本文、`docs/superpowers/specs/2026-10-22-resolution-reduce-design.md`、`docs/superpowers/plans/2026-10-22-resolution-reduce.md`。

## 授权输入与版本

`load_input(evidence_store, task_ids)` 通过已有公开接口读取任务、文档状态和 `verified` 证据；复用 Catalog 校验来源/解析缓存，使用 `get_origin` 核对引用与位置。没有提取到证据的已完成文档同样参与版本检查。不重复做文档解析、检索或 Map，不写向量数据库。

任务定义、状态、文档处理状态、证据载荷、Storage 身份和策略组成固定输入指纹。读取期间的任务变化、后续运行、结果发布前及查询时的版本变化都会拒绝复用。新源文件版本需先走现有同步与 Map，再生成新的关联/归纳运行；旧运行不能拿新版本证据继续恢复。

上游任务部分失败或未完成时，只使用其中已完成文档的已验证证据，输出保留 `upstream_incomplete=true`。失败文档的暂存/失效证据不会混入结果。所有输入和查询继续受当前权限及 `KnowledgeBase/documents/Diary/24.7.16.md` 强制排除约束。

## Entity Resolution

```python
from app.document_analysis.synthesis_store import SynthesisStore
from app.document_analysis.entity_resolution import EntityResolver

# evidence_store 是现有 EvidenceStore 实例；map_result 来自前一阶段的真实运行。
derived_store = SynthesisStore(evidence_store)
resolution = EntityResolver(derived_store).run([map_result["task_id"]])
```

从既有三种证据模板派生人物、实验及对象提及，保存提及 ID、结构化名称、角色、Evidence ID、原文归属和标识。默认 `ResolutionConfig(max_mentions=256, max_pairs=32768)`，仅比较同类别提及，超过配置上限即失败，不静默截断。数字形式的事实对象作为字面值，不强行当作实体；主体仍可为单字母或数字标识。

| relation | 含义 | 默认处理 |
| --- | --- | --- |
| `same` | 规则支持明确相同 | 无冲突时可进入同一关联组 |
| `possible_same` | 可能相同 | 不自动合并 |
| `unknown` | 无法确定 | 不自动合并 |
| `different` | 明确不同的规则判断或标注为建议的模型判断 | 不合并，保留判断来源 |

同时记录 `confidence_status`（`rule_supported/model_suggestion/uncertain/conflicted`）、`rationale`、`evidence_ids`、`automatic_merge`。关系状态必须连同置信状态读取，模型建议不等于规则确认。

支持的身份依据保持保守：

- 同一个原文位置、角色、名称在不同 Map 任务中重复出现，可识别为相同原始提及。
- 名称后明确紧邻的 `（ORCID: 0000-0002-1825-0097）` 仅用于人物；`（全局ID: UUID）` / `（UUID: UUID）` 可用于相应类别。不同名称的相同标识可建立跨文档关联；同域不同标识保留差异判断。
- 同一条证据原文中明确的 `张三（又名阿三）`、`张三又名阿三。` 等别名形式支持关联这两个提及，不据此猜测另一文档中的同名者。
- 姓名与标识的匹配有边界限制，`Ann` 不会借用 `JoAnn` 的标识，`李四` 不会被当成 `李四海` 的完整别名。无法确定边界时宁可不关联。
- 同名只有 `possible_same`；常见中英文第一、第二、第三人称及实验指代词保持 unresolved。名称未出现在引用中，也不能用于自动合并。
- 同一关联分量中出现明确不同约束或相互矛盾的标识/声明时，不合并该分量，保留 `identity_conflict` 和原始依据。
  传递冲突使一条关联失效时，该关联会同时保留第三篇文档等阻断依据的 Evidence ID。

这些规则处理的是**原文中的身份声明**，没有联网验证 ORCID/UUID 的真实性或所有权，也不裁决原文是否写错。

可注入 `EntityAssistant`：提供 `identity: ExtractorIdentity` 和 `resolve(ResolutionRequest) -> RelationSuggestion`。请求只包含这对提及及其授权证据副本；响应必须 `complete=true` 并引用双方证据，不能注入其他 Evidence ID。模型的 `same` 一律降为 `possible_same`，不因模型自报高置信而自动合并。

## Evidence Reduce

```python
from app.document_analysis.evidence_reduce import EvidenceReducer

if resolution["status"] == "completed":
    reduced = EvidenceReducer(derived_store).run(resolution["run_id"])
    if reduced["status"] == "completed":
        result = reduced["result"]
        # statistics / claims / comparisons / timeline / undated / uncertainties
```

Reduce 必须引用一个完成的 Entity Resolution 运行，并绑定其结果摘要。其 `completed` 只表示本次计算完成，不表示全部文档已经成功分析或全部事实已被提取。
没有可用证据时，Reduce 返回 `failed/no_available_evidence`，不输出缺少 Evidence ID 的零值统计结论。

Python 负责：

1. 按来源、文档 SHA、解析版本、原文位置、模板及结构化字段合并重复的结构化观察，保留全部 Evidence ID。
2. 结合关联组及精确字段整理相同陈述。身份不明的同名实体仍分开；不同出处不会丢失。陈述组数量不当作真实人数或事件次数。
3. 使用 Decimal 的足够精度表示数值，`0.001` 和 `1e-3` 可以归为同值，高于 28 位的不同数值不会被默认上下文舍入成相同。超出数值解析限制的值保留原文字面形式。
4. 按实验/参数和主体/谓词比较备选值。不同数值或对象保留为 `potential_conflict`；它可能来自不同时间或条件，不自动判定谁对谁错。不同单位不换算，不生成可比最小/最大值。
5. 从原引用中的 ISO 日期（`YYYY-MM-DD`）整理人物/事件陈述时间线。无日期、多日期、非法日期，或结构化日期不在原文中时，保留 `undated`；不使用文件修改时间冒充事件日期。

`statistics` 包含 `input_records`、`unique_observations`、`duplicate_origin_records`、`statement_groups`、`entity_groups`、`unresolved_mentions`、`potential_conflicts`、`dated_items`。这些数值是**当前提供的证据集合上的精确计算**。每条统计带 `basis="provided_evidence_only"`、`exhaustive=false` 和输入 Evidence ID；不是现实世界中完整的人数、实验数或事件数。

每个陈述、比较、时间线及统计有稳定 ID 和 Evidence ID。潜在冲突保留全部备选值及各自来源；事件/实验对应事实的语义真实性不在本阶段验证。

可注入 `EvidenceSummarizer`：提供 `identity` 和 `summarize(ReduceRequest) -> SummaryResponse`。默认不启用语义归纳。请求按证据条数与记录字符数分批，`ReduceConfig` 默认 16 条/32768 字符（记录 JSON，非 Token 预算），过大的单条记录会明确失败。辅助器得到当前批次证据、相关陈述和不确定性；必须显式完成，输出只能引用本批次 Evidence ID。

语义输出单独放在 `semantic_conclusions`，不能改写 Python 统计。服务器统一加上“仅就已提供证据”和“非完整统计；事实语义未核验”的限定，并拒绝常见的完整统计/伪造计数表述。这是输出协议检查，不是完整的语义核验器，仍可能遗漏模型文本中的其他错误。

## 原文位置与事实语义必须区分

- `source_location_verified=true`：引用、文档 SHA 和解析正文位置已经由既有校验链核对。
- `semantic_verified=false`：本阶段所有关联和归纳输出恒为 false；原文位置正确不能证明结论被引用蕴含，也不能证明原文事实为真。
- `exhaustive=false`：统计与语义归纳只针对提供的证据。即使上游任务全部 completed，也不宣称现实世界或完整文档集合统计无遗漏。
- 证据冲突、身份未定、上游部分失败、时间不明均保留在 `uncertainties`，不会被归纳器静默消除。

## 独立 SQLite、恢复与溯源

默认按需创建 `Storage/KnowledgeBase/sqlite/evidence_synthesis.sqlite3`，拒绝越界、链接、解析库/快照库/Map 库及已有其他表的数据库。

| 表 | 内容 |
| --- | --- |
| `synthesis_runs` | 类型、输入与依赖指纹、配置/算法/辅助器版本、运行租约、状态、结果及摘要 |
| `synthesis_items` | 按实体对或归纳批次增量保存的检查点及载荷摘要 |

重复运行同一任务定义得到相同运行 ID，已完成结果在重新校验后复用；失败或取消保留已提交检查点，恢复跳过已提交的单元。不完整结果保持 `result=None`，不会返回成功。进程异常退出后释放内核锁，下次运行恢复遗留租约；不保证尚未提交的模型调用恰好执行一次。

同一派生库使用安全的独立非阻塞文件锁，只允许一个执行者；不占用 RAG 同步锁或 Map 锁。结果发布及查询前再次检查输入与依赖，源文件、缓存、权限或上游任务状态变化会拒绝旧结果。外部修改发生在完成之后时，由下次查询/运行发现；不锁定整个文件系统。

```python
run = derived_store.get_run(reduced["run_id"])
claim = run["result"]["claims"][0]
origins = derived_store.get_origin(run["run_id"], claim["claim_id"])
# origins 中包含原 EvidenceRecord、SHA、引用、Unicode 位置和当前版本状态。
```

`get_origin` 同样支持输出中的 `entity_id`、`relation_id`、`mention_id`、`comparison_id`、`timeline_id`、`statistic_id`。查询会重新授权，不能借历史结果访问被排除的日记或当前无权限的来源。

## 默认离线与独立云端授权

Entity、Reduce 和之前的 Map 开关互不替代：

```dotenv
DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM=0
DOCUMENT_ANALYSIS_ALLOW_REDUCE_LLM=0
```

只有注入的辅助器声明云端 `identity.provider="deepseek"`、显式 `authorized=True`，并开启对应阶段开关为 `1`，才允许调用。每次调用前再次检查当前输入及开关，撤销授权后不继续发起请求。拥有 API Key 或只开启 Map/另一阶段不构成授权。

此阶段仅提供可注入协议，没有新建云端模型客户端或适配器。注入器属于可信应用代码，应如实声明 provider；授权门禁不可能阻止任意 Python 代码私下联网。测试始终使用模拟辅助器，并禁止外部网络；没有调用真实 DeepSeek 或 Embedding。

## 测试与限制

```bash
/home/sweet/miniconda3/envs/agent_kb/bin/python -m unittest \
  tests.test_synthesis_input tests.test_synthesis_store \
  tests.test_entity_resolution tests.test_evidence_reduce
```

覆盖四态关系、同名与代词、别名/标识边界、传递冲突、证据去重、Decimal 精度、单位差异、潜在冲突、时间线、未知来源、上游恢复/部分失败、权限、版本变化、协作取消、KeyboardInterrupt、实际子进程异常退出和独立云端授权。

验证结果：新增 51 项离线测试；与原有基础层、Map/Store、API/SSE、引用、定时同步、文件及会话功能联合运行，**246 项测试全部通过（21.390 秒）**。另外 17 组历史离线回归全部通过。测试阻止外部网络，HTTP 测试仅使用本地回环。`git diff --check` 通过。

独立只读复核发现的姓名/别名子串误关联及常见代词遗漏，已通过先失败后通过的测试修复；另补传递冲突第三方出处、超过 28 位的小数精度、单字母主体与空证据不产出无引用统计的回归。正式 Storage 未创建本阶段派生库，未运行真实云端分析。

限制：规则只识别有限的明确身份/日期表达，宁可保留 unknown；无别名词典、实体全局注册表或学习排序。同名和代词可能产生多个未定关联组，不能据此解释真实人数。来源与 SHA 校验会增加批量分析开销；语义归纳记录大小按字符而非模型 Token 估算。协作取消不能强制中断已经进行的辅助器调用，超时由注入器负责。模型语义质量、身份声明真实性及真实云端兼容性未验证。本阶段没有 Evidence Verification、Coverage Report、Reduce 之外的后续子图或界面接入。
