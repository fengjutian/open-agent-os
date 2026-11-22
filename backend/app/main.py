from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import register_exception_handlers
from app.api.middleware import request_context_middleware
from app.api.routes import router
from app.api.tool_routes import router as tool_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.migrate import run_migrations
from app.models import entities  # noqa: F401  注册 SQLAlchemy 模型


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    # 原先是 Base.metadata.create_all；现在 schema 由 Alembic 迁移唯一负责。
    run_migrations()
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


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}
