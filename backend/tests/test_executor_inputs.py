"""输入文件挂载的测试。

威胁模型：容器里跑的是**模型给的命令**。如果允许挂任意宿主机路径，
等于把宿主机文件系统整个交出去。所以规则很窄：

1. 来源只能是**当前租户登记过的产物**（数据库里的 ToolArtifact 记录）；
2. 目标路径必须在 `/workspace` 下，且一律**只读**；
3. 目标路径两端各校验一次（Python 侧 + Rust 侧），一边被绕过另一边仍拦得住。

这里用 `tests/fixtures/fake_executor.py` 代替真实 Docker：验证协议、路径校验、
租户隔离与错误映射；真正的 docker 挂载参数由 Rust 单测断言。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.artifacts import safe_input_target

FIXTURE = Path(__file__).parent / "fixtures" / "fake_executor.py"
EXECUTOR_COMMAND = f'"{Path(sys.executable).as_posix()}" "{FIXTURE.as_posix()}"'

MAX_INPUTS = 16

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "file_id": {"type": "string"},
    },
    "required": ["command"],
    "additionalProperties": False,
}


def _configure(monkeypatch, mode: str = "success") -> None:
    monkeypatch.setattr(settings, "executor_enabled", True)
    monkeypatch.setattr(settings, "executor_command", EXECUTOR_COMMAND)
    monkeypatch.setenv("FAKE_EXECUTOR_MODE", mode)


def _create_tool(client, name: str = "run_shell", *, inputs=None, schema=None) -> dict:
    config = {"runtime": "shell"}
    if inputs is not None:
        config["inputs"] = inputs
    payload = {
        "name": name,
        "description": "在隔离容器里执行命令",
        "execution_type": "executor",
        "config": config,
        "input_schema": schema or INPUT_SCHEMA,
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


def _mounts_from(body: dict) -> list[dict]:
    """从假执行器回显的 stdout 里取出收到的输入挂载。"""
    return json.loads(body["result"]["stdout"])


# ---------------------------------------------------------------------------
# 目标路径校验（纯函数）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["input/report.txt", "data.csv", "input/nested/dir/report.txt", "a-b_c.1.txt"],
)
def test_safe_input_target_accepts_workspace_relative_paths(target):
    assert safe_input_target(target) == target


@pytest.mark.parametrize(
    "target",
    [
        "",  # 空
        "../escape.txt",  # 上跳
        "input/../../escape.txt",  # 上跳
        "/etc/passwd",  # 绝对路径
        "C:\\windows\\system32",  # 盘符
        "input//report.txt",  # 空片段
        "./report.txt",  # 当前目录片段
        "output/report.txt",  # 产物目录不能被遮住
        "output",  # 同上
        "a" * 201,  # 过长
    ],
)
def test_safe_input_target_rejects_unsafe_paths(target):
    assert safe_input_target(target) is None


def test_safe_input_target_normalizes_windows_separators():
    assert safe_input_target("input\\report.txt") == "input/report.txt"


# ---------------------------------------------------------------------------
# 注册期校验
# ---------------------------------------------------------------------------


def test_executor_tool_rejects_non_array_inputs(client):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {"runtime": "shell", "inputs": {"argument": "file_id"}},
            "input_schema": INPUT_SCHEMA,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "argument",
    ["File-Id", "1file", "file id", "file.id", "", "a" * 65],
)
def test_executor_tool_rejects_bad_input_argument(client, argument):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {
                "runtime": "shell",
                "inputs": [{"argument": argument, "target": "input/report.txt"}],
            },
            "input_schema": INPUT_SCHEMA,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("target", ["../escape.txt", "/etc/passwd", "output/report.txt"])
def test_executor_tool_rejects_unsafe_input_target(client, target):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {
                "runtime": "shell",
                "inputs": [{"argument": "file_id", "target": target}],
            },
            "input_schema": INPUT_SCHEMA,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_executor_tool_rejects_duplicate_input_target(client):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {
                "runtime": "shell",
                "inputs": [
                    {"argument": "file_id", "target": "input/report.txt"},
                    {"argument": "other_id", "target": "input/report.txt"},
                ],
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "file_id": {"type": "string"},
                    "other_id": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_executor_tool_rejects_input_argument_not_declared_as_string(client):
    """承载产物 id 的参数必须是 string，否则模型根本传不了 id 进来。"""
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {
                "runtime": "shell",
                "inputs": [{"argument": "file_id", "target": "input/report.txt"}],
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "file_id": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_executor_tool_rejects_too_many_inputs(client):
    response = client.post(
        "/api/v1/tools",
        json={
            "name": "run",
            "execution_type": "executor",
            "config": {
                "runtime": "shell",
                "inputs": [
                    {"argument": f"file_{index}", "target": f"input/file_{index}.txt"}
                    for index in range(MAX_INPUTS + 1)
                ],
            },
            "input_schema": INPUT_SCHEMA,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_executor_tool_accepts_valid_inputs_declaration(client):
    tool = _create_tool(
        client,
        inputs=[{"argument": "file_id", "target": "input/report.txt", "required": True}],
    )
    assert tool["id"]


# ---------------------------------------------------------------------------
# 执行期：产物 → 只读挂载
# ---------------------------------------------------------------------------


def test_executor_mounts_tenant_artifact_read_only(client, agent, monkeypatch):
    _configure(monkeypatch, "success")
    # 产出产物的工具不声明输入；挂载输入的是另一个工具
    _create_tool(client, name="run_shell")
    _create_tool(
        client,
        name="run_with_file",
        inputs=[{"argument": "file_id", "target": "input/report.txt", "required": True}],
    )
    _allow(client, agent, "run_shell")
    _allow(client, agent, "run_with_file")

    # 先跑一次，产出一个产物
    produced = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert produced["status"] == "succeeded"
    artifact_id = produced["artifacts"][0]["id"]

    # 再跑一次，把上面那个产物作为输入挂进去
    monkeypatch.setenv("FAKE_EXECUTOR_MODE", "echo_inputs")
    body = _invoke(
        client,
        agent,
        "run_with_file",
        {"command": "cat input/report.txt", "file_id": artifact_id},
    ).json()
    assert body["status"] == "succeeded", body

    mounts = _mounts_from(body)
    assert len(mounts) == 1
    assert mounts[0]["target"] == "input/report.txt"

    source = Path(mounts[0]["source"])
    assert source.is_file()
    assert source.read_bytes() == b"hello artifact"
    # 来源必须是产物目录里的文件，不能是任意宿主机路径
    assert source.is_relative_to(Path(settings.artifact_dir))


def test_executor_skips_optional_input_when_absent(client, agent, monkeypatch):
    _configure(monkeypatch, "echo_inputs")
    _create_tool(
        client,
        inputs=[{"argument": "file_id", "target": "input/report.txt", "required": False}],
    )
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "succeeded", body
    assert _mounts_from(body) == []


def test_executor_rejects_missing_required_input(client, agent, monkeypatch):
    _configure(monkeypatch, "echo_inputs")
    _create_tool(
        client,
        inputs=[{"argument": "file_id", "target": "input/report.txt", "required": True}],
    )
    _allow(client, agent, "run_shell")

    body = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "tool_arguments_invalid"
    assert "file_id" in body["error_message"]


def test_executor_rejects_unknown_artifact(client, agent, monkeypatch):
    _configure(monkeypatch, "echo_inputs")
    _create_tool(
        client,
        inputs=[{"argument": "file_id", "target": "input/report.txt", "required": True}],
    )
    _allow(client, agent, "run_shell")

    body = _invoke(
        client,
        agent,
        "run_shell",
        {"command": "echo hi", "file_id": "art_definitely_missing"},
    ).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "tool_arguments_invalid"
    assert "不存在或不可用" in body["error_message"]


def test_executor_cannot_mount_another_tenants_artifact(
    client, agent, monkeypatch, tenant_key
):
    """跨租户的产物必须挂不上：租户隔离靠 ORM 过滤，不靠调用方自觉。"""
    _configure(monkeypatch, "success")
    _create_tool(client, name="run_shell")
    _allow(client, agent, "run_shell")

    produced = _invoke(client, agent, "run_shell", {"command": "echo hi"}).json()
    artifact_id = produced["artifacts"][0]["id"]

    # 换到另一个租户：同一套工具、另一个 agent
    client.headers["Authorization"] = f"Bearer {tenant_key('acme')}"
    acme_agent = client.post("/api/v1/agents", json={"name": "Acme Agent"}).json()
    _create_tool(
        client,
        name="acme_shell",
        inputs=[{"argument": "file_id", "target": "input/report.txt", "required": True}],
    )
    _allow(client, acme_agent, "acme_shell")

    body = _invoke(
        client,
        acme_agent,
        "acme_shell",
        {"command": "echo hi", "file_id": artifact_id},
    ).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "tool_arguments_invalid"
    # 不区分「不存在」与「属于别的租户」：跨租户时不该泄露产物是否存在
    assert "不存在或不可用" in body["error_message"]

    missing_id = "art_definitely_missing"
    missing = _invoke(
        client,
        acme_agent,
        "acme_shell",
        {"command": "echo hi", "file_id": missing_id},
    ).json()
    assert missing["status"] == "failed"
    # 两种情形的措辞必须完全一致（只有调用方自己传的 id 不同）
    assert body["error_message"].replace(artifact_id, "<ID>") == missing["error_message"].replace(
        missing_id, "<ID>"
    )


def test_executor_does_not_mount_when_tool_has_no_inputs(client, agent, monkeypatch):
    """没有声明 inputs 的工具不该凭空多出挂载。"""
    _configure(monkeypatch, "echo_inputs")
    _create_tool(client)
    _allow(client, agent, "run_shell")

    body = _invoke(
        client,
        agent,
        "run_shell",
        {"command": "echo hi", "file_id": "art_whatever"},
    ).json()
    assert body["status"] == "succeeded", body
    assert _mounts_from(body) == []
