import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.approval_routes import router as approval_router
from app.api.auth_routes import router as auth_router
from app.api.cron_routes import router as cron_router
from app.api.error_handlers import register_exception_handlers
from app.api.knowledge_routes import router as knowledge_router
from app.api.memory_routes import router as memory_router
from app.api.middleware import RequestContextMiddleware
from app.api.routes import router
from app.api.run_routes import router as run_router
from app.api.skill_routes import router as skill_router
from app.api.tenant_routes import router as tenant_router
from app.api.tool_routes import router as tool_router
from app.api.usage_routes import router as usage_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.telemetry import init_telemetry, shutdown_telemetry
from app.db.migrate import run_migrations
from app.db.session import SessionLocal
from app.models import entities  # noqa: F401  注册 SQLAlchemy 模型
from app.services.api_keys import ApiKeyService
from app.services.cron_scheduler import scheduler_loop
from app.services.mcp_pool import get_mcp_pool
from app.services.run_stream import get_run_hub
from app.services.tenants import bootstrap_default_tenant
from app.services.tool_offload import ensure_artifact_read_tool

logger = logging.getLogger(__name__)


def bootstrap_admin_key() -> None:
    """确保系统里至少有一个可用的 admin 密钥。

    没有配置 BOOTSTRAP_ADMIN_API_KEY 时自动生成一把，并在日志里打印一次——
    这是「首次启动即可用」与「默认开启认证」之间的折中，务必只在可信环境查看日志。
    """
    if not settings.auth_enabled:
        logger.warning(
            "AUTH_ENABLED=false：所有接口无需认证即可访问，且具备管理员权限",
            extra={"fields": {"warning": "insecure_mode"}},
        )
        return

    with SessionLocal() as db:
        # 先确保默认租户存在，否则引导密钥会因为租户校验失败
        bootstrap_default_tenant(db)
        raw = ApiKeyService(db).ensure_bootstrap_admin(settings.bootstrap_admin_api_key)

    if raw:
        logger.warning(
            "已生成初始 admin API Key（仅显示这一次，请立即保存）",
            extra={
                "fields": {
                    "api_key": raw,
                    "hint": "建议复制到 backend/.env 的 BOOTSTRAP_ADMIN_API_KEY",
                }
            },
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    # 默认关闭；配置 OTEL_ENABLED 后才会真正建立 provider
    init_telemetry()
    # 原先是 Base.metadata.create_all；现在 schema 由 Alembic 迁移唯一负责。
    run_migrations()
    bootstrap_admin_key()
    # 平台内建工具：artifact.read（取回被旁路的大结果）。
    # 默认租户先注册一次；其它租户在首次调用时按需注册（见 tool_gateway）。
    with SessionLocal() as db:
        ensure_artifact_read_tool(db)
        db.commit()

    # 定时任务调度：进程内轮询。多实例部署需要外部分布式锁，这里不假装支持。
    scheduler_task: asyncio.Task | None = None
    if settings.cron_enabled:
        scheduler_task = asyncio.create_task(scheduler_loop())

    yield

    if scheduler_task is not None:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task

    # 取消在途运行（避免留下半截任务），再回收 MCP 子进程
    await get_run_hub().close_all()
    await get_mcp_pool().close_all()
    shutdown_telemetry()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)
app.include_router(router)
app.include_router(tool_router)
app.include_router(auth_router)
app.include_router(run_router)
app.include_router(skill_router)
app.include_router(tenant_router)
app.include_router(cron_router)
app.include_router(approval_router)
app.include_router(memory_router)
app.include_router(knowledge_router)
app.include_router(usage_router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
