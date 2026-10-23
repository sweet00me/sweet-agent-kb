#!/usr/bin/env bash
# 前端正式发布入口：检查、lint、测试、构建、原子切换和失败回滚；不操作 systemd 或 Tailscale。
# 首次管理员配置 /srv 静态目录后，以 sweet 运行。详见 docs/deployment.md。
set -Eeuo pipefail
umask 027

deploy_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_python="${SWEET_DEPLOY_PYTHON:-/home/sweet/miniconda3/envs/agent_kb/bin/python}"
if [[ ! -x "$deploy_python" ]]; then
    printf '找不到可执行的 agent_kb Python：%s\n' "$deploy_python" >&2
    exit 1
fi
exec "$deploy_python" "$deploy_script_dir/../deploy/frontend_release.py" "$@"
