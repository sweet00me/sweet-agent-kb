
"""
文件：app/rag/pdf_ingestion.py

用途：
    将科研论文 PDF 解析为 Markdown，
    并将原始 PDF、解析结果和元数据归档到知识库。

主要功能：
    1. 使用 PyMuPDF4LLM 提取 PDF 文本。
    2. 按页面保存 Markdown 内容。
    3. 使用 SHA256 标识 PDF 版本，避免重复解析。
    4. 保存原始 PDF 和文档元数据。

注意：
    本模块不执行 Embedding，也不写入 ChromaDB。
    后续由独立的文档分块和索引模块负责向量化。

运行：
    python -m app.rag.pdf_ingestion /path/to/paper.pdf
"""

import os
import json
import shutil
import hashlib
import argparse

from pathlib import Path

import pymupdf4llm


# =====================================
# 1. 知识库存储路径
# =====================================

STORAGE_ROOT = Path("/home/sweet/Storage")

KB_ROOT = STORAGE_ROOT / "KnowledgeBase"

PAPER_ROOT = KB_ROOT / "documents" / "papers"

ORIGINAL_DIR = PAPER_ROOT / "originals"
METADATA_DIR = PAPER_ROOT / "_metadata"


# =====================================
# 2. 计算文件哈希
# =====================================

def calculate_sha256(file_path: Path) -> str:
    """
    分块计算 PDF 的 SHA256。

    避免一次性将整个 PDF 加载到内存。
    相同文件内容将得到相同的哈希值。
    """

    digest = hashlib.sha256()

    with file_path.open("rb") as file:

        while True:
            data = file.read(1024 * 1024)

            if not data:
                break

            digest.update(data)

    return digest.hexdigest()


# =====================================
# 3. 解析 PDF
# =====================================

def parse_pdf(pdf_path: Path):
    """
    使用 PyMuPDF4LLM 按页解析 PDF。

    返回：
        pages: 每页提取的 Markdown 文本
        empty_pages: 未提取到有效文本的页码
    """

    print("[PDF] 开始解析：", pdf_path.name)

    # page_chunks=True 使结果按页返回，
    # 便于后续建立 Chunk 与论文页码之间的映射。
    page_chunks = pymupdf4llm.to_markdown(
        str(pdf_path),
        page_chunks=True,
        use_ocr=False,
        write_images=False,
    )

    if not isinstance(page_chunks, list):
        raise RuntimeError("PDF 解析结果不是预期的页面列表")

    pages = []
    empty_pages = []

    for page_number, item in enumerate(
        page_chunks, start=1
    ):

        text = item.get("text", "").strip()

        if not text:
            empty_pages.append(page_number)

        pages.append(text)

    if not any(pages):
        raise RuntimeError(
            "PDF 未提取到正文，可能需要 OCR"
        )

    print("[PDF] 页面数量：", len(pages))

    if empty_pages:
        print("[PDF] 空文本页码：", empty_pages)

    return pages, empty_pages


# =====================================
# 4. 将 PDF 导入知识库
# =====================================

def import_pdf(pdf_path: Path):
    """归档原始论文、Markdown 和来源元数据。"""

    pdf_path = pdf_path.expanduser().resolve()

    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("只支持 PDF 文件")

    # 防止 Storage 未挂载时写入系统盘。
    if not os.path.ismount(STORAGE_ROOT):
        raise RuntimeError("Storage 未正确挂载")

    # 限制本进程新建文件的默认访问权限。
    os.umask(0o077)

    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    file_hash = calculate_sha256(pdf_path)

    # 使用文件名和内容哈希生成稳定文档标识。
    # 同一 PDF 重复导入，不产生新的文档版本。
    document_id = (
        f"{pdf_path.stem}-{file_hash[:12]}"
    )

    markdown_path = PAPER_ROOT / f"{document_id}.md"
    archived_pdf = ORIGINAL_DIR / f"{document_id}.pdf"
    metadata_path = METADATA_DIR / f"{document_id}.json"

    outputs = [
        markdown_path,
        archived_pdf,
        metadata_path,
    ]

    # 仅在三份输出都存在且元数据一致时跳过。
    if all(path.exists() for path in outputs):

        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8")
        )

        if metadata.get("sha256") == file_hash:
            print("[PDF] 文档已导入，跳过重复解析")
            print("Markdown：", markdown_path)
            return

    # 如果只有部分文件存在，不贸然覆盖，
    # 避免破坏上次导入留下的文件。
    if any(path.exists() for path in outputs):
        raise RuntimeError(
            "检测到不完整或冲突的导入文件，"
            "请先检查归档目录"
        )

    pages, empty_pages = parse_pdf(pdf_path)

    # =================================
    # 5. 构造 Markdown
    # =================================

    sections = [
        f"<!-- document_id: {document_id} -->",
        f"<!-- source_pdf: originals/{document_id}.pdf -->",
    ]

    for page_number, text in enumerate(pages, start=1):

        # 保留显式页码标记。
        # 后续分块器可以利用标记建立来源映射。
        sections.append(
            f"<!-- PDF_PAGE: {page_number} -->\n\n"
            + text
        )

    markdown = "\n\n".join(sections) + "\n"

    # =================================
    # 6. 保存原始 PDF 和解析文件
    # =================================

    shutil.copy2(
        pdf_path,
        archived_pdf
    )

    markdown_path.write_text(
        markdown,
        encoding="utf-8"
    )

    # 保存机器可读取的来源信息。
    metadata = {
        "document_id": document_id,
        "filename": pdf_path.name,
        "original_path": str(pdf_path),
        "archived_pdf": str(
            archived_pdf.relative_to(KB_ROOT)
        ),
        "markdown_path": str(
            markdown_path.relative_to(KB_ROOT)
        ),
        "sha256": file_hash,
        "parser": "pymupdf4llm",
        "page_count": len(pages),
        "empty_pages": empty_pages,
        "text_characters": sum(len(p) for p in pages),
    }

    metadata_path.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print("\n========== PDF 导入完成 ==========")
    print("文档 ID：", document_id)
    print("原始 PDF：", archived_pdf)
    print("Markdown：", markdown_path)
    print("元数据：", metadata_path)
    print("页数：", len(pages))
    print("正文字符数：", metadata["text_characters"])

    print("\n当前仅完成解析归档，尚未写入 ChromaDB。")


# =====================================
# 7. 命令行入口
# =====================================

def main():

    parser = argparse.ArgumentParser(
        description="将科研论文 PDF 解析并归档到知识库"
    )

    parser.add_argument(
        "pdf",
        type=Path,
        help="需要导入的 PDF 路径"
    )

    args = parser.parse_args()

    import_pdf(args.pdf)


if __name__ == "__main__":
    main()
