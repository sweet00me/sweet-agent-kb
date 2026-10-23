#!/usr/bin/env bash
# 首次安装正式服务（手册 5.2）：仅安装 Nginx、创建静态目录、登记后端开机自启。
# 不启动/重启现有后端，不操作 Vite、Tailscale Serve 或数据库；前端由 sweet 另行发布。
# 只适用于尚未安装 Nginx 的已审计主机。--check 只读检查；中途失败须先核查，不强制覆盖重跑。
set -Eeuo pipefail
umask 027
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

install_project=/home/sweet/Workspace/sweet-agent-kb
install_config=/home/sweet/.config/sweet-research-agent
install_static=/srv/sweet-research-agent
install_python=/home/sweet/miniconda3/envs/agent_kb/bin/python
install_check=0

fail() { printf '安装未完成：%s\n' "$*" >&2; exit 1; }

case "${1:-}" in
    --check) install_check=1 ;;
    --help|-h)
        printf '用法：sudo bash scripts/install-production.sh\n只读检查：bash scripts/install-production.sh --check\n'
        exit 0 ;;
    '') ;;
    *) fail '不支持的参数；使用 --help 查看用法' ;;
esac
(( $# <= 1 )) || fail '不支持多个参数'
if (( ! install_check && EUID != 0 )); then
    fail '需要本机 sudo 验证；请以 sweet 在终端运行 sudo bash scripts/install-production.sh'
fi

preflight() {
    local dependency directory site
    for dependency in systemctl systemd-analyze ss pgrep findmnt install \
        apt-get readlink stat id getent cmp tailscale runuser flock curl awk; do
        command -v "$dependency" >/dev/null || fail "缺少依赖：$dependency"
    done
    [[ "$(readlink -f -- "${BASH_SOURCE[0]}")" == "$install_project/scripts/install-production.sh" ]] || fail '脚本位置与审计项目不符'
    [[ "$(readlink -f -- "$install_project")" == "$install_project" ]] || fail '项目路径不能经由符号链接'
    [[ -x "$install_python" ]] || fail '找不到审计确认的 agent_kb Python'
    id sweet >/dev/null
    getent group www-data >/dev/null
    for site in nginx.service sweet-research-agent.service; do
        [[ "$(systemctl show "$site" -p LoadState --value)" == not-found ]] || fail "$site 已存在，不能覆盖"
    done
    for directory in /etc/nginx /etc/systemd/system/sweet-research-agent.service "$install_static"; do
        [[ ! -e "$directory" && ! -L "$directory" ]] || fail "$directory 已存在，停止首次安装"
    done
    if command -v nginx >/dev/null || pgrep -x nginx >/dev/null; then
        fail '发现现有 Nginx，不能套用首次安装流程'
    fi
    [[ -z "$(ss -Hlt 'sport = :8088')" ]] || fail '8088 已被占用，不会停止占用进程'
    [[ "$(readlink -f -- "$install_config")" == "$install_config" ]] || fail '用户运行配置路径不安全'
    [[ "$(stat -c '%a:%u' "$install_config")" == "700:$(id -u sweet)" ]] || fail '先按手册 5.1 创建私有配置目录'
    for site in runtime.env serve.before.json; do
        [[ -f "$install_config/$site" && ! -L "$install_config/$site" ]] || fail "缺少安全的 $site"
        [[ "$(stat -c '%a:%u' "$install_config/$site")" == "600:$(id -u sweet)" ]] || fail "$site 应由 sweet 拥有且权限为 0600"
    done
    cmp -s "$install_project/deploy/runtime.env.example" "$install_config/runtime.env" || fail '代理环境与审计样例不同，请先重新核对'
    cmp -s <(tailscale serve status --json) "$install_config/serve.before.json" || fail 'Serve 与备份不同，请先重新核对；不会改写现有配置'
    for site in deploy/systemd/sweet-research-agent.service deploy/nginx/sweet-research-agent.conf; do
        [[ -f "$install_project/$site" && ! -L "$install_project/$site" ]] || fail "部署配置不存在或是链接：$site"
    done
    if (( EUID == 0 )); then
        runuser -u sweet -- "$install_python" -I "$install_project/deploy/check_runtime.py"
    else
        "$install_python" -I "$install_project/deploy/check_runtime.py"
    fi
    systemd-analyze verify "$install_project/deploy/systemd/sweet-research-agent.service" \
        /run/systemd/generator/home-sweet-Storage.mount
    curl --noproxy '*' --fail --silent --show-error --max-time 5 --output /dev/null \
        http://127.0.0.1:8010/api/v1/health
}

preflight
if (( install_check )); then
    printf '首次安装只读检查通过；未安装软件、写入系统目录或操作任何服务。\n'
    exit 0
fi

[[ ! -L /run/lock/sweet-research-agent-install.lock ]] || fail '安装锁不能是符号链接'
exec 9>/run/lock/sweet-research-agent-install.lock
flock -n 9 || fail '已有首次安装进行中'
preflight
trap 'printf "首次安装中断；请检查已完成步骤。Nginx 配置验证前保持 masked，不自动启动后端或撤销其他服务。\n" >&2' ERR

# 安装包可能启动默认公网 80 站点：先 mask；needrestart 仅列出，不重启其他服务。
systemctl mask nginx.service
DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install --yes --no-remove --no-install-recommends nginx

[[ ! -e "$install_static" && ! -L "$install_static" ]] || fail '安装期间静态目录已出现，不覆盖'
install -d -o sweet -g www-data -m 2750 "$install_static" "$install_static/releases" "$install_static/assets"
printf 'sweet-research-agent-static-v1\n' > "$install_static/.sweet-static-root"
chown sweet:www-data "$install_static/.sweet-static-root"
chmod 0640 "$install_static/.sweet-static-root"

# 仅摘除此新安装产生的默认站点链接；不删除默认配置，也不接管其他站点。
shopt -s nullglob dotglob
for install_site in /etc/nginx/sites-enabled/*; do
    [[ "$install_site" == /etc/nginx/sites-enabled/default && -L "$install_site" && \
        "$(readlink -- "$install_site")" == /etc/nginx/sites-available/default ]] || fail '发现非预期启用站点，Nginx 保持 masked'
    unlink -- "$install_site"
done
[[ ! -e /etc/nginx/sites-available/sweet-research-agent.conf && ! -L /etc/nginx/sites-available/sweet-research-agent.conf ]] || fail '站点文件已存在，不覆盖'
install -o root -g root -m 0644 "$install_project/deploy/nginx/sweet-research-agent.conf" \
    /etc/nginx/sites-available/sweet-research-agent.conf
ln -s /etc/nginx/sites-available/sweet-research-agent.conf /etc/nginx/sites-enabled/sweet-research-agent.conf
nginx -t
install_listeners="$(nginx -T 2>/dev/null | awk '/^[[:space:]]*listen[[:space:]]/ { print $0 }')"
[[ "$install_listeners" =~ ^[[:space:]]*listen[[:space:]]+127\.0\.0\.1:8088\;[[:space:]]*$ ]] || fail '有效配置不止 127.0.0.1:8088 监听，Nginx 保持 masked'

[[ "$(systemctl show sweet-research-agent.service -p LoadState --value)" == not-found && \
    ! -e /etc/systemd/system/sweet-research-agent.service && ! -L /etc/systemd/system/sweet-research-agent.service ]] || fail '安装期间后端服务已出现，不覆盖'
install -o root -g root -m 0644 "$install_project/deploy/systemd/sweet-research-agent.service" \
    /etc/systemd/system/sweet-research-agent.service
systemctl daemon-reload
systemctl enable sweet-research-agent.service
# 刻意不对后端使用 --now，也不发送任何进程信号。
systemctl unmask nginx.service
systemctl enable --now nginx.service
[[ "$(systemctl show sweet-research-agent.service -p MainPID --value)" == 0 ]] || fail '后端出现非预期 systemd 进程，请人工检查'
curl --noproxy '*' --fail --silent --show-error --max-time 5 --output /dev/null \
    http://127.0.0.1:8088/api/v1/health
cmp -s <(tailscale serve status --json) "$install_config/serve.before.json" || fail 'Serve 在安装期间发生变化，请人工检查；脚本未修改它'
printf '首次安装完成：Nginx 127.0.0.1:8088；后端仅登记开机自启，当前实例保持运行。\n'
printf '接着以 sweet（不加 sudo）执行：cd %s && ./scripts/deploy.sh && ./scripts/deploy.sh --check\n' "$install_project"
