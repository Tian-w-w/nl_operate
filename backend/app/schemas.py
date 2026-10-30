from datetime import datetime
from decimal import Decimal
from typing import Literal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filters(Strict):
    order_id: int | None = Field(default=None, ge=1)
    shipping_cities: list[str] | None = Field(default=None, min_length=1, max_length=20)
    statuses: list[Literal["pending_payment", "paid", "shipped", "completed", "cancelled"]] | None = Field(default=None, min_length=1, max_length=5)
    additional_statuses: list[Literal["pending_payment", "paid", "shipped", "completed", "cancelled"]] | None = Field(default=None, min_length=1, max_length=5, description='Also include these statuses under the same city/amount/date filters, outside the base status and waiting-time branch.')
    customer_name: str | None = Field(default=None, min_length=1, max_length=80)
    product_keyword: str | None = Field(default=None, min_length=1, max_length=80)
    product_category: str | None = Field(default=None, min_length=1, max_length=40)
    shipping_city: str | None = Field(default=None, max_length=30)
    status: Literal["pending_payment", "paid", "shipped", "completed", "cancelled"] | None = None
    priority: bool | None = None
    review_status: Literal["normal", "pending_review"] | None = None
    amount_gte: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    amount_lte: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    created_at_gte: datetime | None = None
    created_at_lt: datetime | None = None
    paid_at_gte: datetime | None = None
    paid_at_lt: datetime | None = None
    waiting_hours_gt: int | None = Field(default=None, ge=0, le=87600)

    @field_validator('shipping_cities')
    @classmethod
    def clean_cities(cls, values):
        if values is not None:
            if any(not city.strip() or len(city) > 30 for city in values):
                raise ValueError('城市名长度无效')
            return list(dict.fromkeys(city.strip() for city in values))
        return values

    @field_validator("created_at_gte", "created_at_lt", "paid_at_gte", "paid_at_lt")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        return value

    @model_validator(mode="after")
    def valid_ranges(self):
        if self.amount_gte is not None and self.amount_lte is not None and self.amount_gte > self.amount_lte:
            raise ValueError("金额下限超过上限")
        for start, end in [(self.created_at_gte, self.created_at_lt), (self.paid_at_gte, self.paid_at_lt)]:
            if start and end and start >= end:
                raise ValueError("时间范围为空或倒置")
        return self


class Sort(Strict):
    field: Literal["id", "total_amount", "created_at", "paid_at"] = "id"
    direction: Literal["asc", "desc"] = "asc"


class QueryRequest(Strict):
    filters: Filters = Field(default_factory=Filters)
    sort: list[Sort] = Field(default_factory=lambda: [Sort()], max_length=4)
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=20, ge=1, le=100)
    expected_context_version: int = Field(ge=0)


class RefineRequest(Strict):
    filters: Filters
    expected_context_version: int = Field(ge=0)


class PageRequest(Strict):
    page: int = Field(ge=1, le=100000)
    expected_context_version: int = Field(ge=0)


class Patch(Strict):
    priority: bool | None = None
    review_status: Literal["normal", "pending_review"] | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("修改字段不能为空")
        return self


class PreviewRequest(Strict):
    result_set_id: str = Field(max_length=64)
    patch: Patch
    scope: Literal["all", "selected", "second"] = "all"
    expected_context_version: int = Field(ge=0)


class ConfirmRequest(Strict):
    digest: str = Field(min_length=64, max_length=64)


class ApplyRequest(Strict):
    idempotency_key: str = Field(min_length=8, max_length=100)


class ExportRequest(Strict):
    result_set_id: str = Field(max_length=64)
    columns: list[Literal["id", "shipping_city", "status", "total_amount", "created_at", "paid_at", "priority", "review_status"]] = Field(default_factory=lambda: ["id", "shipping_city", "status", "total_amount", "priority"], min_length=1, max_length=8)
    idempotency_key: str = Field(min_length=8, max_length=100)


class QueryOrdersInput(Strict):
    """Model-visible arguments; identity and context are supplied by the executor."""
    filters: Filters = Field(default_factory=Filters)
    sort: list[Sort] = Field(default_factory=lambda: [Sort()], max_length=4)
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=20, ge=1, le=100)
    context_mode: Literal['new', 'refine', 'revise'] = 'new'
    clear_filters: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('clear_filters')
    @classmethod
    def valid_filter_names(cls, values):
        if any(value not in Filters.model_fields for value in values):
            raise ValueError('Unknown filter to clear')
        return values


