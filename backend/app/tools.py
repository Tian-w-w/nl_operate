"""Two read-only business tools. No planning, conditional branching or model calls.

Each invocation owns a short transaction and returns success only AFTER commit.
Trusted context must come from the server, never from model-generated arguments.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import logging
from types import MappingProxyType
from typing import Literal

from pydantic import ValidationError

from . import service
from .config import now
from .models import AgentRun, AgentStep
from .schemas import (ExportOrdersInput, ExportRequest, ExportResponse, QueryOrdersInput,
                      QueryRequest, QueryResponse, Strict, ToolError, ToolFailure)


class QueryToolSuccess(Strict):
    tool: Literal['query_orders'] = 'query_orders'
    status: Literal['SUCCEEDED'] = 'SUCCEEDED'
    context_version: int
    output: QueryResponse


class CompletedExport(ExportResponse):
    state: Literal['SUCCEEDED']


class ExportToolSuccess(Strict):
    tool: Literal['export_orders'] = 'export_orders'
    status: Literal['SUCCEEDED'] = 'SUCCEEDED'
    context_version: int
    output: CompletedExport


@dataclass(frozen=True)
class ToolContext:
    session_token: str = field(repr=False)
    task_id: str
    call_id: str
    expected_context_version: int
    # Later populated from persisted successful query observations by the runner.
    observed_result_set_ids: frozenset[str] = frozenset()
    deadline_at: datetime | None = None
    run_id: str | None = None

    def __post_init__(self):
        if not self.task_id or not self.call_id or self.expected_context_version < 0:
            raise ValueError('Invalid trusted tool context')
        object.__setattr__(self, 'observed_result_set_ids', frozenset(self.observed_result_set_ids))

    @property
    def idempotency_key(self):
        # Length-prefix encoding prevents ambiguous task/call concatenations.
        value = f'{len(self.task_id)}:{self.task_id}{self.call_id}'
        return 'agent-' + hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[Strict]
    success_model: type[Strict]


TOOLS = MappingProxyType({
    'query_orders': ToolSpec('query_orders', '查询当前租户订单；生成完整结果集并更新当前展示页。', QueryOrdersInput, QueryToolSuccess),
    'export_orders': ToolSpec('export_orders', '导出本任务已观察的当前完整结果集，保持原有顺序；不只导出当前页。', ExportOrdersInput, ExportToolSuccess),
})


def tool_catalog():
    """Fresh JSON schemas, with no credentials, oracle or trusted context fields."""
    return [{'name': spec.name, 'description': spec.description,
             'input_schema': spec.input_model.model_json_schema(),
             'success_schema': spec.success_model.model_json_schema(),
             'error_schema': ToolFailure.model_json_schema()} for spec in TOOLS.values()]


def failure(name, code, message):
    return ToolFailure(tool=name, error=ToolError(code=code, message=message))


def execute_tool(factory, settings, name, arguments, context: ToolContext):
    """Independent server API, deliberately not an unauthenticated HTTP endpoint.

    Caller records returned observations and advances context_version only on
    success. A failure rolls back all tool effects and terminates this attempt.
    """
    if not isinstance(name, str):
        return failure('(invalid)', 'UNKNOWN_TOOL', '工具名称必须为注册名称。')
    spec = TOOLS.get(name)
    if spec is None:
        return failure(name, 'UNKNOWN_TOOL', '未注册的工具，未执行操作。')
    try:
        body = spec.input_model.model_validate(arguments)
    except ValidationError:
        return failure(name, 'INVALID_TOOL_ARGUMENTS', '工具参数不符合契约，未执行操作。')
    try:
        # Local import keeps the existing single-step coordinator independent.
        from .coordinator import authenticated_session
        with factory.begin() as db:
            session = authenticated_session(db, context.session_token)
            service.check_context(session, context.expected_context_version)
            run = step = None
            if context.deadline_at and now() >= context.deadline_at:
                raise service.BusinessError('TIME_LIMIT', '任务已超过总耗时上限。')
            if context.run_id:
                run, step = db.get(AgentRun, context.run_id), db.get(AgentStep, context.call_id)
                if not run or run.session_id != session.id or run.state != 'RUNNING' or not step or step.run_id != run.id or step.state != 'TOOL_RUNNING':
                    raise service.BusinessError('RUN_INACTIVE', '任务或步骤已结束，未执行工具。')
                if now() >= run.deadline_at:
                    raise service.BusinessError('TIME_LIMIT', '任务已超过总耗时上限。')
            if name == 'query_orders':
                request = QueryRequest(**body.model_dump(), expected_context_version=context.expected_context_version)
                result = service.query_orders(db, session, request, settings)
                response = QueryToolSuccess(context_version=session.context_version, output=result)
            else:
                if body.result_set_id not in context.observed_result_set_ids:
                    raise service.BusinessError('RESULT_NOT_OBSERVED', '只能引用本任务查询工具返回的结果集。', 422)
                service.get_result(db, session, body.result_set_id)
                if body.result_set_id != session.result_set_id:
                    raise service.BusinessError('STALE_CONTEXT', '当前结果集已变化，请重新发起任务。')
                # Do this even for a retry: an old observation must not bypass
                # expiry/version checks through export service's idempotent path.
                request = ExportRequest(**body.model_dump(), idempotency_key=context.idempotency_key)
                result = service.export_orders(db, session, request)
                response = ExportToolSuccess(context_version=session.context_version, output=result)
            if (context.deadline_at and now() >= context.deadline_at) or (run and now() >= run.deadline_at):
                raise service.BusinessError('TIME_LIMIT', '工具执行已超过任务总耗时，本次操作已撤销。')
            if run:
                # Business artifacts, page state and durable observation commit together.
                step.state, step.observation = 'SUCCEEDED', response.model_dump(mode='json')
                run.context_version, run.phase = session.context_version, 'OBSERVED'
                if name == 'query_orders':
                    run.result_set_id = response.output.result_set_id
                    run.observed_ids = [*run.observed_ids, run.result_set_id]
        return response
    except service.BusinessError as exc:
        return failure(name, exc.code, exc.message)
    except Exception as exc:
        logging.getLogger('llm4web').error('tool_execution_failed task=%s call=%s tool=%s type=%s',
                                         context.task_id, context.call_id, name, type(exc).__name__)
        return failure(name, 'TOOL_EXECUTION_FAILED', '工具执行失败，请核对已保存步骤与文件；本次未报告成功。')
