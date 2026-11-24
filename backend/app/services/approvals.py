"""人工审批（HITL）：工具调用的"暂停点"。

当某个工具被标记为 `requires_approval` 时，运行会在**执行之前**停下：

1. 网关落一条 `pending` 的 `ToolApproval`（含工具名与参数），不执行工具；
2. 运行时把当前消息列表存成检查点，run 置为 `waiting_approval`；
3. 有人批准 → 执行该工具、把结果回灌，从检查点**续跑同一个 run**；
4. 有人拒绝 → 不执行，把"被拒绝"作为工具结果回灌，让模型自行收尾。

这么设计的好处是"暂停"不依赖进程存活：检查点在数据库里，重启后审批仍然有效。
"""

from __future__ import annotations

APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"

# 运行时看到这个状态就停下来等审批
STATUS_PENDING_APPROVAL = "pending_approval"


class ApprovalRequired(Exception):
    """运行需要停下来等待人工审批。

    不是错误：调用方应当保留现场（检查点已由运行时写好），
    把 approval_id 返回给客户端，而不是当作失败处理。
    """

    def __init__(self, *, run_id: str, approval_id: str, tool_name: str, iteration: int) -> None:
        super().__init__(f"tool {tool_name} requires human approval")
        self.run_id = run_id
        self.approval_id = approval_id
        self.tool_name = tool_name
        self.iteration = iteration