class CountOrderValuesInput(Strict):
    field: Literal['shipping_city'] = 'shipping_city'
    scope: Literal['tenant', 'current'] = 'tenant'
    filters: Filters = Field(default_factory=Filters)


class DistinctValue(Strict):
    value: str
    order_count: int


class StatisticResponse(Strict):
    statistic_id: str
    field: Literal['shipping_city']
    scope: Literal['tenant', 'current']
    result_set_id: str | None
    distinct_count: int
    order_count: int
    values: list[DistinctValue]
    basis: str


class KnowledgeSource(Strict):
    source_id: str
    title: str
    section: str
    version: str
    is_demo: bool
    excerpt: str


class KnowledgeResponse(Strict):
    answer: str
    grounded: bool
    mode: Literal['retrieval_only', 'retrieval_augmented_generation', 'no_evidence']
    sources: list[KnowledgeSource]


class ConversationMessage(Strict):
    role: Literal['user', 'assistant']
    text: str
    state: str | None = None
    knowledge: KnowledgeResponse | None = None
    statistic: StatisticResponse | None = None
    download: 'ExportResponse | None' = None
    usage: 'CallUsage | None' = None


class ExportOrdersInput(Strict):
    result_set_id: str = Field(min_length=1, max_length=64)
    columns: list[Literal["id", "shipping_city", "status", "total_amount", "created_at", "paid_at", "priority", "review_status"]] = Field(
        default_factory=lambda: ["id", "shipping_city", "status", "total_amount", "priority"], min_length=1, max_length=8)


class ToolError(Strict):
    code: str
    message: str
    retryable: Literal[False] = False


class ToolFailure(Strict):
    tool: str
    status: Literal["FAILED"] = "FAILED"
    error: ToolError


class SelectionRequest(Strict):
    ids: list[int] = Field(max_length=100)
    expected_context_version: int = Field(ge=0)


class ChatRequest(Strict):
    message: str = Field(min_length=1, max_length=1000)
    expected_context_version: int = Field(ge=0)


class DemoLogin(Strict):
    actor: Literal["operator_a", "viewer_a", "operator_b"] = "operator_a"


class OrderRow(Strict):
    id: int
    shipping_city: str
    status: Literal["pending_payment", "paid", "shipped", "completed", "cancelled"]
    total_amount: str
    created_at: str
    paid_at: str | None
    shipped_at: str | None
    priority: bool
    review_status: Literal["normal", "pending_review"]
    version: int


class QueryState(Strict):
    filters: Filters
    sort: list[Sort]
    page: int
    page_size: int


class QueryResponse(Strict):
    result_set_id: str
    rows: list[OrderRow]
    total: int
    total_amount: str
    query: QueryState
    context_version: int
    reference_time: str | None = None
    expires_at: str | None = None


class Change(Strict):
    id: int
    before: dict[str, bool | str]
    after: dict[str, bool | str]


class PreviewResponse(Strict):
    preview_id: str
    digest: str
    count: int
    result_set_id: str
    ids: list[int]
    versions: dict[str, int]
    patch: Patch
    actor_id: str
    context_version: int
    expires_at: str
    changes: list[Change]
    parent_plan_id: str | None = None


class AggregateGroup(Strict):
    group: str
    count: int
    total_amount: str


class AggregateResponse(Strict):
    group_by: Literal["shipping_city", "status"]
    result_set_id: str
    groups: list[AggregateGroup]
    total: int
    basis: str


class ExportResponse(Strict):
    export_id: str
    state: str
    row_count: int
    columns: list[str]
    checksum: str
    download_url: str


class SnapshotError(Strict):
    code: str
    message: str


class SessionResponse(Strict):
    actor: str
    tenant: str
    role: str
    context_version: int
    selected_ids: list[int]
    reference_time: str
    adapter: str
    auth_mode: str
    agent_available: bool = False
    current_result: QueryResponse | None = None
    preview: PreviewResponse | None = None
    snapshot_error: SnapshotError | None = None


