
import os
from pathlib import Path

import pymupdf


# =====================================
# 1. 基础路径
# =====================================

PDF_PATH = Path(
    "/home/sweet/Workspace/paper/2310.05026v3.pdf"
)

STORAGE_ROOT = Path("/home/sweet/Storage")

OUTPUT_DIR = (
    STORAGE_ROOT
    / "KnowledgeBase/reports/pdf_diagnostics"
)

# 检查 Storage 挂载
if not os.path.ismount(STORAGE_ROOT):
    raise RuntimeError("Storage 未挂载")

if not PDF_PATH.is_file():
    raise FileNotFoundError(PDF_PATH)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =====================================
# 2. 打开论文，选择第 3 页
# =====================================

with pymupdf.open(PDF_PATH) as pdf:

    PAGE_NUMBER = 3

    page = pdf[PAGE_NUMBER - 1]

    # 页面大小，单位是 PDF point
    rect = page.rect

    width = rect.width
    height = rect.height

    print("========== 页面信息 ==========")
    print("页码：", PAGE_NUMBER)
    print("页面宽度：", width)
    print("页面高度：", height)

    # =================================
    # 3. 原始文本提取
    # =================================

    original_text = page.get_text(
        "text",
        sort=True
    )

    # =================================
    # 4. 分左右两栏提取
    # =================================

    middle_x = width / 2

    left_rect = pymupdf.Rect(
        0, 0,
        middle_x, height
    )

    right_rect = pymupdf.Rect(
        middle_x, 0,
        width, height
    )

    left_text = page.get_text(
        "text",
        clip=left_rect,
        sort=True
    )

    right_text = page.get_text(
        "text",
        clip=right_rect,
        sort=True
    )

    combined_text = (
        left_text.strip()
        + "\n\n"
        + right_text.strip()
    )

    # =================================
    # 5. 保存两种解析结果
    # =================================

    original_file = (
        OUTPUT_DIR / "page03_original.txt"
    )

    columns_file = (
        OUTPUT_DIR / "page03_two_columns.txt"
    )

    original_file.write_text(
        original_text,
        encoding="utf-8"
    )

    columns_file.write_text(
        combined_text,
        encoding="utf-8"
    )

    # =================================
    # 6. 输出对比
    # =================================

    print("\n========== 字符数 ==========")
    print("原始提取：", len(original_text))
    print("左栏：", len(left_text))
    print("右栏：", len(right_text))
    print("双栏合并：", len(combined_text))

    print("\n========== 原始提取前800字符 ==========")
    print(original_text[:800])

    print("\n========== 左栏前800字符 ==========")
    print(left_text[:800])

    print("\n========== 右栏前800字符 ==========")
    print(right_text[:800])

    print("\n========== 保存路径 ==========")
    print(original_file)
    print(columns_file)

print("\nPDF 双栏对照测试完成")
