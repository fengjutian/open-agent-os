"""在途运行的广播通道。

要解决的问题：客户端断开不能让运行跟着死。原来的实现里 HTTP 生成器**就是**执行体，
``GeneratorExit`` 一到运行就被标记为失败，重连也无从续起。

现在的分工是：

- 运行本身跑在一个独立 `asyncio.Task` 里，自带数据库会话；
- HTTP 生成器只订阅一个队列，断开时只是退订，任务继续跑完；
- 重连方可以按 `seq` 回放已落库的事件（见 `/runs/{id}/events/stream`）。

注意：事件里的**增量文本不落库**，所以重连能拿到生命周期事件与最终消息，
但拿不回断流前已经流出的那段文字——这一点必须如实写在文档里。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

Queue = asyncio.Queue


class RunChannel:
    """一次运行的广播通道。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.task: asyncio.Task | None = None
        self._subscribers: set[Queue] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def subscribe(self) -> Queue:
        queue: Queue = asyncio.Queue()
        if self._closed:
            # 已经结束：立刻给订阅者一个终止信号
            queue.put_nowait(None)
            return queue
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def close(self) -> None:
        """通知所有订阅者结束（不影响任务本身）。"""
        self._closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)
        self._subscribers.clear()


class RunHub:
    def __init__(self) -> None:
        self._channels: dict[str, RunChannel] = {}

    def create(self, run_id: str) -> RunChannel:
        channel = RunChannel(run_id)
        self._channels[run_id] = channel
        return channel

    def get(self, run_id: str) -> RunChannel | None:
        return self._channels.get(run_id)

    def discard(self, run_id: str) -> None:
        self._channels.pop(run_id, None)

    def active_count(self) -> int:
        return len(self._channels)

    async def close_all(self) -> None:
        """应用退出：取消所有在途运行，避免半截任务。"""
        channels = list(self._channels.values())
        self._channels.clear()

        for channel in channels:
            channel.close()
            if channel.task is not None and not channel.task.done():
                channel.task.cancel()

        for channel in channels:
            if channel.task is None:
                continue
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await channel.task


_hub = RunHub()


def get_run_hub() -> RunHub:
    return _hub