class CallUsage(Strict):
    request_id: str
    billable: bool
    real_model: bool = False
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: str | None
    reserved_cost_usd: str


class ChatResponse(Strict):
    plan_id: str
    state: str
    adapter: str
    context_version: int
    message: str
    error: str | None = None
    ui_patch: QueryResponse | None = None
    preview: PreviewResponse | None = None
    aggregate: AggregateResponse | None = None
    detail: OrderRow | None = None
    export: ExportResponse | None = None
    model_usage: CallUsage | None = None
    analytics: 'AnalyticsResponse | None' = None
    audit: 'AuditResponse | None' = None
    audit_order_id: int | None = None
    knowledge: KnowledgeResponse | None = None
    statistic: StatisticResponse | None = None


class PlanSummary(Strict):
    id: str
    intent: str
    state: str
    instruction: str
    result: dict[str, Any] | None
    created_at: str


class Action(Strict):
    """A plan proposal; authentication and confirmation are never model inputs."""
    intent: Literal["query", "update", "export", "aggregate", "analytics", "audit", "detail", "page", "clarify", "refuse", "cancel", "knowledge", "count_distinct"]
    filters: Filters = Field(default_factory=Filters)
    sort: list[Sort] = Field(default_factory=lambda: [Sort()], min_length=1, max_length=4)
    patch: Patch | None = None
    scope: Literal["all", "selected", "second"] = "all"
    contextual: bool = False
    sort_explicit: bool = False
    group_by: Literal["shipping_city", "status"] | None = None
    page: int = Field(default=1, ge=1, le=100000)
    order_id: int | None = Field(default=None, ge=1)
    message: str | None = Field(default=None, max_length=500)
    context_mode: Literal['new', 'refine', 'revise'] = 'new'
    clear_filters: list[str] = Field(default_factory=list, max_length=20)
    statistic_scope: Literal['tenant', 'current'] = 'tenant'
    answer: str | None = Field(default=None, max_length=1500)
    citation_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def complete_action(self):
        if any(value not in Filters.model_fields for value in self.clear_filters):
            raise ValueError('Unknown filter to clear')
        if self.intent == 'update' and self.patch is None:
            raise ValueError('update requires a patch')
        if self.intent == 'detail' and self.order_id is None:
            raise ValueError('detail requires order_id')
        if self.intent == 'aggregate' and self.group_by is None:
            raise ValueError('aggregate requires group_by')
        if self.intent in {'clarify','refuse'} and not self.message:
            raise ValueError('clarification/refusal requires a message')
        if self.scope != 'all':
            if self.intent != 'update' or self.filters.model_dump(exclude_none=True):
                raise ValueError('selected/ordinal update requires an existing explicit scope without extra filters')
            self.contextual = True
        return self


class SavedViewCreate(Strict):
    name: str = Field(min_length=1, max_length=60)
    result_set_id: str = Field(min_length=1, max_length=64)
    expected_context_version: int = Field(ge=0)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError('名称不能为空')
        return value.strip()


class SavedViewVersion(Strict):
    expected_version: int = Field(ge=1)


class SavedViewRename(SavedViewVersion):
    name: str = Field(min_length=1,max_length=60)

    @field_validator('name')
    @classmethod
    def clean_name(cls,value):
        if not value.strip():
            raise ValueError('名称不能为空')
        return value.strip()


class ViewApply(Strict):
    expected_context_version: int = Field(ge=0)


class SavedViewResponse(Strict):
    id: str
    name: str
    query: QueryState
    version: int
    created_at: str
    updated_at: str


class AuditQuery(Strict):
    order_id: int | None = Field(default=None,ge=1)
    actor_id: str | None = Field(default=None,max_length=80)
    created_at_gte: datetime | None = None
    created_at_lt: datetime | None = None
    page: int = Field(default=1,ge=1)
    page_size: int = Field(default=20,ge=1,le=100)

    @field_validator('created_at_gte','created_at_lt')
    @classmethod
    def aware(cls,value):
        if value is not None and value.tzinfo is None:
            raise ValueError('时间必须包含时区')
        return value

    @model_validator(mode='after')
    def range_valid(self):
        if self.created_at_gte and self.created_at_lt and self.created_at_gte >= self.created_at_lt:
            raise ValueError('时间范围倒置')
        return self


