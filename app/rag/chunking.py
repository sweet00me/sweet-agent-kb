
"""
文件：app/rag/chunking.py

用途：
    将 PDF 解析生成的 Markdown 切分为可检索的 Chunk。

主要功能：
    1. 识别 PDF_PAGE 页码标记。
    2. 按页面、段落组织文本。
    3. 为每个 Chunk 生成来源元数据和稳定 ID。
    4. 预览分块结果，不修改原始文件。

设计说明：
    当前优先保持段落完整。
    超过目标长度的单个段落暂不强制截断，
    而是标记出来，供后续优化。

运行：
    python -m app.rag.chunking <markdown文件>
"""

import re
import json
import hashlib
import argparse

from pathlib import Path


# PDF 解析阶段写入的页码标记
PAGE_PATTERN = re.compile(
    r"(?m)^<!-- PDF_PAGE:\s*(\d+)\s*-->\s*$"
)


# =====================================
# 1. 提取各页面
# =====================================

def extract_pages(markdown: str):
    """
    读取 Markdown 中的 PDF 页面标记。

    返回：
        [(page_number, page_text), ...]

    通过页面边界分块，方便以后引用论文页码。
    """

    markers = list(PAGE_PATTERN.finditer(markdown))

    if not markers:
        raise ValueError(
            "没有找到 PDF_PAGE 标记，"
            "请确认输入的是 PDF 解析生成的 Markdown"
        )

    pages = []

    for i, marker in enumerate(markers):

        start = marker.end()

        end = (
            markers[i + 1].start()
            if i + 1 < len(markers)
            else len(markdown)
        )

        page_number = int(marker.group(1))
        page_text = markdown[start:end].strip()

        pages.append((page_number, page_text))

    return pages


# =====================================
# 2. 页面内按段落分块
# =====================================

def split_page(page_text: str, max_chars: int = 1000):
    """
    将单页 Markdown 按段落组织成 Chunk。

    max_chars 是目标长度，不是绝对上限。

    重要：
        如果单个段落超过 max_chars，
        当前版本保留整个段落，避免破坏公式或表格。
    """

    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")

    # Markdown 中的空行通常表示段落边界
    blocks = [
        block.strip()
        for block in re.split(
            r"\n\s*\n+",
            page_text
        )
        if block.strip()
    ]

    chunks = []
    current = ""

    for block in blocks:

        if not current:
            current = block
            continue

        candidate = current + "\n\n" + block

        # 当前 Chunk 仍在目标长度内，可以继续合并
        if len(candidate) <= max_chars:
            current = candidate

        else:
            # 已经接近目标长度，先保存当前 Chunk
            chunks.append(current)

            # 新段落作为下一个 Chunk 的开始
            current = block

    if current:
        chunks.append(current)

    return chunks


# =====================================
# 3. 构建带来源信息的 Chunk
# =====================================

def build_chunks(
    markdown_path: Path,
    max_chars: int = 1000
):
    """
    将 Markdown 转换为结构化 Chunk 列表。

    每个 Chunk 包含：
        id
        document_id
        source
        source_pdf
        page_number
        chunk_index
        content
    """

    markdown_path = markdown_path.resolve()

    markdown = markdown_path.read_text(
        encoding="utf-8"
    )

    # 使用 PDF 导入阶段生成的元数据
    metadata_path = (
        markdown_path.parent
        / "_metadata"
        / f"{markdown_path.stem}.json"
    )

    if not metadata_path.is_file():
        raise FileNotFoundError(
            f"缺少 PDF 元数据：{metadata_path}"
        )

    metadata = json.loads(
        metadata_path.read_text(encoding="utf-8")
    )

    document_id = metadata["document_id"]
    source_pdf = metadata["archived_pdf"]
    source = metadata["markdown_path"]

    pages = extract_pages(markdown)

    chunks = []

    for page_number, page_text in pages:

        if not page_text:
            continue

        page_chunks = split_page(
            page_text,
            max_chars=max_chars
        )

        for content in page_chunks:

            chunk_index = len(chunks)

            # 使用文档、页码、位置与文本计算 ID。
            # 相同输入会产生相同 ID，方便后续避免重复索引。
            identity = (
                f"{document_id}:"
                f"{page_number}:"
                f"{chunk_index}:"
                f"{content}"
            )

            chunk_id = hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:32]

            chunks.append({
                "id": chunk_id,
                "document_id": document_id,
                "source": source,
                "source_pdf": source_pdf,
                "page_number": page_number,
                "chunk_index": chunk_index,
                "content": content,
            })

    if not chunks:
        raise RuntimeError("没有产生有效的 Chunk")

    return chunks, pages


# =====================================
# 4. 命令行预览
# =====================================

def main():

    parser = argparse.ArgumentParser(
        description="预览科研论文 Markdown 分块"
    )

    parser.add_argument(
        "markdown",
        type=Path,
        help="PDF 解析得到的 Markdown 文件"
    )

    parser.add_argument(
        "--max-chars",
        type=int,
        default=1000,
        help="单个 Chunk 的目标字符数"
    )

    parser.add_argument(
        "--preview",
        type=int,
        default=3,
        help="展示前几个 Chunk"
    )

    args = parser.parse_args()

    chunks, pages = build_chunks(
        args.markdown,
        max_chars=args.max_chars
    )

    lengths = [
        len(chunk["content"])
        for chunk in chunks
    ]

    oversized = [
        chunk
        for chunk in chunks
        if len(chunk["content"]) > args.max_chars
    ]

    print("========== Chunking 统计 ==========")
    print("Markdown：", args.markdown)
    print("页面数量：", len(pages))
    print("Chunk 总数：", len(chunks))
    print("最短 Chunk：", min(lengths))
    print("最长 Chunk：", max(lengths))
    print("平均字符数：", round(sum(lengths) / len(lengths), 1))
    print("超出目标长度：", len(oversized))

    for chunk in chunks[:args.preview]:

        print("\n" + "=" * 55)
        print("Chunk：", chunk["chunk_index"])
        print("页码：", chunk["page_number"])
        print("字符数：", len(chunk["content"]))
        print("来源：", chunk["source"])
        print("内容预览：")
        print(chunk["content"][:350])

    print("\n本次只进行分块预览，没有写入 ChromaDB。")


if __name__ == "__main__":
    main()
