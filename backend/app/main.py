import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth_routes import router as auth_router
from app.api.error_handlers import register_exception_handlers
from app.api.middleware import request_context_middleware
from app.api.routes import router
from app.api.tool_routes import router as tool_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.migrate import run_migrations
from app.db.session import SessionLocal
from app.models import entities  # noqa: F401  注册 SQLAlchemy 模型
from app.services.api_keys import ApiKeyService

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
    # 原先是 Base.metadata.create_all；现在 schema 由 Alembic 迁移唯一负责。
    run_migrations()
    bootstrap_admin_key()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(request_context_middleware)
register_exception_handlers(app)
app.include_router(router)
app.include_router(tool_router)
app.include_router(auth_router)


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
