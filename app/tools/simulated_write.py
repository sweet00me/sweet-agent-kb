
"""
文件：app/tools/simulated_write.py

用途：
    提供一个用于测试 Agent 审批机制的模拟写入工具。

主要功能：
    1. 接收笔记标题和内容。
    2. 校验输入参数的类型与长度。
    3. 返回模拟保存结果。

安全说明：
    本工具不会创建、覆盖或删除任何真实文件。

    人工审批由统一 Approval Node 负责。
    本工具负责自身的参数合法性校验。

    未来替换为真实写入工具时，
    还必须增加路径限制、用户权限检查
    和重复操作防护，不能只依赖 Graph 审批。
"""

import json

from langchain_core.tools import tool


# =====================================
# 1. 模拟保存科研笔记
# =====================================

@tool
def simulate_save_note(
    title: str,
    content: str
) -> str:
    """
    模拟保存一条科研笔记。

    这是用于测试人工审批流程的模拟工具，
    不会实际写入文件。

    Args:
        title: 科研笔记标题，1～120字符。
        content: 科研笔记正文，1～2000字符。

    Returns:
        JSON 格式的模拟执行结果。
    """

    # 即使 LLM 提供了错误参数，
    # 工具也不能直接信任这些输入。
    if not isinstance(title, str):
        raise ValueError("标题必须是字符串")

    if not isinstance(content, str):
        raise ValueError("正文必须是字符串")

    title = title.strip()
    content = content.strip()

    if not 1 <= len(title) <= 120:
        raise ValueError("标题长度必须在1到120字符之间")

    if not 1 <= len(content) <= 2000:
        raise ValueError("正文长度必须在1到2000字符之间")

    # 当前只模拟执行。
    # 不使用 Path.write_text，也不调用数据库。
    result = {
        "status": "simulated",
        "title": title,
        "content_length": len(content),
        "message": "模拟保存成功，未写入真实知识库"
    }

    return json.dumps(
        result,
        ensure_ascii=False
    )
