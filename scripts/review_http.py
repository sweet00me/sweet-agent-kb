
"""
文件：scripts/review_http.py

用途：
    通过 FastAPI 完成人工工具审批。

主要功能：
    1. 查询当前线程的待审批工具。
    2. 展示工具名称、参数和调用 ID。
    3. 从终端逐项收集批准或拒绝决定。
    4. 将审批结果提交给 FastAPI。
    5. 展示 Agent 恢复执行后的返回结果。

安全说明：
    本脚本不直接操作 LangGraph 或 SQLite。
    只调用 FastAPI 的 HTTP 接口。

运行：
    python -m scripts.review_http --thread-id research-xxx
"""

import json
import argparse
import re
import urllib.request
import urllib.error


BASE_URL = "http://127.0.0.1:8010/api/v1"


# =====================================
# 1. HTTP JSON 请求
# =====================================

def request_json(
    url: str,
    payload: dict | None = None,
    timeout: int = 180,
):
    """发送 GET 或 POST 请求并解析 JSON。"""

    data = None

    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json"
        },
        method="POST" if payload is not None else "GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            return json.load(response)

    except urllib.error.HTTPError as exc:
        print(f"HTTP 错误：{exc.code}")
        print(exc.read().decode("utf-8"))
        raise SystemExit(1) from exc


# =====================================
# 2. 收集审批决定
# =====================================

def collect_decisions(calls: list[dict]):
    """
    逐项收集审批决定。

    y：批准
    n：拒绝（默认）
    q：取消整批审批，不发送 HTTP 请求
    """

    decisions = {}
    fingerprints = {}

    for call in calls:

        call_id = call["tool_call_id"]

        print("\n========== 待审批工具 ==========")
        print("Call ID：", call_id)
        print("工具名称：", call["tool_name"])
        print("工具参数：")

        print(json.dumps(
            call["arguments"],
            ensure_ascii=False,
            indent=2,
        ))

        while True:

            answer = input(
                "\n是否批准？[y/N/q]："
            ).strip().lower()

            if answer in ("q", "quit"):
                print("已取消，本次未提交审批")
                return None

            if answer in ("y", "yes"):
                decisions[call_id] = "approve"
                break

            if answer in ("", "n", "no"):
                decisions[call_id] = "reject"
                break

            print("请输入 y、n 或 q")

        fingerprints[call_id] = call["fingerprint"]

    return {
        "decisions": decisions,
        "expected_fingerprints": fingerprints,
    }


# =====================================
# 3. 主程序
# =====================================

def main():

    parser = argparse.ArgumentParser(
        description="Sweet Research Agent HTTP 审批工具"
    )

    parser.add_argument(
        "--thread-id",
        required=True,
        help="需要审批的 LangGraph 线程 ID",
    )

    args = parser.parse_args()

    thread_id = args.thread_id

    if not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}",
        thread_id,
    ):
        parser.error("thread_id 格式不正确")

    status_url = (
        f"{BASE_URL}/threads/{thread_id}/status"
    )

    review_url = (
        f"{BASE_URL}/threads/{thread_id}/review"
    )

    # =================================
    # 4. 查询待审批状态
    # =================================

    status = request_json(
        status_url,
        timeout=15,
    )

    print("Thread ID：", thread_id)
    print("当前状态：", status["status"])

    if status["status"] != "pending_approval":
        print("当前没有待审批任务")
        return

    calls = status.get("pending_approvals", [])

    if not calls:
        print("待审批工具列表为空，停止操作")
        return

    # =================================
    # 5. 人工输入决定
    # =================================

    try:
        payload = collect_decisions(calls)

    except (KeyboardInterrupt, EOFError):
        print("\n输入已中断，没有提交审批")
        return

    if payload is None:
        return

    # =================================
    # 6. 提交 HTTP 审批
    # =================================

    print("\n正在提交审批请求...")

    result = request_json(
        review_url,
        payload=payload,
    )

    print("\n========== Agent 返回 ==========")

    print(json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
