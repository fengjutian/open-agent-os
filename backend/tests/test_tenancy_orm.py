"""验证租户过滤**真的生效**——这是本次改动最关键的测试。

如果 ORM 事件没注册，本文件的测试会全绿以外的部分全部失败，
因此它们的存在意义就是「证明隔离不是看起来生效」。
"""

import pytest
from sqlalchemy import select

from app.core.tenancy import reset_tenant, set_tenant
from app.db.session import SessionLocal
from app.models.entities import Agent


def test_orm_filter_applies_to_select_and_get():
    """直接打数据库层：同一会话换个租户上下文，看得到的东西必须不同。"""
    token = set_tenant("tenant-a")
    try:
        with SessionLocal() as db:
            agent = Agent(name="a-agent", system_prompt="x", model="m")
            db.add(agent)
            db.commit()
            agent_id = agent.id
    finally:
        reset_tenant(token)

    token = set_tenant("tenant-a")
    try:
        with SessionLocal() as db:
            assert db.scalar(select(Agent).where(Agent.id == agent_id)) is not None
            assert db.get(Agent, agent_id) is not None
    finally:
        reset_tenant(token)

    token = set_tenant("tenant-b")
    try:
        with SessionLocal() as db:
            # select 与 Session.get 都必须被过滤
            assert db.scalar(select(Agent).where(Agent.id == agent_id)) is None
            assert db.get(Agent, agent_id) is None
            assert db.scalars(select(Agent)).all() == []
    finally:
        reset_tenant(token)


def test_bypass_option_is_visible_and_works():
    """绕过过滤必须写成显式的 execution option，且确实能看到跨租户数据。"""
    token = set_tenant("tenant-a")
    try:
        with SessionLocal() as db:
            db.add(Agent(name="visible-shared", system_prompt="x", model="m"))
            db.commit()
    finally:
        reset_tenant(token)

    token = set_tenant("tenant-b")
    try:
        with SessionLocal() as db:
            scoped = db.scalars(select(Agent)).all()
            all_tenants = db.scalars(
                select(Agent).execution_options(include_all_tenants=True)
            ).all()
    finally:
        reset_tenant(token)

    assert scoped == []
    assert any(item.name == "visible-shared" for item in all_tenants)


def test_writes_get_current_tenant():
    token = set_tenant("tenant-w")
    try:
        with SessionLocal() as db:
            agent = Agent(name="w-agent", system_prompt="x", model="m")
            db.add(agent)
            db.commit()
            assert agent.tenant_id == "tenant-w"
    finally:
        reset_tenant(token)


def test_every_mapped_class_has_tenant_id():
    """结构性防呆：新增模型若忘记继承 TenantScoped，这里会失败。

    `Tenant` 是隔离边界本身，不需要（也不该有）tenant_id。
    """
    from app.db.session import Base
    from app.models.entities import Tenant, TenantScoped

    global_models = {Tenant}
    missing: list[str] = []
    not_scoped: list[str] = []

    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if cls in global_models:
            continue
        if "tenant_id" not in cls.__table__.columns:
            missing.append(cls.__name__)
        if not issubclass(cls, TenantScoped):
            not_scoped.append(cls.__name__)

    assert missing == [], f"以下模型缺少 tenant_id: {missing}"
    assert not_scoped == [], f"以下模型没有继承 TenantScoped（不会被租户过滤）: {not_scoped}"


@pytest.mark.parametrize("tenant", ["a", "b", "default"])
def test_tenant_isolation_in_database(tenant):
    """多租户写入后互不可见。"""
    token = set_tenant(tenant)
    try:
        with SessionLocal() as db:
            db.add(Agent(name=f"agent-{tenant}", system_prompt="x", model="m"))
            db.commit()
    finally:
        reset_tenant(token)

    token = set_tenant(tenant)
    try:
        with SessionLocal() as db:
            names = [item.name for item in db.scalars(select(Agent)).all()]
    finally:
        reset_tenant(token)

    assert names == [f"agent-{tenant}"]
