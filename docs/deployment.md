<!-- 文件说明：正式部署审计、系统安装步骤、当前执行状态、发布与回滚、验证范围和风险。 -->
# Sweet Research Agent 私人 Web 部署

**当前状态：5.1～5.5 已由用户执行并完成接管后的独立核验。后端 systemd active/enabled，PID 2827767，2026-10-22 20:40:16（Asia/Shanghai）启动，单 Worker、NRestarts=0；Nginx active/enabled，监听 127.0.0.1:8088。Tailscale Serve 已增加 Tailnet 内 HTTPS 443，原 HTTP 8080 和开发 Vite 5173 保留。**

正式地址：**[https://sweetserver.taile94715.ts.net/](https://sweetserver.taile94715.ts.net/)**。已使用真实证书验证 HTTPS Health 与首页，未跳过 TLS 校验；开发与正式页面仍共用同一后端及数据库。iPhone 保持 Tailscale 连接，在 Safari 打开该地址，分享 → 添加到主屏幕。手机操作及真实聊天 SSE/HITL 尚需在正常使用中验收。

以下首次接管命令仅作为执行记录，**无需再运行 sudo 激活命令或向旧进程发送信号**。日常前端更新见第 7 节；安全只读验收可以运行 `bash scripts/activate-production.sh --check` 和 `./scripts/deploy.sh --check`：

```bash
cd /home/sweet/Workspace/sweet-agent-kb
bash scripts/activate-production.sh --check
sudo bash scripts/activate-production.sh
```

`--check` 本机只读检查及 12 项离线模拟回归通过，用户随后实际执行了接管。助手在当前进程有新增未确认任务、Graph 忙、进程身份变化或 443 已有其他服务时拒绝接管。旧请求仅在更新时间早于当前进程启动、只读诊断可取得 Checkpoint 时识别为前进程遗留，保留原状态；这不是业务完成证明，也不放宽原 `check_runtime.py --quiescence` 的严格检查。

当前版本：`releases/20261022T122201Z-c73b76c6`，这是首个正式版本，暂无 previous。首页、主脚本、API Health、文件根目录及 `deploy.sh --check` 均已通过；发布日志确认 lint、前端 38/38 测试和 build 成功。安装后的站点、systemd 文件与项目源文件逐字节一致，静态目录权限符合设计。无需再次执行首次安装脚本。

用户运行环境与原 Serve 备份目录 0700、文件 0600；将新增 443 项去除后，Serve 配置与原备份完全一致，无 Funnel。用户终端完成了需要 sudo 的安装和接管；本轮核验仅执行只读检查，没有再次启停服务或改写 Serve。后续只有前端更新时，以 sweet 执行 `./scripts/deploy.sh`，不要加 sudo。

## 1. 只读环境审计

审计时间：2026-10-22 19:33（Asia/Shanghai）；进程和任务状态会变化，切换前必须重新检查。

下表是安装前快照，当前运行状态以本文顶部和第 6 节的正式验收结果为准。

| 项目 | 实际结果 |
| --- | --- |
| 系统 | Ubuntu 24.04.3 LTS，systemd 255 |
| 项目 | `/home/sweet/Workspace/sweet-agent-kb`，存在大量未提交及未跟踪文件，禁止用 reset/clean 整理 |
| Python | `/home/sweet/miniconda3/envs/agent_kb/bin/python`，实际解释器 `python3.11` |
| 后端 | PID 2724236，tmux scope，单 Worker，监听 `127.0.0.1:8010`；非 systemd Service |
| 开发前端 | PID 2723922，tmux scope，Vite 监听 `100.71.65.23:5173`；HTTP 页面返回 200 |
| 正式监听 | 8088、443 未占用；`nginx` 未安装，`nginx.service` 和建议的后端单元均不存在 |
| Health | `/api/v1/health` 返回 `status=ok, service=sweet-research-agent`；仅说明 HTTP 可用，不证明空闲 |
| 文件根目录 | 现有 `/api/v1/files/roots` 返回 200，两个入口 |
| Storage | `/dev/nvme0n1p4`，独立 ext4、rw，UUID `eb8b314c-2321-4737-ac39-cb08507691b8` |
| fstab | UUID 挂载，含 `nofail,x-systemd.device-timeout=10s`；生成单元 `home-sweet-Storage.mount` 活跃 |
| Tailscale | 1.102.4，`tailscaled.service` active/enabled；本机域名 `sweetserver.taile94715.ts.net` |
| 现有 Serve | **HTTP 8080 的 `/` 托管 `/home/sweet/Workspace/taidicup`**，必须保留；未配置 HTTPS 443 |
| 运行环境 | 原后端继承六个大小写代理变量，均指向 `http://127.0.0.1:7890`；对应 `clash.service` active/enabled |
| 密钥机制 | `app/agents/graph.py` 通过项目绝对路径 `load_dotenv(PROJECT_ROOT / '.env')` 加载；现有 `.env` 权限 0600 |
| 请求快照 | completed 52、pending_approval 13、running 1；**不能据此判断 running 是活任务还是旧记录，也不能直接切换** |

实际限制：后端上传文件最多 **50 MiB**；现有图片全尺寸预览最多 20 MiB，缩略图输入最多 32 MiB/4000 万像素；SSE 空闲心跳每 10 秒一次；主 Agent 模型请求超时 60 秒、最多一次重试。部署不改这些业务参数。

## 2. 目录与兼容性

```text
iPhone（Tailscale 已连接）
  → HTTPS sweetserver.taile94715.ts.net:443（Serve 已启用，Tailnet only）
  → 127.0.0.1:8088（Nginx，静态页面与 /api/ 代理）
  → 127.0.0.1:8010（唯一 FastAPI 单 Worker）

开发浏览器 → 100.71.65.23:5173（原 Vite 热更新）→ 同一个 8010
```

开发与正式 UI **共用一个后端、执行锁、SQLite、ChromaDB、审批状态及同步任务**。本次不复制、不迁移数据库，不启动第二个真实 Agent。`vite.config.ts` 和 Agent 业务代码未改。

5173 的 HTTP 地址与新 HTTPS 域名属于不同浏览器 origin，草稿和未确认请求的本地回执不会自动迁移。聊天历史、Checkpoint 和 request journal 在服务端保留；**换入口前先在旧页面确认未完成请求，不清理旧页面存储，不在新入口重复提交未确认的审批**。

正式静态文件仅放在 `/srv/sweet-research-agent/`：

```text
.sweet-static-root             # 授权标记，防止脚本误写其他目录
current -> releases/<版本ID>  # 原子切换
previous -> releases/<旧版本> # 快速回滚
releases/                     # 只包含通过检查的静态构建；旧版本保留
assets/                       # 共享不可变哈希资源，保留已打开页面需要的旧 JS/CSS/worker
.deploy-<ID>.log              # 0600 发布日志；不在静态根中
```

Nginx 只读取这份构建，不能把项目或 Storage 设置为静态根。发布根目录及 releases/assets 使用 `sweet:www-data,2750`，版本目录供组遍历，构建文件 0640；**不 chmod `/home/sweet`、Storage 或科研文件**。发布容器目录的 setgid 使 sweet 创建的文件继承 www-data 组，无需把 Nginx 加入 sweet 组。

## 3. 新增文件

- `deploy/systemd/sweet-research-agent.service`：单 Worker、挂载依赖、异常重启、journal、无限期优雅退出。
- `deploy/nginx/sweet-research-agent.conf`：回环监听、SPA、API/SSE、52 MiB 请求上限、缓存、MIME、静态路径保护。
- `deploy/runtime.env.example`：保留审计确认的本机代理，不包含 API Key 或云端开关。
- `deploy/check_runtime.py`：只读检查分区 UUID/rw、启动端口及请求状态。
- `deploy/frontend_release.py`、`scripts/deploy.sh`：前端快照构建、验证、发布、自动恢复和回滚。
- `scripts/install-production.sh`：已授权步骤 5.2 的首次安装入口，`--check` 仅只读；避免默认公网站点和其他服务的安装后自动重启。
- `scripts/activate-production.sh`、`deploy/activate_production.py`：已授权步骤 5.4～5.5 的精确进程核对、优雅接管和 HTTPS 验证；root 权限不足不会停下旧后端。
- `tests/test_production_activation.py`：只读与模拟接管回归，不操作真实服务或模型。
- `tests/test_deployment.py`、`tests/test_deployment_nginx.py`：部署及真实临时 Nginx 回归。
- `docs/deployment.md`：本手册。

## 4. 安装前再检查（只读）

```bash
cd /home/sweet/Workspace/sweet-agent-kb
ss -lntp
systemctl show nginx.service sweet-research-agent.service tailscaled.service clash.service \
  -p Id -p LoadState -p ActiveState -p UnitFileState -p MainPID
tailscale serve status --json
findmnt --mountpoint /home/sweet/Storage -o SOURCE,TARGET,FSTYPE,OPTIONS,UUID
/home/sweet/miniconda3/envs/agent_kb/bin/python deploy/check_runtime.py --quiescence
```

本次最后一项因一条 running 记录按设计返回非零；**不要删除、改写或自动重试这条记录以绕过检查**。在原聊天中检查执行状态及 request journal，区分正在执行、已完成但未确认或旧进程遗留。

安装后复核仍为 completed 52、pending_approval 13、running 1。该 review 请求最后更新于 2026-10-22 16:25:31（Asia/Shanghai）；只读诊断返回 `checkpoint_status=unfinished`、`diagnosis=requires_reconciliation`、`has_saved_response=false`、`automatic_replay_allowed=false`。这表明存在未确认请求，不能仅凭时间久就判定为安全空闲；尚未自动恢复、重试或修改其状态。

接管前只读核对：该请求的审批节点完成，但 `controlled_tools`、`analysis_map` 在 16:25 开始后没有完成审计，集合分析没有 execution completed 记录；接管前的后端进程于 19:16:47 才启动，且启动代码不自动恢复旧 Graph。因此它是前进程遗留的未完成任务，不能标记成功或自动重试。非阻塞诊断能读取 Checkpoint 只证明检查时刻可获得执行锁；切换仍依靠 Uvicorn 和 SSE executor 的优雅排空。接管后再次确认该请求仍为 running/unfinished、无保存响应，更新时间和最新 Checkpoint ID 均未变，没有自动重放。

检查器只看检查时刻的日志快照，不能阻止后续提交，也看不到未带 request_id 的请求或自动同步。必须与暂停所有客户端提交、优雅关闭旧进程一起使用。

## 5. 系统安装与接管（均已完成，以下保留部署记录）

### 5.1 保留环境与 Serve 配置

以下以 sweet 执行；文件已存在则停止检查，不覆盖：

**本步骤已执行，以下仅保留首次安装参考，请勿重新覆盖现有配置或备份。**

```bash
set -euo pipefail
cd /home/sweet/Workspace/sweet-agent-kb
umask 077
install -d -m 0700 /home/sweet/.config/sweet-research-agent
test ! -e /home/sweet/.config/sweet-research-agent/runtime.env
install -m 0600 deploy/runtime.env.example /home/sweet/.config/sweet-research-agent/runtime.env
test ! -e /home/sweet/.config/sweet-research-agent/serve.before.json
tailscale serve status --json > /home/sweet/.config/sweet-research-agent/serve.before.json
```

代理样例与本次审计一致；若切换时环境已变化，应人工核对后调整。systemd 不继承 tmux 环境，因此不能省略这一步后假定云端连接行为相同。业务密钥、预算和独立授权继续由原 `.env` 加载；**不要将 `.env` 改写为 systemd EnvironmentFile，也不要复制密钥进静态目录**。不修改或重启现有 Clash。

### 5.2 仅适用于当前“未安装 Nginx”的首次安装

**本步骤已由用户执行，以下作为首次安装记录，不要在当前主机重复执行。**

下面会创建系统文件，但暂不启动后端。若检查时已经存在 Nginx 配置或服务，停止并重新审查，不能套用此块覆盖。

推荐先以 sweet 运行 `bash scripts/install-production.sh --check`，再在本机终端运行 `sudo bash scripts/install-production.sh`。下面列出同等操作，供审阅；选择脚本或手工步骤之一即可，不要重复执行。

```bash
set -euo pipefail
cd /home/sweet/Workspace/sweet-agent-kb
test "$(systemctl show nginx.service -p LoadState --value)" = not-found
test ! -e /etc/nginx
test ! -e /etc/systemd/system/sweet-research-agent.service
test ! -e /srv/sweet-research-agent
if pgrep -x nginx >/dev/null; then echo '发现 Nginx 进程，停止安装'; exit 1; fi

# Ubuntu 安装 nginx 时可能自动启动默认公网 80 站点。先屏蔽启动，再安装和配置。
sudo systemctl mask nginx.service
sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
  apt-get install --yes --no-remove --no-install-recommends nginx

sudo install -d -o sweet -g www-data -m 2750 \
  /srv/sweet-research-agent \
  /srv/sweet-research-agent/releases \
  /srv/sweet-research-agent/assets
printf 'sweet-research-agent-static-v1\n' | sudo tee /srv/sweet-research-agent/.sweet-static-root >/dev/null
sudo chown sweet:www-data /srv/sweet-research-agent/.sweet-static-root
sudo chmod 0640 /srv/sweet-research-agent/.sweet-static-root

# 仅取消这次新安装产生、尚未运行的默认站点链接；保留原配置文件。
test "$(readlink /etc/nginx/sites-enabled/default)" = /etc/nginx/sites-available/default
sudo unlink /etc/nginx/sites-enabled/default
sudo install -o root -g root -m 0644 deploy/nginx/sweet-research-agent.conf \
  /etc/nginx/sites-available/sweet-research-agent.conf
sudo ln -s /etc/nginx/sites-available/sweet-research-agent.conf \
  /etc/nginx/sites-enabled/sweet-research-agent.conf
sudo nginx -t

sudo install -o root -g root -m 0644 deploy/systemd/sweet-research-agent.service \
  /etc/systemd/system/sweet-research-agent.service
sudo systemctl daemon-reload
# 只登记后端开机自启，绝不抢占仍由旧进程使用的 8010。
sudo systemctl enable sweet-research-agent.service

sudo systemctl unmask nginx.service
sudo systemctl enable --now nginx.service
```

预期影响：新增 Nginx，**只监听 127.0.0.1:8088**；原 Vite、FastAPI、Storage、Serve 8080 均保持运行。`NEEDRESTART_MODE=l` 让安装后的 needrestart 只列出建议，不自动重启其他服务。安装中断时 Nginx 可能保持 masked，修复配置并 `nginx -t` 通过后再 unmask，不能先启用默认站点。安装后检查 `ss -lntp`，不应新增公网 80/8088 监听。

### 5.3 发布首个前端

**本步骤已完成。日常更新与回滚见第 7、8 节。**

Nginx 此时可以代理现有 8010，首页在首次发布前可能为 404：

```bash
cd /home/sweet/Workspace/sweet-agent-kb
./scripts/deploy.sh
curl --noproxy '*' --fail http://127.0.0.1:8088/api/v1/health
curl --noproxy '*' --fail -I http://127.0.0.1:8088/
```

此步骤不停止或重启 FastAPI，也不修改 Vite 配置。

### 5.4 将唯一后端从 tmux 交给 systemd

**本步骤已完成，旧 PID 2724236 已退出；当前唯一后端为 systemd PID 2827767。不要重复停止当前实例。**

推荐使用上方 `sudo bash scripts/activate-production.sh` 完成 5.4～5.5，**不要再手动向同一个进程重复发送 Ctrl+C 或信号**。助手用已验证的 Ubuntu `/usr/bin/python3` 标准库运行，使用 pidfd 防止 PID 重用误伤；agent_kb 的 Python 构建缺少 pidfd 接口，因此不用于运行此助手。真正的后端和启动检查仍使用 agent_kb 的原解释器。

助手先验证 root、挂载、配置、代理环境、端口、进程用户/cwd/exe/参数及请求状态，再向精确识别的旧 PID 发出一次 SIGTERM。Uvicorn 按与首次 Ctrl+C 相同的正常关闭流程处理；脚本无限期等待连接、SSE 执行器和同步线程结束，不升级为强杀。旧实例退出和 8010 释放后才启动 systemd，比较启动前后的请求元数据与最新 Checkpoint 摘要，等待服务 PID 与实际监听匹配且 Health 就绪。已接管的实例重复运行不会重启后端。若就绪失败，停止后续 Serve 操作，保留 systemd 的正常异常恢复行为；人工查看 journal，不启动竞争实例。

以下为可选手动流程，不与助手重复执行：

1. 暂停 **正式 UI 和 5173 开发 UI 的所有新提交**。先核对上述 running 记录，不把 Health 当空闲证明；保留 13 条待审批记录及原 Checkpoint。
2. 重新查 `ss -lntp`、`/proc/<当前PID>/cmdline`、`/proc/<当前PID>/cwd`，确认 8010 确实属于本项目。文中 PID 只是审计快照，不能直接据此发信号。
3. 经用户确认，在原后端 tmux 窗口**仅按一次 Ctrl+C**，等待 Uvicorn 完成优雅退出和后台任务持久化。不要反复 Ctrl+C，不用 pkill、fuser -k 或 kill -9。若任务未结束，继续等并查看原日志。
4. 确认原进程退出、8010 已释放，再执行：

```bash
cd /home/sweet/Workspace/sweet-agent-kb
/home/sweet/miniconda3/envs/agent_kb/bin/python deploy/check_runtime.py --startup
sudo systemctl start sweet-research-agent.service
systemctl status sweet-research-agent.service --no-pager
journalctl -u sweet-research-agent.service -n 80 --no-pager
curl --noproxy '*' --fail http://127.0.0.1:8010/api/v1/health
curl --noproxy '*' --fail http://127.0.0.1:8088/api/v1/health
```

单后端切换会有短暂不可用期，不能承诺零停机。单元使用 `TimeoutStopSec=infinity`、`SendSIGKILL=no`，让已有 SSE 和 executor shutdown 等待任务完成；卡住时需要人工处理，不会靠强杀保证重启。服务 active 不是 readiness，必须等 Health 实际返回。

挂载保护有三层：`RequiresMountsFor` 拉起并排序挂载；`BindsTo`/`After` 在挂载消失时联动停止；启动前检查真实 UUID/ext4/rw。错误分区、只读或空挂载目录均拒绝启动。磁盘修复后必要时 `sudo systemctl reset-failed sweet-research-agent.service`，再人工启动。

### 5.5 增加独立 Tailscale HTTPS 入口

**本步骤已完成，443 仅代理本应用，原 Serve 8080 项目逐项保留。不要为了验收重新修改配置，使用 status 和只读 HTTPS 请求即可。**

接管助手会在 systemd 就绪后完成此步：添加前核对 Serve 快照及 443 冲突，只增加 443 代理；添加后严格比较原 Serve 配置，拒绝任何原 8080 变化或新入口的 Funnel 状态。使用 Tailnet IP 加 `curl --resolve` 校验真实证书、HTTPS Health 和正式首页，不跳过 TLS 验证；这不替代 iPhone 上的实际验收。若控制台要求先在 Tailscale 管理端启用 HTTPS，按实际提示处理，不能通过开放公网绕过。

再次查看 Serve 配置；**只有确认 443 仍未被使用、8080 仍是原项目，才执行**：

```bash
tailscale serve status --json
sudo tailscale serve --bg --https=443 http://127.0.0.1:8088
tailscale serve status
tailscale serve status --json
```

只增加 443 的 `/`，不运行 Funnel，不使用 reset/clear，不改变 Tailscale SSH、登录或基础连接。比较前后 JSON，确认原 `TCP.8080` 和 `Web["sweetserver.taile94715.ts.net:8080"]` 完整保留，且 `AllowFunnel` 无 true 项。`--bg` 的 Serve 设置会在设备或 Tailscale 重启后恢复。[Tailscale 官方说明](https://tailscale.com/docs/reference/tailscale-cli/serve#effects-of-rebooting-and-restarting)

已启用的正式地址是 **`https://sweetserver.taile94715.ts.net/`**。实际地址以 `tailscale serve status` 为准，也可只读取得本机名称：

```bash
tailscale status --json | /home/sweet/miniconda3/envs/agent_kb/bin/python -c \
  'import sys,json; print("https://"+json.load(sys.stdin)["Self"]["DNSName"].rstrip(".")+"/")'
```

在 iPhone 保持 Tailscale 连接，用 Safari 打开已验证地址，分享 → 添加到主屏幕。主屏幕图标是在线入口，本次不增加离线 Service Worker。验证文件列表、预览/缩略图和一次正常业务中的审批恢复；手机后台可能断开 SSE，应按原 request_id 恢复，不重新提交或重复批准。

## 6. 验收命令与实际结果

### 离线与隔离验证（无真实模型调用）

- Python 修改模块编译及导入；Bash 语法、ShellCheck 通过。
- 安装前检查：`install-production.sh` 的 Bash 语法、ShellCheck、真实主机 `--check` 均通过；非 root 安装按设计拒绝。apt 只读模拟结果为新增 nginx/nginx-common 两包，0 升级、0 删除。用户随后完成 sudo 安装和正式前端发布，实际结果单独列在下方正式验收中。
- 接管助手新增 **12 项离线回归通过**：旧任务不改状态、新任务/忙状态拒绝、SQLite 只读、PID 身份变化拒绝、仅一次 SIGTERM 且等待退出、先就绪再添加 HTTPS、启动失败/持久化变化不修改 Serve、原 8080 保留、端口冲突/Funnel 拒绝、重复执行不重启及非 root 拒绝。测试中的服务/信号均为模拟；不能用模拟测试代替真实接管验收。
- `systemd-analyze verify` 与现有生成的 Storage mount 联合验证通过；当前 UUID/rw 检查通过，8010 占用时按设计拒绝启动；请求日志存在 running 时按设计拒绝安全切换检查。
- **23 项部署回归通过**：构建/lint 失败不切换、失败恢复、第一版失败、资源碰撞/符号链接/敏感文件防护、并发发布锁、回滚、UUID/ro/端口/请求状态保护、真实临时 Nginx 配置与 HTTP 代理。
- Nginx 实测：SPA/缓存、JS 与 `.mjs`/WASM MIME、SSE 首事件在任务完成前到达、聊天/审批 POST 原样传递且不重放、文件浏览/搜索/上传/下载/Range 预览/缩略图/304/损坏图片、52 MiB 请求上限及静态敏感路径拒绝。
- 真实执行完整 `deploy.sh`，在**临时前端快照**内运行 `npm run lint`、`npm test`（**38/38**）、`npm run build`，发布到隔离 Nginx，随后成功回滚。开发 `public/pdfjs` 文件 mtime 全部未变。
- Chromium 390×844 页面实际通过生产构建 → Nginx → 临时 FastAPI，验证 56×56/object-fit 小图、原图片预览及下载。
- 现有 RAG/API/SSE、文件缩略图及预览离线回归 **37/37**；包括 HITL、断线后后台继续、request_id 重放、数据库提交后才发布 completed。测试禁止外网连接。
- 12 份受保护环境/业务代码（含 `.env`、Vite 配置）与部署前 SHA256 基线一致；未修改 Agent 算法或数据库内容来绕过检查。

### 正式环境只读验收（实际接管后）

| 验收项 | 实际结果 |
| --- | --- |
| 后端与 Nginx | 两者 active/enabled；唯一 8010 监听 PID 等于 systemd MainPID，sweet 用户、agent_kb Python、单 Worker 参数匹配；NRestarts=0 |
| Storage | 独立 ext4、预期 UUID、rw；已安装 unit 与实际生成的 mount 联合 verify 通过 |
| HTTP 与开发环境 | 8010 Health、8088 首页/Health/文件根目录均 200；原 Vite PID 2723922 及 5173 页面保留、200 |
| 私有 HTTPS | 真实证书校验、HTTPS Health 及正式首页字节匹配通过；通过 Tailnet IP 和正确 SNI 本机验证，未使用 insecure |
| Serve 原入口 | 新增 443 后原 8080 的 TCP/Web 配置逐项一致，无 Funnel；没有重置 Serve 或修改 SSH |
| 静态与安全配置 | `deploy.sh --check`、激活助手只读检查通过；首页 no-store；不可信 Origin 为 403，正式 Origin 为 200 |
| 持久化与来源 | completed 52、pending_approval 13、遗留 running 1；遗留更新时间与最新 Checkpoint ID 不变，不允许自动重放 |
| 启动日志 | 本次启动 journal 9 条，警告/错误优先级记录为 0，包含 Application startup complete；接管后模型调用审计新增 0 条 |

上述均未发起新的真实聊天、审批或全文分析；未下载或打印私人文档正文。运行状态与计数是验收时快照，正常使用后会变化。

复验：

```bash
cd /home/sweet/Workspace/sweet-agent-kb
python -m py_compile deploy/check_runtime.py deploy/frontend_release.py
python -c 'from deploy import check_runtime, frontend_release; import app.api.main; print("imports ok")'
bash -n scripts/deploy.sh
bash -n scripts/install-production.sh
bash -n scripts/activate-production.sh
bash scripts/activate-production.sh --check
/usr/bin/python3 -m unittest tests.test_production_activation
# 仅首次安装前适用；已安装的主机应拒绝再次首次安装：
# bash scripts/install-production.sh --check
systemd-analyze verify deploy/systemd/sweet-research-agent.service \
  /run/systemd/generator/home-sweet-Storage.mount
python -m unittest tests.test_deployment
```

本次未 sudo 安装验证工具，而是将 Ubuntu 软件包解包到 `/tmp/sweet-deployment-validation/runtime`。相同环境完整复验命令：

```bash
LD_LIBRARY_PATH=/tmp/sweet-agent-ui-qa/libs/usr/lib/x86_64-linux-gnu \
SWEET_TEST_PLAYWRIGHT=/tmp/sweet-agent-ui-qa/node_modules/playwright-core/index.mjs \
SWEET_TEST_NGINX=/tmp/sweet-deployment-validation/runtime/usr/sbin/nginx \
SWEET_TEST_NGINX_MIME=/tmp/sweet-deployment-validation/runtime/etc/nginx/mime.types \
/home/sweet/miniconda3/envs/agent_kb/bin/python -m unittest \
  tests.test_deployment tests.test_deployment_nginx
```

安装正式 Nginx 后的**只读**验收：

```bash
sudo nginx -t
systemctl is-enabled nginx.service sweet-research-agent.service tailscaled.service
systemctl is-active nginx.service sweet-research-agent.service tailscaled.service
ss -lntp
curl --noproxy '*' --fail http://127.0.0.1:8088/api/v1/health
curl --noproxy '*' --fail -I http://127.0.0.1:8088/
curl --noproxy '*' --fail http://127.0.0.1:8088/api/v1/files/roots
./scripts/deploy.sh --check
```

真实 systemd 启动和本机 Tailnet HTTPS 全链路已验收。**实际开机、崩溃后的自动重启、掉盘联动、外部 iPhone/Safari 主屏幕及真实聊天 SSE/HITL 尚未实测**；自启与异常重启配置已核对，没有为测试而重启服务器、停止健康服务、卸载磁盘或自动发起云端聊天/审批。手机 SSE/HITL 在用户下一次正常且已授权的业务中观察验收，不为测试额外调用模型。

## 7. 日常更新

仅前端更新，以 sweet 在项目根目录执行：

```bash
./scripts/deploy.sh
./scripts/deploy.sh --check
```

脚本先检查依赖和本机 API，通过发布锁创建前端源文件快照，再运行 lint → 离线前端测试 → 生产构建；既不 npm ci，也不触碰开发监听目录中的 PDF.js 生成资源。校验静态白名单、补齐哈希资源池后才原子更换 current；首页或 API 健康不符会恢复旧链接。**不 reload Nginx，不 restart FastAPI，不操作 Serve。**日志路径在失败时显示，旧版本始终保留。

依赖变更要人工审阅 lockfile 后在合适的开发维护窗口安装；不要为了每次发布执行会删除开发 node_modules 的 npm ci。前端快照不复制 backend 或数据库。非哈希 `/pdfjs` 资源随 current，未来升级 PDF.js 依赖后，已打开的旧 PDF 页面应刷新。

后端更新则需单独安排：暂停两种 UI 的提交 → 核对请求/审批及检查点 → 等现有任务结束 → 更新经过测试的后端文件 → **经确认**执行 `sudo systemctl restart sweet-research-agent.service` → 查 journal、Health、原请求恢复状态。不要在长任务中途混合改写懒加载的后端代码；不自动启用云端开关。实际崩溃恢复后检查原 request_id，Checkpoint 不保证外部工具恰好执行一次。

日志：`journalctl -u sweet-research-agent.service -f`；现有业务自己的审计/trace 日志继续按原位置保存。journal 与 Nginx 错误日志可能含私人路径或业务信息，仅保留本机受限权限，不公开下载。

## 8. 回滚与风险

前端回滚不需要 sudo，不重启 Agent：

```bash
./scripts/deploy.sh --list
./scripts/deploy.sh --rollback
./scripts/deploy.sh --rollback <列表中的版本ID>
./scripts/deploy.sh --check
```

脚本校验回滚版本及健康，不删除旧构建或资源池。失败/普通 SIGTERM 会尝试恢复指针；SIGKILL、断电或文件系统故障不能保证自动执行恢复，重启后用保留的 previous/历史版本人工回滚。不自动清理历史版本，需留意 `/srv` 空间。

若仅撤销**本次新建且经确认的 443 Serve 入口**，先核实它仍指向本应用，再执行：

```bash
sudo tailscale serve --https=443 off
tailscale serve status --json
```

这不是整个 Serve 的 reset，应保留原 8080 项目。后端若需退回旧代码，先排空并优雅停止新服务，再恢复审查过的代码/启动方式；不回滚或删除 SQLite、ChromaDB、缓存或 `.env`，不把未提交文件整体 reset。任何系统服务停止及旧启动方式恢复仍需用户确认。

安全边界：

- 应用目前没有独立多用户认证，**Tailnet 可达不等于只有你本人可达**。需在 Tailscale 管理端核对现有 grants/ACL，将节点 443 限制给可信用户/设备；本次不擅自修改 ACL、SSH 或节点基础设置。现有 5173/8080 仍保留其原可达范围。
- Nginx 的 Origin 检查降低浏览器跨站请求风险，不是身份认证，不能替代 Tailnet 访问控制。更换正式域名/HTTPS 端口时需一并审查 Origin 白名单。
- 不在 `VITE_*` 放 API Key；前端构建本来就可被浏览器读取。静态白名单和 Nginx 拒绝源码/数据库路径，不能把业务密钥变成“可以放到 public 中”。
- 所有 UI 共用正式后端和数据；开发操作同样可能修改正式历史或文件。离线测试必须继续使用临时存储和模拟工具。
- 当前设计是单机、单 Worker，后台任务异常恢复依赖已有 journal/Checkpoint，不通过并行启动真实后端实现零停机。

配置依据：[systemd 挂载依赖](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#RequiresMountsFor=)、[Nginx 代理缓冲及超时](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。本机实际使用 systemd 255、Nginx 1.24 验证过本项目配置；代理读取超时按两次读取之间的空闲时间计算，10 秒 SSE 心跳无需等到整个任务结束才返回。
