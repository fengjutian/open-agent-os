"""跨请求的关联标识：request_id 与 run_id。

放在 contextvar 里，这样日志 Formatter、异常处理器和业务代码都能取到同一个值，
不必层层传参。
"""

from contextvars import ContextVar

# 每个 HTTP 请求一个；也接受客户端通过 X-Request-ID 传入，便于链路串联。
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# 每次 Agent 运行（一次 respond 调用）一个；后续 V0.2 的 Run/Event 持久化会复用它。
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
