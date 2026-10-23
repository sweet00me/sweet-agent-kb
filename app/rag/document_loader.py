
"""
文件：app/rag/document_loader.py

用途：
    Sweet Research Agent 统一文档解析器。

支持：
    Markdown (.md)
    TXT (.txt)
    Word (.docx)

功能：
    1. 读取知识库指定目录中的文档。
    2. 统一提取文本内容。
    3. 尽可能保留 Word 段落与表格的顺序。
    4. 生成稳定文档 ID 和 SHA256。
    5. 标记暂未完整解析的图片和公式。

限制：
    不解析 PDF。
    不调用 LLM。
    不生成 Embedding。
    不写入 ChromaDB。
"""

import hashlib
import zipfile

from pathlib import Path
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.table import Table


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CONTENT_CHARS = 1_000_000
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024

SUPPORTED_EXTENSIONS = {
    ".md",
    ".txt",
    ".docx",
}


class DocumentLoadError(Exception):
    """文档读取或解析失败。"""


# =====================================
# 1. 基础工具
# =====================================

def calculate_sha256(path: Path) -> str:

    digest = hashlib.sha256()

    with path.open("rb") as f:

        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def resolve_document_path(
    root: Path,
    relative_path: str,
) -> Path:
    """
    只允许读取指定文档目录中的普通文件。

    这是针对受控知识库的第一版入口，
    暂不直接对外暴露为 HTTP 文件读取 API。
    """

    root = root.resolve(strict=True)

    if (
        not relative_path
        or "\\" in relative_path
        or relative_path.startswith("/")
    ):
        raise DocumentLoadError("文档路径无效")

    parts = relative_path.split("/")

    if any(
        not part
        or part in {".", ".."}
        or part.startswith(".")
        for part in parts
    ):
        raise DocumentLoadError("文档路径包含禁止的部分")

    current = root

    for part in parts:

        current = current / part

        if current.is_symlink():
            raise DocumentLoadError(
                "不允许通过符号链接读取文件"
            )

    try:
        target = current.resolve(strict=True)

        target.relative_to(root)

    except (OSError, ValueError) as exc:
        raise DocumentLoadError(
            "文档不存在或不在知识库目录中"
        ) from exc

    if not target.is_file():
        raise DocumentLoadError("目标不是普通文件")

    if target.stat().st_size > MAX_FILE_BYTES:
        raise DocumentLoadError(
            "文档超过 20 MiB 限制"
        )

    return target


# =====================================
# 2. Markdown 和 TXT 解析
# =====================================

def read_text_document(path: Path) -> tuple[str, list]:

    raw = path.read_bytes()

    try:
        content = raw.decode("utf-8-sig")

    except UnicodeDecodeError:

        try:
            content = raw.decode("gb18030")

        except UnicodeDecodeError as exc:
            raise DocumentLoadError(
                "不支持的文本编码"
            ) from exc

    if "\x00" in content:
        raise DocumentLoadError(
            "文件疑似二进制内容"
        )

    return content, []


# =====================================
# 3. Word DOCX 解析
# =====================================

def read_docx_document(path: Path) -> tuple[str, list]:
    """
    读取 Word 正文和表格。

    不声称已经完整恢复 OMML 数学公式和图片。
    """

    if not zipfile.is_zipfile(path):
        raise DocumentLoadError("DOCX 文件格式错误")

    try:

        with zipfile.ZipFile(path) as archive:

            members = archive.infolist()

            if len(members) > 5000:
                raise DocumentLoadError(
                    "DOCX 内部文件数量过多"
                )

            total_size = sum(
                item.file_size for item in members
            )

            if total_size > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise DocumentLoadError(
                    "DOCX 解压后内容过大"
                )

        doc = Document(str(path))

    except DocumentLoadError:
        raise

    except Exception as exc:
        raise DocumentLoadError(
            "Word 文档无法解析"
        ) from exc

    warnings = []
    sections = []

    # 检测 Word 原生数学公式。
    math_count = sum(
        1 for _ in doc.element.body.iter(qn("m:oMath"))
    )

    if math_count:
        warnings.append(
            f"DOCX_MATH_NOT_EXTRACTED:{math_count}"
        )

    # 检测正文中的图片/绘图元素。
    drawing_count = sum(
        1 for _ in doc.element.body.iter(qn("w:drawing"))
    )

    if drawing_count:
        warnings.append(
            f"DOCX_DRAWINGS_NOT_EXTRACTED:{drawing_count}"
        )

    # python-docx 1.2+ 支持按原文顺序遍历。
    for block in doc.iter_inner_content():

        if isinstance(block, Paragraph):

            text = block.text.strip()

            if not text:
                continue

            style = block.style.name if block.style else ""

            # 保留常见 Word 标题层级。
            if style.startswith("Heading"):

                suffix = style.replace(
                    "Heading", ""
                ).strip()

                if suffix.isdigit():
                    level = min(int(suffix), 6)
                    text = "#" * level + " " + text

            sections.append(text)

        elif isinstance(block, Table):

            rows = []

            for row in block.rows:

                cells = [
                    cell.text.strip()
                    .replace("\n", " ")
                    .replace("|", r"\|")
                    for cell in row.cells
                ]

                rows.append(cells)

            if rows:

                width = max(len(r) for r in rows)

                header = rows[0] + [""] * (
                    width - len(rows[0])
                )

                table_lines = [
                    "| " + " | ".join(header) + " |",
                    "| " + " | ".join(
                        ["---"] * width
                    ) + " |",
                ]

                for row in rows[1:]:

                    padded = row + [""] * (
                        width - len(row)
                    )

                    table_lines.append(
                        "| " + " | ".join(padded) + " |"
                    )

                sections.append(
                    "\n".join(table_lines)
                )

    return "\n\n".join(sections), warnings


# =====================================
# 4. 统一文档解析入口
# =====================================

def load_document(
    relative_path: str,
    documents_root: Path,
) -> dict:
    """
    将 MD / TXT / DOCX 转换成统一的文档结构。

    当前只负责解析，不负责分块和入库。
    """

    path = resolve_document_path(
        documents_root,
        relative_path,
    )

    extension = path.suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        raise DocumentLoadError(
            "暂不支持该文件格式"
        )

    file_sha256 = calculate_sha256(path)

    if extension in {".md", ".txt"}:

        content, warnings = read_text_document(path)

    elif extension == ".docx":

        content, warnings = read_docx_document(path)

    else:
        raise DocumentLoadError(
            "未知的文件类型"
        )

    if not content.strip():
        raise DocumentLoadError(
            "文档没有可提取的正文"
        )

    if len(content) > MAX_CONTENT_CHARS:
        raise DocumentLoadError(
            "文档提取文本超过长度限制"
        )

    # 同一路径、相同内容具有稳定 ID。
    identity = (
        relative_path + "\0" + file_sha256
    )

    document_id = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:32]

    return {
        "document_id": document_id,
        "source": (
            "documents/" + relative_path
        ),
        "filename": path.name,
        "file_type": extension.lstrip("."),
        "sha256": file_sha256,
        "size_bytes": path.stat().st_size,
        "content": content,
        "content_chars": len(content),
        "page_number": None,
        "warnings": warnings,
    }
