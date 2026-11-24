"""add tool artifacts

Revision ID: 7c4a1e9b2d10
Revises: ba1af77bd7ce
Create Date: 2026-11-23 23:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '7c4a1e9b2d10'
down_revision = 'ba1af77bd7ce'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tool_artifacts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('invocation_id', sa.String(length=36), nullable=False),
        sa.Column('run_id', sa.String(length=40), nullable=False),
        sa.Column('tool_name', sa.String(length=120), nullable=False),
        sa.Column('path', sa.String(length=400), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'invocation_id', 'path', name='uq_tool_artifacts_invocation_path'
        ),
    )
    with op.batch_alter_table('tool_artifacts', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_tool_artifacts_tenant_id'), ['tenant_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_tool_artifacts_invocation_id'), ['invocation_id'], unique=False
        )
        batch_op.create_index(batch_op.f('ix_tool_artifacts_run_id'), ['run_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('tool_artifacts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tool_artifacts_run_id'))
        batch_op.drop_index(batch_op.f('ix_tool_artifacts_invocation_id'))
        batch_op.drop_index(batch_op.f('ix_tool_artifacts_tenant_id'))

    op.drop_table('tool_artifacts')
