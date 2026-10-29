"""Durable parser/model attempts and conservative budget reservations."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('model_budget_locks',sa.Column('id',sa.String(),primary_key=True))
    op.create_table('model_calls',
        sa.Column('id',sa.String(),primary_key=True),
        sa.Column('plan_id',sa.String(),sa.ForeignKey('plans.id'),nullable=False,unique=True),
        sa.Column('session_id',sa.String(),sa.ForeignKey('sessions.id'),nullable=False),
        sa.Column('request_id',sa.String(),nullable=False),
        sa.Column('adapter',sa.String(),nullable=False), sa.Column('model',sa.String()),
        sa.Column('billable',sa.Boolean(),nullable=False), sa.Column('state',sa.String(),nullable=False),
        sa.Column('reserved_cost',sa.Numeric(16,8),nullable=False), sa.Column('actual_cost',sa.Numeric(16,8)),
        sa.Column('input_tokens',sa.Integer()),sa.Column('output_tokens',sa.Integer()),
        sa.Column('duration_ms',sa.Integer()),sa.Column('error_code',sa.String()),
        sa.Column('prompt_hash',sa.String(),nullable=False),sa.Column('response_hash',sa.String()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_model_calls_session_id','model_calls',['session_id'])
    op.create_index('ix_model_calls_request_id','model_calls',['request_id'])


def downgrade():
    op.drop_table('model_calls')
    op.drop_table('model_budget_locks')
