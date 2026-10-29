"""Immutable v1 migration schema. Do not import mutable ORM metadata here."""
import sqlalchemy as sa

metadata = sa.MetaData()

sa.Table(
    'customers', metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False, index=True),
    sa.Column('name', sa.String(), nullable=False),
)

sa.Table(
    'products', metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False, index=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('category', sa.String(), nullable=False),
)

sa.Table(
    'sessions', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('actor_id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('context_version', sa.Integer(), nullable=False),
    sa.Column('result_set_id', sa.String(), nullable=True),
    sa.Column('page_ids', sa.JSON(), nullable=False),
    sa.Column('selected_ids', sa.JSON(), nullable=False),
    sa.Column('pending_plan_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    'orders', metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False, index=True),
    sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=False),
    sa.Column('shipping_city', sa.String(), nullable=False, index=True),
    sa.Column('status', sa.String(), nullable=False, index=True),
    sa.Column('total_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('shipped_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('priority', sa.Boolean(), nullable=False),
    sa.Column('review_status', sa.String(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
)

sa.Table(
    'plans', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('session_id', sa.String(), sa.ForeignKey('sessions.id'), nullable=False, index=True),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('intent', sa.String(), nullable=False),
    sa.Column('instruction', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    'result_sets', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('session_id', sa.String(), sa.ForeignKey('sessions.id'), nullable=False, index=True),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('query', sa.JSON(), nullable=False),
    sa.Column('ids', sa.JSON(), nullable=False),
    sa.Column('versions', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    'confirmations', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('plan_id', sa.String(), sa.ForeignKey('plans.id'), nullable=False, unique=True),
    sa.Column('session_id', sa.String(), sa.ForeignKey('sessions.id'), nullable=False),
    sa.Column('digest', sa.String(), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    'exports', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('session_id', sa.String(), sa.ForeignKey('sessions.id'), nullable=False),
    sa.Column('result_set_id', sa.String(), sa.ForeignKey('result_sets.id'), nullable=False),
    sa.Column('idempotency_key', sa.String(), nullable=False),
    sa.Column('digest', sa.String(), nullable=False),
    sa.Column('columns', sa.JSON(), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('checksum', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint('session_id', 'idempotency_key'),
)

sa.Table(
    'operations', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('session_id', sa.String(), sa.ForeignKey('sessions.id'), nullable=False),
    sa.Column('plan_id', sa.String(), sa.ForeignKey('plans.id'), nullable=False, unique=True),
    sa.Column('idempotency_key', sa.String(), nullable=False),
    sa.Column('digest', sa.String(), nullable=False),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint('session_id', 'idempotency_key'),
)

sa.Table(
    'order_items', metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id'), nullable=False),
    sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=False),
)

sa.Table(
    'plan_steps', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('plan_id', sa.String(), sa.ForeignKey('plans.id'), nullable=False, index=True),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('tool', sa.String(), nullable=False),
    sa.Column('args', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
)

sa.Table(
    'shipments', metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id'), nullable=False, unique=True),
    sa.Column('carrier', sa.String(), nullable=False),
    sa.Column('tracking_no', sa.String(), nullable=False),
    sa.Column('shipped_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    'audit_logs', metadata,
    sa.Column('id', sa.String(), primary_key=True, nullable=False),
    sa.Column('operation_id', sa.String(), sa.ForeignKey('operations.id'), nullable=False, index=True),
    sa.Column('actor_id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id'), nullable=False),
    sa.Column('before', sa.JSON(), nullable=False),
    sa.Column('after', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)
