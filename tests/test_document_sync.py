"""增量解析离线测试：仅使用临时目录。"""
from pathlib import Path
from tempfile import TemporaryDirectory

from app.rag.document_sync import sync_storage


def main():
    with TemporaryDirectory() as temp:
        root = Path(temp) / 'Storage'
        root.mkdir()
        out = root / 'KnowledgeBase' / 'parsed_documents'
        db = root / 'KnowledgeBase' / 'sqlite' / 'document_sync.sqlite3'
        doc = root / 'PC_Files' / 'Uploads' / 'note.md'
        doc.parent.mkdir(parents=True)
        doc.write_text('# 初稿\n你好', encoding='utf-8')

        preview = sync_storage(root, out, db, commit=False)
        assert preview['new'] == 1 and not db.exists()
        print('PASS：预览只读，不创建数据库')

        first = sync_storage(root, out, db, commit=True)
        assert first['new'] == 1 and first['failed'] == 0
        assert len(list(out.glob('*.json'))) == 1
        print('PASS：新增文档解析入缓存')

        second = sync_storage(root, out, db, commit=True)
        assert second['unchanged'] == 1
        print('PASS：内容未改变自动跳过')

        doc.write_text('# 新版\n你好世界', encoding='utf-8')
        updated = sync_storage(root, out, db, commit=True)
        assert updated['modified'] == 1 and updated['failed'] == 0
        print('PASS：内容修改触发重新解析')

        doc.unlink()
        missing = sync_storage(root, out, db, commit=False)
        assert missing['missing'] == 1
        print('PASS：文件删除只标记缺失，不自动删除缓存')

    print('\n增量解析测试全部通过')


if __name__ == '__main__':
    main()
