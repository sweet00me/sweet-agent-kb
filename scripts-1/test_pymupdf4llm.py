
import os
from pathlib import Path

import pymupdf4llm


# =====================================
# 1. 文件与目录配置
# =====================================

PDF_PATH = Path(
    "/home/sweet/Workspace/paper/2310.05026v3.pdf"
)

STORAGE_ROOT = Path("/home/sweet/Storage")

OUTPUT_DIR = (
    STORAGE_ROOT
    / "KnowledgeBase/reports/pdf_diagnostics"
)

if not os.path.ismount(STORAGE_ROOT):
    raise RuntimeError("Storage 未挂载")

if not PDF_PATH.is_file():
    raise FileNotFoundError(PDF_PATH)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =====================================
# 2. 解析论文第三页
# =====================================

print("========== PyMuPDF4LLM 测试 ==========")
print("论文：", PDF_PATH.name)
print("解析页码：3")

# 注意：pages 使用从 0 开始的页码
markdown_text = pymupdf4llm.to_markdown(
    str(PDF_PATH),
    pages=[2],
    use_ocr=False,
    write_images=False
)


# =====================================
# 3. 保存解析结果
# =====================================

OUTPUT_FILE = (
    OUTPUT_DIR / "page03_pymupdf4llm.md"
)

OUTPUT_FILE.write_text(
    markdown_text,
    encoding="utf-8"
)


# =====================================
# 4. 输出检查信息
# =====================================

print("\n========== 解析统计 ==========")
print("Markdown 字符数：", len(markdown_text))
print("保存位置：", OUTPUT_FILE)

print("\n========== 前2500字符 ==========")
print(markdown_text[:2500])

print("\n========== 关键内容检查 ==========")

keywords = [
    "TABLE 1",
    "Convolutional Neural Networks",
    "Vision Transformers",
    "METHODOLOGY",
    "Low-Resolution Self-Attention"
]

for keyword in keywords:
    found = keyword.lower() in markdown_text.lower()
    print(f"{keyword}: {'找到' if found else '未找到'}")

print("\n测试完成")
