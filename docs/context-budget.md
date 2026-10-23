# 900K 上下文摘要阈值

`900K` 表示完整模型输入的本地估算达到 **900,000 Token** 时触发摘要。输入包含系统提示、已有摘要、活动历史、工具定义和工具返回。每次 Agent 调用前检查，包括工具执行后和审批恢复后；不再按三轮对话触发，也不固定压缩到 250 字。

## 配置

在项目 `.env` 设置以下值，修改后重启 API：

```dotenv
AGENT_CONTEXT_TRIGGER_TOKENS=900000
AGENT_CONTEXT_TARGET_TOKENS=650000
AGENT_MAX_OUTPUT_TOKENS=32768
AGENT_SUMMARY_MAX_TOKENS=4096
AGENT_SUMMARY_BATCH_TOKENS=65536
```

使用 DeepSeek Flash 当前的 1,048,576 Token 窗口，预留最多 32,768 Token 输出、65,536 Token 工具空间和 32,768 Token 安全空间。总预算为 1,031,072，启动时校验预算关系。切换模型需要核对窗口和分词器，不能直接沿用上述模型假设。模型窗口说明见 [DeepSeek 模型接口](https://api-docs.deepseek.com/api/list-models/)。

## 本地分词器

使用 [DeepSeek-V4.1-Flash-1017 官方词表](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017)，固定版本 `1bae0e1088993b9b82de08f59de5be5a34e42170`，SHA256 为 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`。只下载约 6 MB 的 `tokenizer.json`，不加载模型权重、不占用 GPU，也不执行远程模型代码。

在运行 API 的 Python 环境安装 `tokenizers`（本次验证版本 0.23.3），然后在项目根目录运行：

```bash
python -m app.memory.context_budget --install-tokenizer
```

默认缓存路径：`~/.cache/sweet-agent-kb/tokenizers/deepseek-v41-flash-1017/tokenizer.json`。可通过 `DEEPSEEK_TOKENIZER_PATH` 指定位置；启动和每个进程首次使用时校验固定哈希，运行时不会自动联网下载。缺失或损坏时明确报错，不退回字符数或其他模型的分词器。

正文按官方词表计数；消息和工具参数按 JSON 编码计数，再添加保守的协议开销。它是触发策略使用的本地估算，不是服务端原生聊天协议的精确计数；实际用量仍以 API 返回的 `usage` 为准。

## 压缩与保留

达到阈值后，从最早的完整对话轮次开始生成滚动摘要，每批摘要输入不超过 65,536 Token，最终摘要不超过 4,096 Token。压缩后通常回到 650,000 Token 以下，给后续对话留下空间；不会每问一次就压缩。

当前用户轮次和工具调用/返回始终完整保留。若当前轮本身超过 650K、但连同摘要仍低于 900K，会保留当前轮，并记录 `target_reached=False`；650K 是回落目标。若当前轮本身或压缩后的完整输入仍达到 900K，则停止调用并明确失败，不静默截断当前问题或工具结果。

摘要必须正常结束；超时、空摘要、超预算、被输出上限截断、其他异常结束或不完整的工具调用链，都会停止后续 Agent 调用。摘要全部成功且 Agent 节点成功返回后，才从最新活动状态移出旧消息；原始聊天历史、审计日志和旧 Checkpoint 保留。较早已经生成的摘要不能恢复为完整原文，但原聊天记录仍可查询。

压缩摘要仅作对话记忆，不能替代本轮 RAG 检索作为来源证据。HTTP/SSE 不会把内部摘要当作回答输出。单条 API 消息的 10,000 字符限制、RAG 的片段数量和长度仍由各自配置管理。

## 验证与运行观察

回归入口：`python -m unittest tests.test_context_budget`。覆盖实际官方分词器的 900K 上下边界、等于阈值、滚动摘要、当前轮和工具配对、摘要异常、旧 Checkpoint 和完整聊天记录保留，以及 HTTP/SSE 联动。

API 日志中的 `[Context]` 行记录本地输入估算、触发阈值、移出的消息数、摘要调用次数和是否达到回落目标，不打印聊天内容。900K 边界通过本地测试验证；未向云端发送完整 900K 压力请求。接近窗口时可能增加费用和延迟，现有单次模型请求超时仍为 60 秒。
