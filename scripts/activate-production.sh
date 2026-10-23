#!/usr/bin/env bash
# 正式接管入口：先在本机完成 sudo 验证，再优雅移交唯一后端并增加 Tailnet HTTPS。
# --check 为只读检查；不重放遗留任务，不修改数据库、Vite 或原 Serve 8080 入口。
# 助手仅用标准库，使用 Ubuntu Python 的 pidfd 接口；后端仍使用 agent_kb Python。
set -Eeuo pipefail
umask 077
if [[ "${1:-}" != --check && $EUID -ne 0 ]]; then
    printf '尚未切换：请在 SSH 终端运行 sudo bash scripts/activate-production.sh（密码只在终端输入）。\n' >&2
    exit 1
fi
activate_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 -I "$activate_dir/../deploy/activate_production.py" "$@"
