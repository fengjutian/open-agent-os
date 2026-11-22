"""Server-Sent Events 格式化。"""

from __future__ import annotations

import json
from typing import Any


def sse_event(event: dict[str, Any]) -> str:
    """把 {"type": "...", ...} 渲染成一条 SSE 消息。

    事件名取自 `type`，其余字段作为 JSON data。前端按 `event:` 分支。
    """
    payload = dict(event)
    name = str(payload.pop("type", "message"))
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {data}\n\n"
