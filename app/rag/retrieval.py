"""
科研知识库检索模块。

第一阶段复用已验证的检索实现，
后续再将检索实现完整迁移到 app/rag。
"""

from scripts.search_knowledge import search_knowledge

__all__ = ["search_knowledge"]