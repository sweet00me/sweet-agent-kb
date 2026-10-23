"""
文件：app/rag/document_chunking.py
用途：将 document_sync.py 保存的统一解析 JSON 进行确定性分块。

特点：
- 保留 Markdown/Word 标题层级（heading_path）。
- 优先按段落、表格、代码块切分，超长块才按文本边界拆分。
- 同一章节相邻文本块可有有限重叠，严格遵守 max_chars。
- 基于文档 ID、块顺序和块内容生成稳定 Chunk ID。
- 不调用云端模型、不写 SQLite 或 ChromaDB；不输出正文到终端。

限制：
- 不负责修正解析阶段已经丢失的公式、图片或表格结构。
- 对大型表格、代码块的拆分提供警告，需要后续质量门禁处理。
"""

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Iterator

DEFAULT_MAX_CHARS = 1000
DEFAULT_OVERLAP_CHARS = 120
DEFAULT_STATE_DB = Path('/home/sweet/Storage/KnowledgeBase/sqlite/document_sync.sqlite3')

_HEADER_RE = re.compile(r'^(#{1,6})[ \t]+(.+?)\s*$')
_FENCE_RE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')


def _units(text: str) -> Iterator[tuple[str, str]]:
    """按 Markdown 块输出 ('heading'|'body', text)，不在围栏代码内部按空行拆分。"""
    lines: list[str] = []
    fence_char = None
    fence_length = 0

    def flush():
        nonlocal lines
        if lines:
            content = '\n'.join(lines).strip()
            lines = []
            if content:
                return ('body', content)
        return None

    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        match = _FENCE_RE.match(line)
        if match:
            marks = match.group(1)
            if fence_char is None:
                previous = flush()
                if previous:
                    yield previous
                fence_char, fence_length = marks[0], len(marks)
                lines.append(line)
            elif marks[0] == fence_char and len(marks) >= fence_length:
                lines.append(line)
                previous = flush()
                if previous:
                    yield previous
                fence_char = None
                fence_length = 0
            else:
                lines.append(line)
            continue

        if fence_char is None:
            header = _HEADER_RE.match(line)
            if header:
                previous = flush()
                if previous:
                    yield previous
                yield ('heading', line.strip())
                continue
            if not line.strip():
                previous = flush()
                if previous:
                    yield previous
                continue

        lines.append(line)

    previous = flush()
    if previous:
        yield previous


def _split_long(text: str, limit: int) -> list[str]:
    """将超长块按换行/空白边界拆分；没有合适边界时按字符截断。"""
    result = []
    remaining = text
    while len(remaining) > limit:
        # 优先使用较靠近结尾的自然边界，避免生成大量极短块。
        start = max(1, int(limit * 0.55))
        chunk_area = remaining[:limit + 1]
        choices = [chunk_area.rfind('\n', start, limit + 1),
                   chunk_area.rfind(' ', start, limit + 1),
                   chunk_area.rfind('\t', start, limit + 1)]
        cut = max(choices)
        if cut <= 0:
            cut = limit
        else:
            # 保留边界空白，不丢失非空白字符。
            cut += 1
        result.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        result.append(remaining)
    return result


