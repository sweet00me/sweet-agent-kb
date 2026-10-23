<!-- 文件说明：文件管理图片缩略图的改动清单、启动方式和离线验收记录。 -->
# 文件管理图片缩略图

图片文件左侧显示 56×56 圆角缩略图，使用 `object-fit: cover`；加载中及失败时显示原文件图标。文件夹和其他文件图标、名称、大小、修改时间、原预览及下载操作保持原样。

## 文件清单

| 文件 | 改动 |
| --- | --- |
| `app/services/file_thumbnail.py` | 新增受限解码、EXIF 方向处理、私有缓存及版本校验 |
| `app/api/files.py` | 新增 `/api/v1/files/thumbnail`，保留原有接口行为 |
| `frontend/src/components/files/FileBrowser.tsx` | 图片图标替换、可视区域加载、失败回退 |
| `frontend/src/services/files.ts` | 构建缩略图地址，编码根目录、相对路径和刷新版本 |
| `requirements-thumbnail.txt` | Pillow 固定版本；已在 `agent_kb` 安装 12.3.0 |
| `tests/test_file_thumbnail.py` | 新增 13 项隔离目录回归 |
| `frontend/tests/file-thumbnail-ui.mjs` | 真实 React 页面、Chromium/WebKit 回归，所有 API 指向隔离本地模拟服务 |
| `docs/file-thumbnails.md` | 本说明 |

## 接口与限制

`GET /api/v1/files/thumbnail?root_id=...&path=...`

- `root_id` 必须来自现有 `/api/v1/files/roots`，`path` 为文件相对路径。每次请求包括缓存命中、条件请求，均通过 `open_download()` 重新检查访问资格；沿用逐级目录描述符、`O_NOFOLLOW`、受保护路径及普通文件规则。
- 支持 JPG/JPEG/PNG/WebP/GIF/BMP，扩展名忽略大小写，实际格式由 Pillow 验证。GIF 仅取首帧；透明区域铺白，所有缩略图输出 `image/jpeg`，删除 EXIF/GPS 等元数据。
- 原图最多 32 MiB、4000 万像素；最长边 160，不放大小图。JPEG 优先降采样，每进程同时解码最多两张，等待生成名额最多 2 秒。大小及像素限制约束单次解码资源，不提供强制终止解码的进程级硬超时。
- 路径非法 400；访问禁止 403；不存在 404；源文件读取时发生修改 409；超限 413；不支持类型 415；损坏图片 422；生成繁忙 503。错误提供明确中文原因，前端回退图标，浏览仍可继续。
- 响应保留 `nosniff`、CSP sandbox、`no-referrer`。缓存采用 `private, no-cache` 和 ETag；浏览器复用前需重新验证，可返回 304。
- 缓存为 `~/.cache/sweet-agent-kb/thumbnails-v1/`，目录 0700、文件 0600，不在知识库文档扫描目录或 ChromaDB 中。文件身份、大小、纳秒级 mtime/ctime 参与缓存键，源文件改变即失效。原子写入；按生成时间淘汰，最多 1024 项、64 MiB、7 天。缓存无法使用时直接返回生成的小图。
- 保留每页 100 项、搜索最多 50 项；缩略图进入视窗外 200px 范围才设置图片地址，并使用 `loading="lazy"`、`decoding="async"`。刷新列表会更新地址版本并允许失败图片重试。
- 全尺寸预览仍遵循原接口能力和大小限制：PNG/JPG/JPEG 的图片预览保持不变；新增 WebP/GIF/BMP 缩略图不会改变其原预览支持情况。HEIC、SVG、RAW 等显示原图标，可下载。

## 启动

在项目根目录进入已有环境并安装增量依赖：

```bash
conda activate agent_kb
python -m pip install -r requirements-thumbnail.txt
```

重启当前后端服务使新路由生效。手动启动入口仍为：

```bash
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8010
```

前端开发启动：

```bash
cd frontend
npm run dev -- --host 0.0.0.0
```

现有生产部署沿用原启动方式，重新部署 `npm run build` 产物后刷新页面。

## 已运行的验证

1. Python 语法编译及修改模块导入通过；Pillow 12.3.0 的 WebP 编解码可用。
2. `python -m unittest tests.test_file_thumbnail tests.test_file_preview`：**25/25 通过**，覆盖 EXIF、16 MiB 源文件、小尺寸输出、PNG/WebP/JPEG/GIF/BMP、透明图、缓存复用/失效、条件请求、原路径安全规则、解码限流、损坏图片、文件变化及原预览下载。
3. 现有 `test_file_browser`、`test_file_search`、`test_file_download`、`test_file_upload` 的临时目录测试脚本：**全部通过**。
4. `npm test`：**38/38 通过**；`npm run lint`、`npm run build`：**通过**。
5. 真实 Chromium PC、Chromium iPhone 尺寸、WebKit iPhone 尺寸：**3/3 场景通过**。使用真实 App → 知识库 → 文件管理页面和 173 项模拟目录，验证加载占位、JPG/JPEG/PNG/WebP、损坏图片回退、普通文件/文件夹图标、两页浏览、滚动加载、搜索、名称/按钮预览、移动端全屏预览、下载和行宽。首次请求分别为 **4、3、3** 张小图；打开预览前原图请求数为 **0**。
6. 独立 FastAPI/uvicorn 临时目录的真实本地 HTTP 测试：临时 6000×4000、16 MiB JPEG 返回 **909 B、107×160、image/jpeg**；条件请求 **304**；原预览和下载与测试源文件 SHA256 一致。
7. 原分析算法及正式知识库 SQLite/ChromaDB 的 11 个受保护文件对比：大小、mtime、SHA256 均未变化。

后端快速复验（项目根目录）：

```bash
python -m py_compile app/services/file_thumbnail.py app/api/files.py tests/test_file_thumbnail.py
python -m unittest tests.test_file_thumbnail tests.test_file_preview
python -m tests.test_file_browser
python -m tests.test_file_search
python -m tests.test_file_download
python -m tests.test_file_upload
```

前端复验（`frontend/`）：

```bash
npm test
npm run lint
npm run build
node tests/file-thumbnail-ui.mjs
```

页面测试依赖本机已有 Playwright Core 及浏览器，可通过 `SWEET_PLAYWRIGHT_CORE_PATH` 指定安装路径。本次服务器缺少浏览器运行库，使用 `/tmp/sweet-agent-ui-qa/` 中的临时解包库及 WebKit 启动器完成测试，没有安装系统包。复现本次完整浏览器测试：

```bash
LD_LIBRARY_PATH=/tmp/sweet-agent-ui-qa/libs/usr/lib/x86_64-linux-gnu \
SWEET_PLAYWRIGHT_CORE_PATH=/tmp/sweet-agent-ui-qa/node_modules/playwright-core/index.mjs \
SWEET_WEBKIT_EXECUTABLE=/tmp/sweet-agent-ui-qa/webkit-run \
node tests/file-thumbnail-ui.mjs
```

页面测试使用模拟 API；实际缩略图接口另由真实 Pillow、临时文件和独立 HTTP 服务测试。WebKit 模拟不能替代 iPhone 真机验收；正式照片及现有生产服务未用于本次测试，部署后需在手机刷新确认。
