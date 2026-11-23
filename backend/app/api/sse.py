"""Server-Sent Events 格式化。"""

from __future__ import annotations

import json
from typing import Any


def sse_event(event: dict[str, Any], *, event_id: int | str | None = None) -> str:
    """把 {"type": "...", ...} 渲染成一条 SSE 消息。

    事件名取自 `type`，其余字段作为 JSON data。前端按 `event:` 分支。
    带上 `id:` 之后浏览器/客户端可以用 `Last-Event-ID` 断点续订。
    """
    payload = dict(event)
    name = str(payload.pop("type", "message"))
    data = json.dumps(payload, ensure_ascii=False, default=str)
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {name}\ndata: {data}\n\n"
