"""部署只读检查：核对真实 Storage 分区、启动端口和请求状态；不导入或执行 Agent。

--startup 用于 systemd 启动前保护；--quiescence 仅是检查时刻的请求日志快照，
不能替代停止新提交和优雅退出，也不能证明没有无 request_id 的请求或正在自动同步。
"""

import argparse
import json
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

STORAGE = Path("/home/sweet/Storage")
EXPECTED_UUID = "eb8b314c-2321-4737-ac39-cb08507691b8"


def check_mount(storage=STORAGE, expected_uuid=EXPECTED_UUID):
    if not storage.is_absolute() or storage.resolve() != storage or not os.path.ismount(storage):
        raise RuntimeError("Storage 不是预期的独立真实挂载点，拒绝启动")
    result = subprocess.run(
        ["/usr/bin/findmnt", "--mountpoint", str(storage), "--json", "-o", "TARGET,FSTYPE,OPTIONS,UUID"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    mounts = json.loads(result.stdout).get("filesystems", [])
    if len(mounts) != 1:
        raise RuntimeError("无法唯一确认 Storage 挂载")
    info = mounts[0]
    if (info.get("target") != str(storage) or info.get("uuid") != expected_uuid
            or info.get("fstype") != "ext4" or "rw" not in info.get("options", "").split(",")):
        raise RuntimeError("Storage UUID、文件系统或读写状态不符，拒绝启动")
    print("Storage 独立 ext4 分区 UUID 及读写状态：通过")


def check_port(host="127.0.0.1", port=8010):
    with socket.socket() as listener:
        try:
            listener.bind((host, port))
        except OSError as exc:
            raise RuntimeError("8010 已占用；请确认旧实例空闲并优雅退出，不会自动停止任何进程") from exc
    print("后端端口可绑定：通过")


def check_quiescence(database=STORAGE / "KnowledgeBase/sqlite/agent_sessions.sqlite3"):
    # 不使用业务 connect_db，避免初始化或修改现有数据库。
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
        connection.execute("PRAGMA query_only=ON")
        counts = dict(connection.execute("SELECT status, COUNT(*) FROM api_requests GROUP BY status"))
    print("请求状态快照：" + json.dumps(counts, ensure_ascii=False, sort_keys=True))
    if any(counts.get(status, 0) for status in ("accepted", "running", "uncertain")):
        raise RuntimeError("有执行中或结果未确认记录；先核对原请求，不删除、不重试、不据此自动重启")
    print("日志快照未发现执行中记录；仍须停止新提交，并依靠优雅退出等待后台任务结束")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup", action="store_true", help="检查挂载及 8010，适用于启动前")
    parser.add_argument("--quiescence", action="store_true", help="只读检查请求日志，不能代替排空")
    args = parser.parse_args()
    try:
        check_mount()
        if args.startup:
            check_port()
        if args.quiescence:
            check_quiescence()
    except (RuntimeError, OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"部署检查未通过：{exc}\n")


if __name__ == "__main__":
    main()
