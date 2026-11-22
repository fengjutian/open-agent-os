"""策略引擎单元测试：deny-by-default、优先级、作用域与通配。"""

from app.db.session import SessionLocal
from app.models.entities import PolicyRule
from app.services.policy import PolicyEngine


def _add_rule(db, **fields) -> PolicyRule:
    rule = PolicyRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def test_deny_by_default_when_no_rule_matches(client):
    with SessionLocal() as db:
        decision = PolicyEngine(db).evaluate(agent_id=None, tool_name="echo")

    assert decision.allowed is False
    assert decision.effect is None
    assert "默认拒绝" in decision.reason


def test_allow_rule_grants_access(client, agent):
    with SessionLocal() as db:
        _add_rule(db, agent_id=agent["id"], tool_name="echo", effect="allow", priority=100)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is True
    assert decision.effect == "allow"


def test_deny_wins_over_allow_at_same_priority(client, agent):
    with SessionLocal() as db:
        _add_rule(db, agent_id=agent["id"], tool_name="echo", effect="allow", priority=100)
        _add_rule(db, agent_id=agent["id"], tool_name="echo", effect="deny", priority=100)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is False
    assert decision.effect == "deny"


def test_higher_priority_wins(client, agent):
    with SessionLocal() as db:
        _add_rule(db, agent_id=agent["id"], tool_name="echo", effect="deny", priority=100)
        _add_rule(db, agent_id=agent["id"], tool_name="echo", effect="allow", priority=200)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is True


def test_agent_scoped_rule_beats_global_rule(client, agent):
    with SessionLocal() as db:
        # 全局放行、特定 Agent 拒绝：同优先级下更具体的规则生效
        _add_rule(db, agent_id=None, tool_name="echo", effect="allow", priority=100)
        _add_rule(db, agent_id=agent["id"], tool_name="echo", effect="deny", priority=100)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is False


def test_global_rule_applies_to_any_agent(client, agent):
    with SessionLocal() as db:
        _add_rule(db, agent_id=None, tool_name="echo", effect="allow", priority=100)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is True


def test_wildcard_rule_matches_any_tool(client, agent):
    with SessionLocal() as db:
        _add_rule(db, agent_id=agent["id"], tool_name="*", effect="allow", priority=100)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="some_future_tool")

    assert decision.allowed is True


def test_disabled_rule_is_ignored(client, agent):
    with SessionLocal() as db:
        _add_rule(
            db,
            agent_id=agent["id"],
            tool_name="echo",
            effect="allow",
            priority=100,
            enabled=False,
        )
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is False


def test_rule_for_other_tool_does_not_apply(client, agent):
    with SessionLocal() as db:
        _add_rule(db, agent_id=agent["id"], tool_name="other", effect="allow", priority=100)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is False


def test_rule_for_other_agent_does_not_apply(client, agent):
    with SessionLocal() as db:
        _add_rule(db, agent_id="another-agent", tool_name="echo", effect="allow", priority=100)
        decision = PolicyEngine(db).evaluate(agent_id=agent["id"], tool_name="echo")

    assert decision.allowed is False
