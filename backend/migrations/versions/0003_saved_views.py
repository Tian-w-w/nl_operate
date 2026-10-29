"""Personal saved queries, independent of expiring result sets."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('saved_views',
        sa.Column('id',sa.String(),primary_key=True),
        sa.Column('tenant_id',sa.String(),nullable=False),
        sa.Column('actor_id',sa.String(),nullable=False),
        sa.Column('name',sa.String(60),nullable=False),
        sa.Column('query',sa.JSON(),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('tenant_id','actor_id','name'))
    op.create_index('ix_saved_views_tenant_id','saved_views',['tenant_id'])
    op.create_index('ix_saved_views_actor_id','saved_views',['actor_id'])


def downgrade():
    op.drop_table('saved_views')
