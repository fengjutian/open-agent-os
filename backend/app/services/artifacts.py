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

from sqlalchemy.orm import Session

from app.core.config import settings
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
