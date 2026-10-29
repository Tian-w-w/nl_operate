"""Initial synthetic business and execution schema.

Revision ID: 0001
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Initial snapshot is materialized here after schema validation.
    from backend.migrations.schema_v1 import metadata
    metadata.create_all(bind=op.get_bind())


def downgrade():
    from backend.migrations.schema_v1 import metadata
    metadata.drop_all(bind=op.get_bind())
