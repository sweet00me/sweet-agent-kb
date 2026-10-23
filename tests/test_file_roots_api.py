
"""
文件：tests/test_file_roots_api.py

用途：
    验证文件管理 API 中的存储入口查询功能。

测试内容：
    1. 正确返回存储入口。
    2. 返回统一的数据结构。
    3. 禁用客户端缓存。
    4. 存储发现异常时返回 HTTP 503。
    5. 不支持的 POST 请求返回 HTTP 405。

说明：
    使用独立 FastAPI 测试应用。
    模拟存储数据，不访问真实磁盘。
    不启动 LangGraph，也不调用 DeepSeek。
"""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.files as files_api

from app.services.storage import StorageDiscoveryError


def main():

    # 创建独立测试应用，
    # 避免触发正式 Agent 的生命周期初始化。
    test_app = FastAPI()

    test_app.include_router(
        files_api.router
    )

    mock_roots = [
        {
            "root_id": "home",
            "label": "Linux 主目录",
            "path": "/home/sweet",
            "kind": "home",
            "filesystem": None,
            "writable": True,
        },
        {
            "root_id": "mount-test001",
            "label": "Storage",
            "path": "/home/sweet/Storage",
            "kind": "mount",
            "filesystem": "ext4",
            "writable": True,
        },
    ]

    with TestClient(test_app) as client:

        print("========== 1. 正常查询 ==========")

        with patch.object(
            files_api,
            "list_storage_roots",
            return_value=mock_roots,
        ):

            response = client.get(
                "/api/v1/files/roots"
            )

        assert response.status_code == 200

        data = response.json()

        assert data["count"] == 2
        assert len(data["items"]) == 2

        assert data["items"][0]["root_id"] == "home"
        assert data["items"][1]["label"] == "Storage"

        print("PASS：存储入口查询成功")

        print("\n========== 2. 响应结构 ==========")

        expected_keys = {
            "root_id",
            "label",
            "path",
            "kind",
            "filesystem",
            "writable",
        }

        assert set(
            data["items"][0].keys()
        ) == expected_keys

        assert response.headers["cache-control"] == (
            "no-store"
        )

        print("PASS：响应结构正确")
        print("PASS：响应禁止缓存")

        print("\n========== 3. 异常处理 ==========")

        with patch.object(
            files_api,
            "list_storage_roots",
            side_effect=StorageDiscoveryError(
                "simulated failure"
            ),
        ):

            response = client.get(
                "/api/v1/files/roots"
            )

        assert response.status_code == 503

        print("PASS：存储发现异常返回 HTTP 503")

        print("\n========== 4. 只读接口 ==========")

        response = client.post(
            "/api/v1/files/roots"
        )

        assert response.status_code == 405

        print("PASS：拒绝不支持的 POST 请求")

    print("\n文件存储入口 API 测试全部通过")


if __name__ == "__main__":
    main()
