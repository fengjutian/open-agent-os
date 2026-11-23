"""API 层的租户隔离：跨租户一律 404，且同名资源可在不同租户共存。"""

from uuid import uuid4


def _h(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_agents_are_isolated(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    created = client.post("/api/v1/agents", json={"name": "only-a"}, headers=_h(key_a))
    assert created.status_code == 201
    agent_id = created.json()["id"]

    # B 看不到
    assert client.get("/api/v1/agents", headers=_h(key_b)).json()["total"] == 0
    assert client.get(f"/api/v1/agents/{agent_id}", headers=_h(key_b)).status_code == 404
    assert (
        client.patch(f"/api/v1/agents/{agent_id}", json={"name": "x"}, headers=_h(key_b)).status_code
        == 404
    )
    assert client.delete(f"/api/v1/agents/{agent_id}", headers=_h(key_b)).status_code == 404

    # A 仍然能看到
    assert client.get("/api/v1/agents", headers=_h(key_a)).json()["total"] == 1


def test_conversations_and_messages_are_isolated(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    agent = client.post("/api/v1/agents", json={"name": "a"}, headers=_h(key_a)).json()
    conv = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={"title": "a"}, headers=_h(key_a)
    ).json()
    client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=_h(key_a)
    )

    # B 既进不去会话，也看不到消息
    assert client.get(f"/api/v1/conversations/{conv['id']}/messages", headers=_h(key_b)).status_code == 404
    assert (
        client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"content": "hack"},
            headers=_h(key_b),
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/agents/{agent['id']}/conversations", headers=_h(key_b)).status_code
        == 404
    )


def test_tool_names_are_unique_per_tenant(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    assert client.post("/api/v1/tools", json={"name": "echo"}, headers=_h(key_a)).status_code == 201
    # 另一个租户可以拥有同名工具（唯一约束是 (tenant_id, name)）
    assert client.post("/api/v1/tools", json={"name": "echo"}, headers=_h(key_b)).status_code == 201

    assert client.get("/api/v1/tools", headers=_h(key_a)).json()["total"] == 1
    assert client.get("/api/v1/tools", headers=_h(key_b)).json()["total"] == 1


def test_policy_rules_are_isolated(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    agent_a = client.post("/api/v1/agents", json={"name": "a"}, headers=_h(key_a)).json()
    client.post("/api/v1/tools", json={"name": "echo"}, headers=_h(key_a))
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent_a["id"], "tool_name": "echo", "effect": "allow"},
        headers=_h(key_a),
    )

    assert client.get("/api/v1/policy-rules", headers=_h(key_b)).json()["total"] == 0


def test_skills_are_isolated(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    created = client.post(
        "/api/v1/skills",
        json={"name": "polite", "instructions": "打招呼"},
        headers=_h(key_a),
    )
    assert created.status_code == 201

    assert client.get("/api/v1/skills", headers=_h(key_b)).json()["total"] == 0
    assert client.get(f"/api/v1/skills/{created.json()['id']}", headers=_h(key_b)).status_code == 404

    # B 引用 A 的技能必须失败（在 B 的视图里它不存在）
    response = client.post(
        "/api/v1/agents", json={"name": "b", "skill_names": ["polite"]}, headers=_h(key_b)
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["missing"] == ["polite"]

    # B 可以建自己的同名技能
    assert (
        client.post(
            "/api/v1/skills", json={"name": "polite", "instructions": "b"}, headers=_h(key_b)
        ).status_code
        == 201
    )


def test_runs_and_audit_are_isolated(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    agent = client.post("/api/v1/agents", json={"name": "a"}, headers=_h(key_a)).json()
    conv = client.post(
        f"/api/v1/agents/{agent['id']}/conversations", json={"title": "a"}, headers=_h(key_a)
    ).json()
    body = client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=_h(key_a)
    ).json()

    assert client.get("/api/v1/runs", headers=_h(key_a)).json()["total"] == 1
    assert client.get("/api/v1/runs", headers=_h(key_b)).json()["total"] == 0
    assert client.get(f"/api/v1/runs/{body['run_id']}", headers=_h(key_b)).status_code == 404
    assert client.get(f"/api/v1/runs/{body['run_id']}/events", headers=_h(key_b)).status_code == 404

    assert client.get("/api/v1/audit-logs", headers=_h(key_b)).json()["total"] == 0


def test_tool_invocations_are_isolated(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    agent = client.post("/api/v1/agents", json={"name": "a"}, headers=_h(key_a)).json()
    client.post(
        "/api/v1/tools",
        json={
            "name": "echo",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        headers=_h(key_a),
    )
    client.post(
        "/api/v1/policy-rules",
        json={"agent_id": agent["id"], "tool_name": "echo", "effect": "allow"},
        headers=_h(key_a),
    )
    invoked = client.post(
        f"/api/v1/agents/{agent['id']}/tools/echo/invoke",
        json={"arguments": {"text": "hi"}},
        headers=_h(key_a),
    )
    assert invoked.json()["status"] == "succeeded", invoked.json()

    assert client.get("/api/v1/tool-invocations", headers=_h(key_a)).json()["total"] == 1
    assert client.get("/api/v1/tool-invocations", headers=_h(key_b)).json()["total"] == 0


def test_invoke_on_other_tenant_agent_is_404(client, tenant_key):
    key_a, key_b = tenant_key("tenant-a"), tenant_key("tenant-b")

    agent = client.post("/api/v1/agents", json={"name": "a"}, headers=_h(key_a)).json()

    response = client.post(
        f"/api/v1/agents/{agent['id']}/tools/echo/invoke",
        json={"arguments": {}},
        headers=_h(key_b),
    )
    assert response.status_code == 404


def test_key_created_for_tenant_lands_in_that_tenant(client, tenant_key):
    key_b = tenant_key("tenant-b")

    me = client.get("/api/v1/me", headers=_h(key_b)).json()
    assert me["key_id"]
    # 该密钥创建的资源只出现在 tenant-b
    client.post("/api/v1/agents", json={"name": "b-agent"}, headers=_h(key_b))
    assert client.get("/api/v1/agents", headers=_h(key_b)).json()["total"] == 1
    assert client.get("/api/v1/agents").json()["total"] == 0  # 默认租户看不到


def test_admin_can_provision_key_for_another_tenant(client, tenant_key):
    key_a = tenant_key("tenant-a")

    # 新租户必须先开通
    created = client.post(
        "/api/v1/tenants", json={"id": "tenant-c", "name": "Tenant C"}, headers=_h(key_a)
    )
    assert created.status_code == 201, created.text

    response = client.post(
        "/api/v1/api-keys",
        json={"name": f"provisioned-{uuid4().hex[:8]}", "role": "admin", "tenant_id": "tenant-c"},
        headers=_h(key_a),
    )
    assert response.status_code == 201

    new_key = response.json()["api_key"]
    me = client.get("/api/v1/me", headers=_h(new_key)).json()
    assert me["role"] == "admin"
