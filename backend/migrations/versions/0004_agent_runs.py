"""Durable bounded read-only Agent runs and decisions."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('agent_runs',
        sa.Column('id', sa.String(), sa.ForeignKey('plans.id'), primary_key=True),
        sa.Column('session_id', sa.String(), sa.ForeignKey('sessions.id'), nullable=False),
        sa.Column('goal', sa.Text(), nullable=False), sa.Column('state', sa.String(), nullable=False),
        sa.Column('phase', sa.String(), nullable=False), sa.Column('adapter', sa.String(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False), sa.Column('error', sa.String()),
        sa.Column('context_version', sa.Integer(), nullable=False), sa.Column('result_set_id', sa.String()),
        sa.Column('observed_ids', sa.JSON(), nullable=False), sa.Column('export_info', sa.JSON()),
        sa.Column('model_calls', sa.Integer(), nullable=False), sa.Column('tool_calls', sa.Integer(), nullable=False),
        sa.Column('max_model_calls', sa.Integer(), nullable=False), sa.Column('max_tool_calls', sa.Integer(), nullable=False),
        sa.Column('max_seconds', sa.Numeric(8, 3), nullable=False), sa.Column('repeat_limit', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False), sa.Column('finished_at', sa.DateTime(timezone=True)))
    op.create_index('ix_agent_runs_session_id', 'agent_runs', ['session_id'])
    op.create_table('agent_steps',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('run_id', sa.String(), sa.ForeignKey('agent_runs.id'), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False), sa.Column('state', sa.String(), nullable=False),
        sa.Column('decision', sa.JSON()), sa.Column('observation', sa.JSON()), sa.Column('signature', sa.String()),
        sa.Column('error', sa.String()), sa.Column('model_call_id', sa.String(), sa.ForeignKey('model_calls.id')),
        sa.Column('duration_ms', sa.Integer()), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('run_id', 'ordinal'))
    op.create_index('ix_agent_steps_run_id', 'agent_steps', ['run_id'])


def downgrade():
    op.drop_table('agent_steps')
    op.drop_table('agent_runs')
