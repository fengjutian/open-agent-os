"""在 ORM 层统一强制租户过滤。

为什么放在这里而不是每条查询里：

- 逐个查询加 `where(tenant_id == ...)` 一定会漏，而漏一次就是跨租户数据泄露；
- `with_loader_criteria` 会被注入到所有 SELECT，**包括 `Session.get()`**、
  `count()` 和带显式 where 的查询（已用实验验证），因此 `get_or_404`
  天然只会命中本租户的行，跨租户访问直接变成 404，不泄露「该 id 是否存在」。

确实需要跨租户读的地方（例如按密钥哈希查 ApiKey 以完成认证）必须显式声明
`execution_options(include_all_tenants=True)`，这样绕过过滤的行为在代码里是可见的。
"""

from __future__ import annotations

from sqlalchemy import event, literal
from sqlalchemy.orm import Session, with_loader_criteria

from app.core.tenancy import current_tenant
from app.models.entities import TenantScoped

INCLUDE_ALL_TENANTS = "include_all_tenants"


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_scope(execute_state) -> None:  # noqa: ANN001 - SQLAlchemy 事件
    if not execute_state.is_select:
        return
    if execute_state.execution_options.get(INCLUDE_ALL_TENANTS):
        return

    # 两条 SQLAlchemy lambda 的硬约束：
    # 1. 条件里不能调用函数（所以不能写 cls.tenant_id == current_tenant()）；
    # 2. 闭包变量必须是可缓存的 SQL 元素，否则报「不是可缓存元素」。
    # 因此先在 lambda 外把租户做成 literal（BindParameter），再在条件里引用它。
    tenant_value = literal(current_tenant())

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.tenant_id == tenant_value,
            include_aliases=True,
        )
    )
