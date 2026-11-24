"""add tool invocation offload markers

Revision ID: b3d9a1c47e20
Revises: 1c42cde4341a
Create Date: 2026-11-24 23:40:00.000000

新增两列，记录「这次工具结果是否因过大而从模型上下文里旁路掉了」，
以及结果序列化后的字符数。完整结果本来就在 result 列里，没有新增表。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'b3d9a1c47e20'
down_revision = '1c42cde4341a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tool_invocations', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('offloaded', sa.Boolean(), server_default='0', nullable=False),
        )
        batch_op.add_column(
            sa.Column('result_chars', sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table('tool_invocations', schema=None) as batch_op:
        batch_op.drop_column('result_chars')
        batch_op.drop_column('offloaded')