def build_document_chunks(document: dict, *, max_chars: int = DEFAULT_MAX_CHARS,
                          overlap_chars: int = DEFAULT_OVERLAP_CHARS) -> list[dict]:
    """纯函数：输入已解析文档 dict，返回确定性的 Chunk 列表。"""
    if not isinstance(max_chars, int) or not 80 <= max_chars <= 10000:
        raise ValueError('max_chars 必须在 80～10000 之间')
    if not isinstance(overlap_chars, int) or not 0 <= overlap_chars < max_chars // 2:
        raise ValueError('overlap_chars 必须为非负数且小于 max_chars 的一半')
    required = ('document_id', 'source', 'sha256', 'file_type', 'content')
    if not isinstance(document, dict) or any(not isinstance(document.get(k), str)
                                             for k in required):
        raise ValueError(f'文档缺少必需字段：{required}')
    text = document['content']
    if not text.strip():
        raise ValueError('文档正文为空')
    if document['file_type'] not in ('md', 'txt', 'docx'):
        raise ValueError('当前分块器仅支持 md、txt、docx')
    warnings = document.get('warnings') or []
    if not isinstance(warnings, list):
        raise ValueError('warnings 必须是列表')

    # 预留重叠量：单个基础 Chunk 最大仅为 max_chars - overlap_chars。
    target = max_chars - overlap_chars
    heading_stack: list[str] = []
    bases: list[tuple[str, str, list[str]]] = []
    accumulator: list[str] = []
    accumulator_heading = ''
    accumulator_warnings: list[str] = []

    def flush():
        nonlocal accumulator, accumulator_warnings
        if accumulator:
            value = '\n\n'.join(accumulator)
            bases.append((value, accumulator_heading, list(dict.fromkeys(accumulator_warnings))))
            accumulator = []
            accumulator_warnings = []

    for kind, value in _units(text):
        if kind == 'heading':
            match = _HEADER_RE.match(value)
            if not match:
                continue
            level = len(match.group(1))
            new_stack = heading_stack[:level - 1]
            while len(new_stack) < level - 1:
                new_stack.append('')
            new_stack.append(match.group(2).strip())
            new_heading = ' / '.join(part for part in new_stack if part)
            if accumulator and new_heading != accumulator_heading:
                flush()
            heading_stack = new_stack
            current_heading = new_heading
        else:
            current_heading = ' / '.join(part for part in heading_stack if part)

        extra_warnings = []
        if len(value) > target:
            if value.lstrip().startswith('|'):
                extra_warnings.append('OVERSIZED_TABLE_SPLIT')
            if value.lstrip().startswith(('```', '~~~')):
                extra_warnings.append('OVERSIZED_CODE_SPLIT')
        pieces = _split_long(value, target)

        for piece in pieces:
            if accumulator and (accumulator_heading != current_heading or
                                len('\n\n'.join(accumulator)) + 2 + len(piece) > target):
                flush()
            if not accumulator:
                accumulator_heading = current_heading
            accumulator.append(piece)
            accumulator_warnings.extend(extra_warnings)

    flush()
    if not bases:
        raise ValueError('没有产生有效文本块')

    result = []
    previous_body = ''
    previous_heading = None

    for index, (body, heading, local_warnings) in enumerate(bases):
        prefix = ''
        if overlap_chars and previous_body and previous_heading == heading:
            # 固定最多 overlap_chars，保证完整 Chunk 不超过 max_chars。
            n = min(overlap_chars, max_chars - len(body) - 1, len(previous_body))
            if n > 0:
                prefix = previous_body[-n:]
        content = f'{prefix}\n{body}' if prefix else body
        if len(content) > max_chars:
            raise AssertionError('Chunk 长度超过上限')

        identifier = hashlib.sha256(
            f'{document["document_id"]}\0{index}\0{content}'.encode('utf-8')
        ).hexdigest()[:32]
        combined_warnings = list(dict.fromkeys([*warnings, *local_warnings]))
        result.append({
            'id': identifier,
            'document_id': document['document_id'],
            'source': document['source'],
            'file_type': document['file_type'],
            'file_sha256': document['sha256'],
            'chunk_index': index,
            'heading_path': heading,
            'content': content,
            'content_chars': len(content),
            'overlap_chars': len(prefix),
            'warnings': combined_warnings,
            'review_required': bool(combined_warnings),
        })
        previous_body, previous_heading = body, heading

    return result


def preview_cached_documents(state_db: Path, *, max_chars: int = DEFAULT_MAX_CHARS,
                             overlap_chars: int = DEFAULT_OVERLAP_CHARS) -> dict:
    """只读预览已经成功解析的缓存，统计分块数量与质量警告。"""
    state_db = Path(state_db)
    if not state_db.is_file():
        raise FileNotFoundError(f'未找到增量解析状态数据库：{state_db}')
    connection = sqlite3.connect(f'file:{state_db}?mode=ro', uri=True)
    try:
        rows = connection.execute(
            'SELECT source_path, parsed_path FROM parsed_documents ORDER BY source_path'
        ).fetchall()
    finally:
        connection.close()

    filetypes = Counter()
    total_chunks = 0
    reviewed = 0
    failures = []
    for source, path in rows:
        try:
            record = json.loads(Path(path).read_text(encoding='utf-8'))
            if record.get('source') != source:
                raise ValueError('解析缓存的来源与 SQLite 不一致')
            chunks = build_document_chunks(record, max_chars=max_chars,
                                           overlap_chars=overlap_chars)
            total_chunks += len(chunks)
            filetypes[record['file_type']] += 1
            reviewed += sum(chunk['review_required'] for chunk in chunks)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            failures.append({'source': source, 'reason': f'{type(exc).__name__}: {exc}'})
    return {'documents': len(rows), 'chunk_total': total_chunks,
            'file_types': dict(sorted(filetypes.items())),
            'chunks_requiring_review': reviewed,
            'failed_documents': failures, 'preview_only': True}


def main():
    parser = argparse.ArgumentParser(description='Storage 文档分块：默认只读预览')
    parser.add_argument('--state-db', type=Path, default=DEFAULT_STATE_DB)
    parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument('--overlap-chars', type=int, default=DEFAULT_OVERLAP_CHARS)
    args = parser.parse_args()
    result = preview_cached_documents(args.state_db, max_chars=args.max_chars,
                                      overlap_chars=args.overlap_chars)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