class AuditEntry(Strict):
    id: str
    order_id: int
    operation_id: str
    actor_id: str
    before: dict[str,bool | str]
    after: dict[str,bool | str]
    created_at: str


class AuditResponse(Strict):
    rows: list[AuditEntry]
    total: int
    page: int
    page_size: int


class CustomerSummary(Strict):
    id: int
    name: str


class ItemSummary(Strict):
    id: int
    product_id: int
    product_name: str
    category: str
    quantity: int
    unit_price: str
    line_total: str


class ShipmentSummary(Strict):
    carrier: str
    tracking_no: str
    shipped_at: str


class OrderDetail(Strict):
    order: OrderRow
    customer: CustomerSummary | None
    items: list[ItemSummary]
    shipment: ShipmentSummary | None
    note: str
    audit: AuditResponse
    reference_time: str


class AnalyticsRequest(Strict):
    filters: Filters = Field(default_factory=Filters)
    result_set_id: str | None = Field(default=None,max_length=64)
    trend_days: int = Field(default=14,ge=1,le=90)


class AnalyticsMetrics(Strict):
    order_count: int
    total_amount: str
    average_amount: str
    paid_amount: str
    pending_amount: str
    pending_shipment_count: int
    overdue_48h_count: int
    priority_count: int
    review_count: int


class TrendPoint(Strict):
    date: str
    count: int
    total_amount: str


class AgeBucket(Strict):
    label: str
    count: int


class AnalyticsResponse(Strict):
    scope: Literal['tenant','result_set']
    result_set_id: str | None
    metrics: AnalyticsMetrics
    cities: list[AggregateGroup]
    statuses: list[AggregateGroup]
    trend: list[TrendPoint]
    waiting_buckets: list[AgeBucket]
    reference_time: str
    basis: str


class QueryDecision(Strict):
    kind: Literal['call_tool']
    tool: Literal['query_orders']
    arguments: QueryOrdersInput


class ExportDecision(Strict):
    kind: Literal['call_tool']
    tool: Literal['export_orders']
    arguments: ExportOrdersInput


class FinishDecision(Strict):
    kind: Literal['finish']
    result_set_id: str = Field(min_length=1, max_length=64)
    total: int = Field(ge=0)
    export_id: str | None = Field(default=None, max_length=64)


class ClarifyDecision(Strict):
    kind: Literal['clarify']
    question: str = Field(min_length=1, max_length=300)


class CountValuesDecision(Strict):
    kind: Literal['call_tool']
    tool: Literal['count_order_values']
    arguments: CountOrderValuesInput


class FinishStatisticDecision(Strict):
    kind: Literal['finish_statistic']
    statistic_id: str = Field(min_length=1, max_length=64)


class KnowledgeDecision(Strict):
    kind: Literal['answer_knowledge']
    answer: str = Field(min_length=1, max_length=1500)
    citation_ids: list[str] = Field(default_factory=list, max_length=3)


class DecisionEnvelope(Strict):
    decision: QueryDecision | ExportDecision | FinishDecision | ClarifyDecision | CountValuesDecision | FinishStatisticDecision | KnowledgeDecision


class AgentStartRequest(Strict):
    goal: str = Field(min_length=1, max_length=1000)
    expected_context_version: int = Field(ge=0)


class AgentStepResponse(Strict):
    id: str
    ordinal: int
    state: str
    decision: dict[str, Any] | None
    observation: dict[str, Any] | None
    error: str | None
    duration_ms: int | None


class AgentRunResponse(Strict):
    id: str
    goal: str
    state: Literal['RUNNING', 'SUCCEEDED', 'FAILED', 'WAITING_CLARIFICATION']
    phase: str
    adapter: str
    message: str
    error: str | None
    result_set_id: str | None
    context_version: int
    model_calls: int
    tool_calls: int
    max_model_calls: int
    max_tool_calls: int
    max_seconds: float
    input_tokens: int
    output_tokens: int
    unknown_usage_calls: int
    estimated_cost_usd: str
    elapsed_ms: int
    export: ExportResponse | None
    steps: list[AgentStepResponse]
    knowledge: KnowledgeResponse | None = None
    statistic: StatisticResponse | None = None
