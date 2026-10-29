from datetime import timezone
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import TypeDecorator

from .config import now

Base = declarative_base()


def uid():
    return uuid4().hex


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("timezone required")
            return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    shipping_city = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    total_amount = Column(Numeric(12, 2), nullable=False)
    created_at = Column(UTCDateTime, nullable=False, index=True)
    paid_at = Column(UTCDateTime)
    shipped_at = Column(UTCDateTime)
    priority = Column(Boolean, nullable=False, default=False)
    review_status = Column(String, nullable=False, default="normal")
    version = Column(Integer, nullable=False, default=1)
    note = Column(Text, nullable=False, default="")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String, nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)


class Shipment(Base):
    __tablename__ = "shipments"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String, nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, unique=True)
    carrier = Column(String, nullable=False)
    tracking_no = Column(String, nullable=False)
    shipped_at = Column(UTCDateTime, nullable=False)


class SavedView(Base):
    __tablename__ = 'saved_views'
    __table_args__ = (UniqueConstraint('tenant_id','actor_id','name'),)
    id = Column(String,primary_key=True,default=uid)
    tenant_id = Column(String,nullable=False,index=True)
    actor_id = Column(String,nullable=False,index=True)
    name = Column(String(60),nullable=False)
    query = Column(JSON,nullable=False)
    version = Column(Integer,nullable=False,default=1)
    created_at = Column(UTCDateTime,nullable=False,default=now)
    updated_at = Column(UTCDateTime,nullable=False,default=now)


class Session(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True, default=uid)
    actor_id = Column(String, nullable=False)
    tenant_id = Column(String, nullable=False)
    role = Column(String, nullable=False)
    context_version = Column(Integer, nullable=False, default=0)
    result_set_id = Column(String)
    page_ids = Column(JSON, nullable=False, default=list)
    selected_ids = Column(JSON, nullable=False, default=list)
    pending_plan_id = Column(String)
    created_at = Column(UTCDateTime, nullable=False, default=now)
    expires_at = Column(UTCDateTime, nullable=False)


class ResultSet(Base):
    __tablename__ = "result_sets"
    id = Column(String, primary_key=True, default=uid)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False, index=True)
    tenant_id = Column(String, nullable=False)
    query = Column(JSON, nullable=False)
    ids = Column(JSON, nullable=False)
    versions = Column(JSON, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=now)
    expires_at = Column(UTCDateTime, nullable=False)


class Plan(Base):
    __tablename__ = "plans"
    id = Column(String, primary_key=True, default=uid)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False, index=True)
    state = Column(String, nullable=False)
    intent = Column(String, nullable=False)
    instruction = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    result = Column(JSON)
    created_at = Column(UTCDateTime, nullable=False, default=now)


class PlanStep(Base):
    __tablename__ = "plan_steps"
    id = Column(String, primary_key=True, default=uid)
    plan_id = Column(String, ForeignKey("plans.id"), nullable=False, index=True)
    state = Column(String, nullable=False)
    tool = Column(String, nullable=False)
    args = Column(JSON, nullable=False, default=dict)
    result = Column(JSON)


class ModelBudgetLock(Base):
    __tablename__ = 'model_budget_locks'
    id = Column(String, primary_key=True)


class ModelCall(Base):
    __tablename__ = 'model_calls'
    id = Column(String, primary_key=True, default=uid)
    plan_id = Column(String, ForeignKey('plans.id'), nullable=False, unique=True)
    session_id = Column(String, ForeignKey('sessions.id'), nullable=False, index=True)
    request_id = Column(String, nullable=False, index=True)
    adapter = Column(String, nullable=False)
    model = Column(String)
    billable = Column(Boolean, nullable=False)
    state = Column(String, nullable=False)
    reserved_cost = Column(Numeric(16, 8), nullable=False)
    actual_cost = Column(Numeric(16, 8))
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    duration_ms = Column(Integer)
    error_code = Column(String)
    prompt_hash = Column(String, nullable=False)
    response_hash = Column(String)
    created_at = Column(UTCDateTime, nullable=False, default=now)
    expires_at = Column(UTCDateTime, nullable=False)


class Confirmation(Base):
    __tablename__ = "confirmations"
    id = Column(String, primary_key=True, default=uid)
    plan_id = Column(String, ForeignKey("plans.id"), nullable=False, unique=True)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    digest = Column(String, nullable=False)
    confirmed_at = Column(UTCDateTime, nullable=False, default=now)
    expires_at = Column(UTCDateTime, nullable=False)


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key"),)
    id = Column(String, primary_key=True, default=uid)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    plan_id = Column(String, ForeignKey("plans.id"), nullable=False, unique=True)
    idempotency_key = Column(String, nullable=False)
    digest = Column(String, nullable=False)
    state = Column(String, nullable=False)
    result = Column(JSON, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String, primary_key=True, default=uid)
    operation_id = Column(String, ForeignKey("operations.id"), nullable=False, index=True)
    actor_id = Column(String, nullable=False)
    tenant_id = Column(String, nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    before = Column(JSON, nullable=False)
    after = Column(JSON, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=now)


class Export(Base):
    __tablename__ = "exports"
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key"),)
    id = Column(String, primary_key=True, default=uid)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False)
    result_set_id = Column(String, ForeignKey("result_sets.id"), nullable=False)
    idempotency_key = Column(String, nullable=False)
    digest = Column(String, nullable=False)
    columns = Column(JSON, nullable=False)
    row_count = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    checksum = Column(String, nullable=False)
    created_at = Column(UTCDateTime, nullable=False, default=now)


class AgentRun(Base):
    __tablename__ = 'agent_runs'
    id = Column(String, ForeignKey('plans.id'), primary_key=True)
    session_id = Column(String, ForeignKey('sessions.id'), nullable=False, index=True)
    goal = Column(Text, nullable=False)
    state = Column(String, nullable=False)
    phase = Column(String, nullable=False)
    adapter = Column(String, nullable=False)
    message = Column(Text, nullable=False, default='任务已接收。')
    error = Column(String)
    context_version = Column(Integer, nullable=False)
    result_set_id = Column(String)
    observed_ids = Column(JSON, nullable=False, default=list)
    export_info = Column(JSON)
    model_calls = Column(Integer, nullable=False, default=0)
    tool_calls = Column(Integer, nullable=False, default=0)
    max_model_calls = Column(Integer, nullable=False)
    max_tool_calls = Column(Integer, nullable=False)
    max_seconds = Column(Numeric(8, 3), nullable=False)
    repeat_limit = Column(Integer, nullable=False)
    started_at = Column(UTCDateTime, nullable=False, default=now)
    deadline_at = Column(UTCDateTime, nullable=False)
    finished_at = Column(UTCDateTime)


class AgentStep(Base):
    __tablename__ = 'agent_steps'
    __table_args__ = (UniqueConstraint('run_id', 'ordinal'),)
    id = Column(String, primary_key=True, default=uid)
    run_id = Column(String, ForeignKey('agent_runs.id'), nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    state = Column(String, nullable=False)
    decision = Column(JSON)
    observation = Column(JSON)
    signature = Column(String)
    error = Column(String)
    model_call_id = Column(String, ForeignKey('model_calls.id'))
    duration_ms = Column(Integer)
    created_at = Column(UTCDateTime, nullable=False, default=now)
