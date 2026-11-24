"""执行器产物：从工作目录收集、落盘、登记与读取。

安全性质：

- 只接受 `<workspace>/output/` 下的普通文件，符号链接一律不读；
- 产物路径必须是相对路径，且解析后仍在 output 目录内（防 `../` 穿越）；
- 落盘时按「租户 / 调用 id / 相对路径」重新拼路径，并再次做包含性校验；
- 大小以宿主机重新读取的结果为准，执行器上报的清单只当索引。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.tenancy import current_tenant
from app.models.entities import ToolArtifact, now_utc
from app.services.executor_client import ExecutorArtifact

logger = logging.getLogger(__name__)

MAX_RELATIVE_PATH = 400
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class CollectedArtifact:
    """已经在宿主机上校验过的产物。"""

    relative_path: str
    source: Path
    size: int
    sha256: str


def artifact_root() -> Path:
    return Path(settings.artifact_dir).expanduser().resolve()


def safe_relative_path(raw: str) -> str | None:
    """把执行器上报的路径归一化成安全的相对 POSIX 路径，非法则返回 None。"""
    normalized = raw.replace("\\", "/").strip()
    if not normalized or len(normalized) > MAX_RELATIVE_PATH:
        return None
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if not normalized:
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        return None
    if any(part in ("..", "") for part in pure.parts):
        return None
    # Windows 盘符 / UNC 在 POSIX 语义下不是绝对路径，这里显式拒绝。
    if ":" in pure.parts[0]:
        return None
    return pure.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_from_workspace(
    workspace: Path, manifest: list[ExecutorArtifact]
) -> list[CollectedArtifact]:
    """按执行器清单，从 `<workspace>/output` 收集并校验产物。"""
    output_root = (workspace / "output").resolve()
    if not output_root.is_dir():
        return []

    collected: list[CollectedArtifact] = []
    seen: set[str] = set()
    for item in manifest:
        relative = safe_relative_path(item.path)
        if relative is None:
            logger.warning(
                "artifact path rejected", extra={"fields": {"path": item.path[:200]}}
            )
            continue
        if relative in seen:
            # 清单可能重复上报同一个路径；数据库有唯一约束，这里先去重。
            continue
        seen.add(relative)
        try:
            resolved = (output_root / relative).resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(output_root) or not resolved.is_file():
            logger.warning(
                "artifact escaped output dir",
                extra={"fields": {"path": item.path[:200]}},
            )
            continue
        try:
            size = resolved.stat().st_size
            if size != item.size:
                # 清单与磁盘不一致时不接受：宁可少一个产物，也不登记不可信的大小。
                logger.warning(
                    "artifact size mismatch",
                    extra={
                        "fields": {"path": relative, "manifest": item.size, "actual": size}
                    },
                )
                continue
            if (
                settings.executor_max_artifact_bytes
                and size > settings.executor_max_artifact_bytes
            ):
                logger.warning(
                    "artifact too large", extra={"fields": {"path": relative, "size": size}}
                )
                continue
            sha256 = _sha256(resolved)
        except OSError:
            # 容器以非 root 写入时可能出现宿主机读不了的文件（权限位过窄），跳过即可。
            logger.warning(
                "artifact unreadable", extra={"fields": {"path": relative}}
            )
            continue
        collected.append(
            CollectedArtifact(
                relative_path=relative,
                source=resolved,
                size=size,
                sha256=sha256,
            )
        )
    return collected


class ArtifactStore:
    """把本次调用的产物复制进持久目录并写入数据库。"""

    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve_input(self, artifact_id: str) -> Path | None:
        """把产物 id 还原成宿主机路径。

        租户隔离靠 ORM 过滤：别的租户的 artifact_id 在这里查不到，
        因此输入挂载不可能拿到别人的文件。
        """
        artifact = self._db.get(ToolArtifact, artifact_id)
        if artifact is None:
            return None
        return stored_path(artifact)

    def persist(
        self,
        *,
        invocation_id: str,
        run_id: str,
        tool_name: str,
        artifacts: list[CollectedArtifact],
    ) -> list[ToolArtifact]:
        if not artifacts:
            return []

        tenant = current_tenant()
        root = (artifact_root() / tenant / invocation_id).resolve()
        total = 0
        rows: list[ToolArtifact] = []

        for item in artifacts:
            if total + item.size > settings.executor_max_total_artifact_bytes:
                logger.warning(
                    "artifact budget exhausted",
                    extra={
                        "fields": {
                            "invocation_id": invocation_id,
                            "skipped": item.relative_path,
                        }
                    },
                )
                break
            target = (root / item.relative_path).resolve()
            if not target.is_relative_to(root):
                logger.warning(
                    "artifact path escaped storage root",
                    extra={"fields": {"path": item.relative_path[:200]}},
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item.source, target)
            row = ToolArtifact(
                invocation_id=invocation_id,
                run_id=run_id,
                tool_name=tool_name,
                path=item.relative_path,
                size_bytes=item.size,
                sha256=item.sha256,
                created_at=now_utc(),
            )
            self._db.add(row)
            rows.append(row)
            total += item.size

        if rows:
            # 让调用方拿到 id（响应里要回给模型/前端）。
            self._db.flush()
        return rows


def stored_path(artifact: ToolArtifact) -> Path | None:
    """把数据库记录还原成磁盘路径，并再次做包含性校验。"""
    relative = safe_relative_path(artifact.path)
    if relative is None:
        return None
    root = (artifact_root() / artifact.tenant_id / artifact.invocation_id).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


# 输入挂载：容器内 /workspace 下的目标路径
MAX_INPUT_TARGET = 200
MAX_INPUTS = 16


def safe_input_target(raw: str) -> str | None:
    """输入文件在容器里的目标路径必须是 /workspace 下的安全相对路径。

    与 Rust 侧 `check_input_target` 同一套规则：两边都校验，
    一边被绕过时另一边仍然拦得住。
    """
    if not isinstance(raw, str):
        return None
    target = raw.strip().replace("\\", "/")
    if not target or len(target) > MAX_INPUT_TARGET:
        return None
    if target.startswith("/") or ":" in target:
        return None
    parts = target.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    # 产物目录是执行器收集产物的地方，不允许被输入挂载遮住
    if parts[0] == "output":
        return None
    return "/".join(parts)


def resolve_input_mounts(
    store: ArtifactStore,
    config: dict[str, Any],
    arguments: dict[str, Any],
) -> list[dict[str, str]]:
    """把工具 config 里的 `inputs` 声明解析成具体挂载。

    声明形如::

        "inputs": [{"argument": "file_id", "target": "input/data.csv", "required": true}]

    `argument` 是工具参数名，模型把**产物 id** 放在这个参数里；
    `target` 是容器内 /workspace 下的相对路径。

    来源只能是**当前租户登记过的产物**——挂载任意宿主机路径等于把宿主机的
    文件系统交出去，所以这里只认数据库里的产物记录。
    """
    declarations = config.get("inputs")
    if not declarations:
        return []
    if not isinstance(declarations, list):
        raise AppError(
            ErrorCode.TOOL_ARGUMENTS_INVALID,
            "config.inputs 必须是数组",
            status_code=422,
        )
    if len(declarations) > MAX_INPUTS:
        raise AppError(
            ErrorCode.TOOL_ARGUMENTS_INVALID,
            f"config.inputs 最多 {MAX_INPUTS} 项",
            status_code=422,
        )

    mounts: list[dict[str, str]] = []
    for item in declarations:
        if not isinstance(item, dict):
            raise AppError(
                ErrorCode.TOOL_ARGUMENTS_INVALID,
                "config.inputs 的每一项必须是对象",
                status_code=422,
            )
        argument = str(item.get("argument") or "")
        target = safe_input_target(str(item.get("target") or ""))
        required = bool(item.get("required"))
        if not argument or target is None:
            raise AppError(
                ErrorCode.TOOL_ARGUMENTS_INVALID,
                "config.inputs 的每一项都要有 argument 与合法的 target",
                status_code=422,
            )

        value = arguments.get(argument)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise AppError(
                    ErrorCode.TOOL_ARGUMENTS_INVALID,
                    f"缺少输入文件参数 {argument}",
                    status_code=422,
                )
            continue

        artifact_id = str(value).strip()
        source = store.resolve_input(artifact_id)
        if source is None:
            # 不区分"不存在"和"属于别的租户"：跨租户时不该泄露是否存在
            raise AppError(
                ErrorCode.TOOL_ARGUMENTS_INVALID,
                f"输入产物 {artifact_id} 不存在或不可用",
                status_code=422,
            )
        mounts.append({"source": str(source), "target": target})
    return mounts
