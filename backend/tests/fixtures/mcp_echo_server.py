"""测试用的最小 MCP 服务器（stdio，换行分隔 JSON-RPC 2.0）。

不是通用实现，只覆盖测试需要的能力：initialize / tools/list / tools/call，
外加一个「非法 schema」的工具和一个会慢的工具，用来验证跳过与超时分支。
"""

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "回显传入的文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "slow",
        "description": "睡眠指定秒数后返回",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
    },
    {
        "name": "boom",
        "description": "总是返回错误结果",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    {
        "name": "broken_schema",
        "description": "inputSchema 非法，同步时应当被跳过",
        "inputSchema": {"type": "definitely-not-a-valid-type"},
    },
]


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    # MCP stdio 传输规定是 UTF-8；Windows 上 Python 默认用 cp936，
    # 不显式改掉的话中文内容会被写成 GBK 字节，客户端按 UTF-8 解析直接失败。
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "echo-fixture", "version": "0.1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}

            if name == "echo":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": str(arguments.get("text", ""))}],
                            "isError": False,
                        },
                    }
                )
            elif name == "slow":
                time.sleep(float(arguments.get("seconds", 1)))
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": "slow done"}], "isError": False},
                    }
                )
            elif name == "boom":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": "boom failure"}],
                            "isError": True,
                        },
                    }
                )
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                    }
                )
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
