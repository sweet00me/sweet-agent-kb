
import argparse
from pathlib import Path

import pymupdf


# =====================================
# 1. 接收 PDF 路径
# =====================================

parser = argparse.ArgumentParser(
    description="测试科研论文 PDF 文本提取"
)

parser.add_argument(
    "pdf_path",
    help="需要解析的 PDF 文件路径"
)

parser.add_argument(
    "--pages",
    type=int,
    default=3,
    help="最多读取多少页，默认3页"
)

args = parser.parse_args()

PDF_PATH = Path(args.pdf_path).expanduser().resolve()

if not PDF_PATH.is_file():
    raise FileNotFoundError(PDF_PATH)

if PDF_PATH.suffix.lower() != ".pdf":
    raise ValueError("输入文件不是 PDF")

if args.pages < 1:
    raise ValueError("--pages 必须大于等于1")


# =====================================
# 2. 打开 PDF
# =====================================

with pymupdf.open(str(PDF_PATH)) as pdf:

    if pdf.needs_pass:
        raise RuntimeError("PDF 需要密码，暂不支持解析")

    print("========== PDF 基本信息 ==========")

    print("文件：", PDF_PATH.name)
    print("文件大小：", PDF_PATH.stat().st_size, "bytes")
    print("总页数：", len(pdf))

    metadata = pdf.metadata or {}

    print("标题：", metadata.get("title") or "未知")
    print("作者：", metadata.get("author") or "未知")

    # =================================
    # 3. 按页提取文本
    # =================================

    page_count = min(args.pages, len(pdf))
    total_chars = 0

    for page_num in range(page_count):

        page = pdf.load_page(page_num)

        # sort=True 尝试按阅读顺序排列文本
        text = page.get_text(
            "text",
            sort=True
        ).strip()

        total_chars += len(text)

        print(
            f"\n========== 第 {page_num + 1} 页 =========="
        )

        print("提取字符数：", len(text))

        if not text:
            print("未提取到文字，可能是扫描页或图片")
            continue

        print("\n文字预览：")
        print(text[:600])

    print("\n========== 提取统计 ==========")
    print("本次检查页数：", page_count)
    print("提取字符总数：", total_chars)

    if total_chars == 0:
        print("提示：可能需要 OCR 或其他解析方式")

print("\nPDF 文本提取测试结束")
