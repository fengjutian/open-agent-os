"""V0.3 隔离执行器竖切面测试。

用 `tests/fixtures/fake_executor.py` 代替真实 Docker：协议、错误映射、产物落盘、
路径安全与租户隔离都能在没有 Docker 的环境里验证。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.core.config import settings

FIXTURE = Path(__file__).parent / "fixtures" / "fake_executor.py"
EXECUTOR_COMMAND = f'"{Path(sys.executable).as_posix()}" "{FIXTURE.as_posix()}"'
FAKE_DOCKER = Path(__file__).parent / "fixtures" / "fake_docker.py"
REPO_ROOT = Path(__file__).resolve().parents[2]
RUST_BINARY = REPO_ROOT / "rust-executor" / "target" / "debug" / (
    "agent-executor.exe" if os.name == "nt" else "agent-executor"
)

EXECUTOR_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
    "additionalProperties": False,
}


def _configure(monkeypatch, mode: str = "success", **overrides) -> None:
    monkeypatch.setattr(settings, "executor_enabled", True)
    monkeypatch.setattr(settings, "executor_command", EXECUTOR_COMMAND)
    monkeypatch.setenv("FAKE_EXECUTOR_MODE", mode)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def _create_tool(client, name: str = "run_shell", **overrides) -> dict:
    payload = {
        "name": name,
        "description": "在隔离容器里执行命令",
        "execution_type": "executor",
        "config": {"runtime": "shell"},
        "input_schema": EXECUTOR_SCHEMA,
        **overrides,
    }
    response = client.post("/api/v1/tools", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _allow(client, agent: dict, name: str) -> None:
    response = client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": name, "effect": "allow"},
    )
    assert response.status_code == 201, response.text


def _invoke(client, agent: dict, name: str, arguments: dict):
    return client.post(
        f"/api/v1/agents/{agent['id']}/tools/{name}/invoke",
        json={"arguments": arguments},
    )


# ---------------------------------------------------------------------------
# 注册校验
# ---------------------------------------------------------------------------


def test_executor_tool_requires_runtime(client):
    response = client.post("/api/v1/tools", json={"name": "run", "execution_type": "executor"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_executor_tool_rejects_configured_command(client):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {"runtime": "shell", "command": "rm -rf /"},
            "input_schema": EXECUTOR_SCHEMA,
        },
    )
    assert response.status_code == 422
    assert "命令" in response.json()["error"]["message"]


def test_executor_tool_requires_command_in_schema(client):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {"runtime": "shell"},
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    )
    assert response.status_code == 422


def test_executor_tool_requires_command_to_be_required(client):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {"runtime": "shell"},
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("image", ["--privileged", "bad image", "a" * 201])
def test_executor_tool_rejects_bad_image(client, image):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {"runtime": "shell", "image": image},
            "input_schema": EXECUTOR_SCHEMA,
        },
    )
    assert response.status_code == 422


def test_developer_cannot_register_executor_tool(client, make_key):
    client.headers["Authorization"] = f"Bearer {make_key('developer')}"
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {"runtime": "shell"},
            "input_schema": EXECUTOR_SCHEMA,
        },
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 执行与产物
# ---------------------------------------------------------------------------


def test_executor_runs_and_stores_artifacts(client, agent, monkeypatch):
    _configure(monkeypatch, "success")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    response = _invoke(client, agent, "run_shell", {"command": "echo hi"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "succeeded"
    assert body["error_code"] is None
    assert body["result"]["exit_code"] == 0
    assert "hello" in body["result"]["stdout"]
    assert body["result"]["artifacts"][0]["path"] == "report.txt"

    # 顶层 artifacts 是从数据库回读的索引（带 id / sha256）
    assert len(body["artifacts"]) == 1
    artifact = body["artifacts"][0]
    assert artifact["path"] == "report.txt"
    assert artifact["size_bytes"] == len(b"hello artifact")
    assert artifact["tool_name"] == "run_shell"
    assert len(artifact["sha256"]) == 64
    # result 里的条目与顶层索引对得上
    assert body["result"]["artifacts"][0]["artifact_id"] == artifact["id"]

    listing = client.get(f"/api/v1/tool-invocations/{body['invocation_id']}/artifacts").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == artifact["id"]

    download = client.get(f"/api/v1/artifacts/{artifact['id']}/download")
    assert download.status_code == 200
    assert download.content == b"hello artifact"
    assert "attachment" in download.headers["content-disposition"]


def test_executor_collects_nested_artifacts(client, agent, monkeypatch):
    _configure(monkeypatch, "nested")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "succeeded"
    assert [item["path"] for item in body["artifacts"]] == ["logs/run.log"]


def test_executor_failure_surfaces_stderr(client, agent, monkeypatch):
    _configure(monkeypatch, "fail")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "false"}).json()

    assert body["status"] == "failed"
    assert body["error_code"] == "tool_execution_failed"
    assert "boom" in body["error_message"]
    assert body["artifacts"] == []

    # 失败也留调用记录与审计
    invocations = client.get("/api/v1/tool-invocations", params={"run_id": body["run_id"]}).json()
    assert invocations["total"] == 1
    assert invocations["items"][0]["status"] == "failed"
    audits = client.get("/api/v1/audit-logs", params={"run_id": body["run_id"]}).json()
    assert {item["action"] for item in audits["items"]} == {
        "tool.invoke",
        "tool.execute.failed",
    }


def test_executor_timeout_maps_to_tool_timeout(client, agent, monkeypatch):
    _configure(monkeypatch, "timeout", executor_grace_seconds=0.2)
    _create_tool(client, timeout_seconds=0.3)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "sleep 10"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "tool_timeout"


def test_missing_executor_binary_is_reported(client, agent, monkeypatch):
    _configure(
        monkeypatch,
        "success",
        executor_command="C:/definitely-missing/agent-executor-does-not-exist",
    )
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "executor_not_available"


def test_docker_unavailable_is_reported(client, agent, monkeypatch):
    _configure(monkeypatch, "reject")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "executor_not_available"
    assert "Docker" in body["error_message"]


def test_garbage_executor_output_is_rejected(client, agent, monkeypatch):
    _configure(monkeypatch, "garbage")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "executor_error"


def test_executor_disabled_returns_not_available(client, agent, monkeypatch):
    _configure(monkeypatch, "success")
    monkeypatch.setattr(settings, "executor_enabled", False)
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "executor_not_available"


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def test_traversal_artifact_is_ignored(client, agent, monkeypatch):
    _configure(monkeypatch, "traversal")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "succeeded"
    assert body["artifacts"] == []
    assert body["result"]["artifacts"] == []

    # 即使假执行器在 output 之外写了 evil.txt，也不应该被复制进产物目录
    assert not list(Path(settings.artifact_dir).rglob("evil.txt"))


def test_size_mismatch_artifact_is_ignored(client, agent, monkeypatch):
    _configure(monkeypatch, "size_mismatch")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "succeeded"
    assert body["artifacts"] == []


def test_duplicate_manifest_entries_are_deduplicated(client, agent, monkeypatch):
    _configure(monkeypatch, "duplicate")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "succeeded"
    assert [item["path"] for item in body["artifacts"]] == ["report.txt"]


# ---------------------------------------------------------------------------
# 租户隔离
# ---------------------------------------------------------------------------


def test_artifacts_are_tenant_isolated(client, agent, monkeypatch, tenant_key):
    _configure(monkeypatch, "success")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    artifact_id = body["artifacts"][0]["id"]
    invocation_id = body["invocation_id"]

    client.headers["Authorization"] = f"Bearer {tenant_key('acme')}"

    assert client.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 404
    assert (
        client.get(f"/api/v1/tool-invocations/{invocation_id}/artifacts").status_code == 404
    )


def test_default_container_user_matches_backend_process():
    from app.services.executor_client import _default_user

    if os.name == "posix":
        assert _default_user() == f"{os.getuid()}:{os.getgid()}"
    else:
        assert _default_user() == "0:0"


def test_executor_command_accepts_plain_windows_path(monkeypatch):
    """Windows 路径里的反斜杠不能被 shlex 吃掉。"""
    from app.services.executor_client import _executor_command

    monkeypatch.setattr(settings, "executor_command", sys.executable)
    assert _executor_command() == [sys.executable]


# ---------------------------------------------------------------------------
# 真实 Rust 二进制 × 假 docker：验证两端协议与超时/杀容器链路
# ---------------------------------------------------------------------------


def _configure_real_binary(monkeypatch, mode: str = "success") -> None:
    monkeypatch.setattr(settings, "executor_enabled", True)
    monkeypatch.setattr(settings, "executor_command", f'"{RUST_BINARY.as_posix()}"')
    monkeypatch.setenv(
        "AGENT_EXECUTOR_DOCKER_BIN",
        f'"{Path(sys.executable).as_posix()}" "{FAKE_DOCKER.as_posix()}"',
    )
    monkeypatch.setenv("FAKE_DOCKER_MODE", mode)


@pytest.mark.skipif(not RUST_BINARY.exists(), reason="agent-executor 未构建（cargo build）")
def test_real_executor_binary_runs_and_collects(client, agent, monkeypatch):
    _configure_real_binary(monkeypatch, "success")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "succeeded", body
    assert "container says hi" in body["result"]["stdout"]
    assert body["result"]["exit_code"] == 0
    assert [item["path"] for item in body["artifacts"]] == ["report.txt"]

    download = client.get(f"/api/v1/artifacts/{body['artifacts'][0]['id']}/download")
    assert download.content == b"from fake docker"


@pytest.mark.skipif(not RUST_BINARY.exists(), reason="agent-executor 未构建（cargo build）")
def test_real_executor_binary_reports_docker_down(client, agent, monkeypatch):
    _configure_real_binary(monkeypatch, "daemon_down")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "executor_not_available"
    assert "Docker" in body["error_message"]


@pytest.mark.skipif(not RUST_BINARY.exists(), reason="agent-executor 未构建（cargo build）")
def test_real_executor_binary_enforces_timeout(client, agent, monkeypatch):
    _configure_real_binary(monkeypatch, "timeout")
    monkeypatch.setattr(settings, "executor_grace_seconds", 1.5)
    _create_tool(client, timeout_seconds=0.6)
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "sleep 30"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "tool_timeout"


# ---------------------------------------------------------------------------
# 产物列表（租户级）
# ---------------------------------------------------------------------------


def _produce_artifact(client, agent, monkeypatch, name="run_shell"):
    _configure(monkeypatch, "success")
    _create_tool(client, name=name)
    _allow(client, agent, name)
    body = _invoke(client, agent, name, {"command": "echo hi"}).json()
    assert body["status"] == "succeeded", body
    return body


def test_artifacts_are_listed_across_invocations(client, agent, monkeypatch):
    """产物中心要能看到租户的全部产物，而不是只有某一次调用的。"""
    body = _produce_artifact(client, agent, monkeypatch)
    artifact_id = body["artifacts"][0]["id"]

    page = client.get("/api/v1/artifacts").json()
    assert page["total"] == 1
    item = page["items"][0]
    assert item["id"] == artifact_id
    assert item["path"] == "report.txt"
    assert item["tool_name"] == "run_shell"
    assert item["size_bytes"] == len(b"hello artifact")
    assert len(item["sha256"]) == 64


def test_artifacts_can_be_filtered(client, agent, monkeypatch):
    body = _produce_artifact(client, agent, monkeypatch)
    run_id = body["run_id"]

    assert client.get("/api/v1/artifacts", params={"run_id": run_id}).json()["total"] == 1
    assert client.get("/api/v1/artifacts", params={"tool_name": "run_shell"}).json()["total"] == 1
    # 过滤条件不命中时返回空列表，而不是 404
    assert client.get("/api/v1/artifacts", params={"tool_name": "other"}).json()["total"] == 0
    assert client.get("/api/v1/artifacts", params={"run_id": "run_missing"}).json()["total"] == 0


def test_artifacts_are_paginated(client, agent, monkeypatch):
    _produce_artifact(client, agent, monkeypatch)

    page = client.get("/api/v1/artifacts", params={"limit": 1}).json()
    assert page["total"] == 1
    assert len(page["items"]) == 1
    assert page["limit"] == 1

    assert client.get("/api/v1/artifacts", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/artifacts", params={"offset": -1}).status_code == 422


def test_artifacts_list_is_tenant_isolated(client, agent, monkeypatch, tenant_key):
    body = _produce_artifact(client, agent, monkeypatch)
    artifact_id = body["artifacts"][0]["id"]

    client.headers["Authorization"] = f"Bearer {tenant_key('acme')}"

    assert client.get("/api/v1/artifacts").json()["total"] == 0
    assert client.get(f"/api/v1/artifacts/{artifact_id}/download").status_code == 404


def test_artifact_download_uses_attachment_disposition(client, agent, monkeypatch):
    body = _produce_artifact(client, agent, monkeypatch)
    artifact_id = body["artifacts"][0]["id"]

    download = client.get(f"/api/v1/artifacts/{artifact_id}/download")
    assert download.status_code == 200
    assert download.content == b"hello artifact"
    assert "attachment" in download.headers["content-disposition"]
    assert "report.txt" in download.headers["content-disposition"]
