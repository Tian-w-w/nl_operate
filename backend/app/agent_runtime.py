"""Durable, bounded read-only decision loop. No natural-language branch policy."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import logging
import time

from pydantic import ValidationError
from sqlalchemy import select

from .config import now
from .coordinator import authenticated_session, lock_budget
from .conversation import constrain_action, context_for, distinct_city_question, knowledge_question, status_followup
from .knowledge import grounded_answer
from .model_adapter import ModelError, ModelReply
from .models import AgentRun, AgentStep, Export, ModelCall, Plan, Session
from .schemas import CountValuesDecision, DecisionEnvelope
from .service import BusinessError, check_context, digest, export_info, get_result, statistic_message
from .tools import TOOLS, ToolContext, execute_tool

logger = logging.getLogger('llm4web')


def available(adapter, settings):
    requires_enabled = getattr(adapter, 'billable', False) or adapter.name.startswith('local-')
    return callable(getattr(adapter, 'decide', None)) and (not requires_enabled or settings.model_enabled)


def stop(db, run, code, message, state='FAILED'):
    run.state, run.phase, run.error, run.message, run.finished_at = state, state, code, message, now()
    parent = db.get(Plan, run.id)
    parent.state, parent.result = state, {'message': message, 'error': code, 'agent_run_id': run.id}
    for step in db.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.state.in_(['DECIDING', 'DECIDED', 'TOOL_RUNNING']))):
        step.state, step.error = state, code


def guard(run, session):
    if not session or session.expires_at <= now():
        raise BusinessError('SESSION_EXPIRED', '会话已过期，任务停止。')
    if run.state != 'RUNNING':
        raise BusinessError('RUN_INACTIVE', '任务已结束。')
    if now() >= run.deadline_at:
        raise BusinessError('TIME_LIMIT', '任务超过总耗时上限，已停止。')
    check_context(session, run.context_version)


def owned_run(db, token, run_id, allow_expired=False):
    session = db.scalar(select(Session).where(Session.id == token).with_for_update()) if allow_expired else authenticated_session(db, token)
    if not session:
        raise BusinessError('UNAUTHENTICATED', '会话不存在。', 401)
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.session_id == session.id).with_for_update())
    if not run:
        raise BusinessError('NOT_FOUND', '任务不存在或无权访问。', 404)
    return session, run


def start_run(factory, token, goal, version, settings, adapter):
    if not available(adapter, settings):
        raise BusinessError('AGENT_UNAVAILABLE', '只读Agent需要配置支持下一步决策的模型；当前离线规则不模拟条件决策。', 503)
    with factory.begin() as db:
        session = authenticated_session(db, token)
        check_context(session, version)
        for prior in db.scalars(select(AgentRun).where(AgentRun.session_id == session.id, AgentRun.state == 'RUNNING')):
            if prior.deadline_at <= now():
                stop(db, prior, 'TIME_LIMIT', '先前任务已超时，未自动续跑。')
            else:
                raise BusinessError('ASSISTANT_BUSY', '该会话已有只读Agent任务。')
        if db.scalar(select(ModelCall.id).where(ModelCall.session_id == session.id, ModelCall.state == 'RUNNING', ModelCall.expires_at > now())):
            raise BusinessError('ASSISTANT_BUSY', '该会话已有模型请求。')
        parent = Plan(session_id=session.id, state='RUNNING', intent='read_only_agent', instruction=goal,
                      payload={'adapter': adapter.name, 'conversation': context_for(db, session, goal)})
        db.add(parent)
        db.flush()
        run = AgentRun(id=parent.id, session_id=session.id, goal=goal, state='RUNNING', phase='QUEUED', adapter=adapter.name,
                       context_version=version, max_model_calls=settings.agent_max_model_calls, max_tool_calls=settings.agent_max_tool_calls,
                       max_seconds=settings.agent_max_seconds, repeat_limit=settings.agent_repeat_limit,
                       deadline_at=now()+timedelta(seconds=settings.agent_max_seconds))
        db.add(run)
        db.flush()
        ident = run.id
    return get_run(factory, token, ident)


def model_state(db, run):
    observations = []
    for step in db.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.ordinal)):
        if not step.observation:
            continue
        observation = dict(step.observation)
        if observation.get('tool') == 'query_orders' and observation.get('status') == 'SUCCEEDED':
            output = observation['output']
            observation['output'] = {key: output[key] for key in ['result_set_id', 'total', 'total_amount', 'query']}
        observations.append(observation)
    parent = db.get(Plan, run.id)
    return {'observations': observations, 'conversation': parent.payload.get('conversation', {}), 'remaining_model_calls': run.max_model_calls-run.model_calls,
            'remaining_tool_calls': run.max_tool_calls-run.tool_calls,
            'remaining_seconds': max(0, (run.deadline_at-now()).total_seconds())}


def prepare_decision(factory, token, run_id, settings, adapter, request_id):
    with factory.begin() as db:
        session, run = owned_run(db, token, run_id)
        guard(run, session)
        if run.model_calls >= run.max_model_calls:
            raise BusinessError('MODEL_CALL_LIMIT', '模型决策次数已到上限，已停止。')
        if getattr(adapter, 'real_model', False):
            prior_real = list(db.scalars(select(ModelCall.id).where(ModelCall.session_id == session.id, ModelCall.adapter.not_like('rules%'))))
            if len(prior_real) >= settings.model_max_calls:
                raise BusinessError('MODEL_CALL_LIMIT', '会话模型调用次数已到上限，已停止。')
        state = model_state(db, run)
        billable = bool(getattr(adapter, 'billable', False))
        reserved = adapter.estimate_decision_reservation(run.goal, settings.reference, state) if billable else Decimal(0)
        if billable:
            lock_budget(db)
            calls = list(db.scalars(select(ModelCall).where(ModelCall.billable.is_(True))))
            session_calls = [call for call in calls if call.session_id == session.id]
            def committed(items):
                return sum((call.actual_cost if call.actual_cost is not None else call.reserved_cost for call in items), Decimal(0))
            if (len(session_calls) >= settings.model_max_calls or committed(session_calls)+reserved > settings.model_budget_usd
                    or committed(calls)+reserved > settings.model_project_budget_usd):
                raise BusinessError('MODEL_BUDGET_EXCEEDED', '会话或项目模型预算不足，已停止。', 429)
        child = Plan(session_id=session.id, state='RECEIVED', intent='agent_decision', instruction=run.goal, payload={'run_id': run.id})
        db.add(child)
        db.flush()
        payload = adapter.decision_payload(run.goal, settings.reference, state) if hasattr(adapter, 'decision_payload') else {'goal': run.goal, 'state': state}
        call = ModelCall(plan_id=child.id, session_id=session.id, request_id=request_id, adapter=adapter.name,
                         model=getattr(adapter, 'model', None), billable=billable, state='RUNNING', reserved_cost=reserved,
                         actual_cost=None if billable else Decimal(0), prompt_hash=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                         expires_at=min(run.deadline_at, now()+timedelta(seconds=settings.model_timeout+30)))
        db.add(call)
        db.flush()
        run.model_calls += 1
        step = AgentStep(run_id=run.id, ordinal=run.model_calls, state='DECIDING', model_call_id=call.id)
        db.add(step)
        db.flush()
        run.phase, run.message = 'DECIDING', f'正在进行第{run.model_calls}次决策。'
        return run.goal, state, step.id, max(0, (run.deadline_at-now()).total_seconds())


def decode_decision(raw):
    proposal = raw.get('decision') if isinstance(raw, dict) else None
    if isinstance(proposal, dict) and proposal.get('kind') == 'call_tool':
        if not isinstance(proposal.get('tool'), str) or proposal.get('tool') not in TOOLS:
            raise BusinessError('UNKNOWN_TOOL', '模型提出未注册工具，已停止。')
        try:
            TOOLS[proposal['tool']].input_model.model_validate(proposal.get('arguments'))
        except ValidationError:
            raise BusinessError('INVALID_TOOL_ARGUMENTS', '模型工具参数无效，已停止。') from None
    try:
        return DecisionEnvelope.model_validate(raw).decision
    except ValidationError:
        raise BusinessError('MODEL_INVALID_DECISION', '模型决策格式无效，已停止。') from None


def consume_reply(factory, token, run_id, step_id, settings, reply, error, elapsed):
    with factory.begin() as db:
        session, run = owned_run(db, token, run_id, allow_expired=True)
        step = db.get(AgentStep, step_id)
        call = db.get(ModelCall, step.model_call_id)
        child = db.get(Plan, call.plan_id)
        step.duration_ms = call.duration_ms = elapsed
        if reply:
            if any(value is not None and (type(value) is not int or value < 0) for value in [reply.input_tokens, reply.output_tokens]):
                error = ('MODEL_INVALID_RESPONSE', '模型用量格式无效，已停止。')
            else:
                call.input_tokens, call.output_tokens = reply.input_tokens, reply.output_tokens
            call.response_hash = reply.response_hash
            if call.billable and all(type(v) is int and v >= 0 for v in [reply.input_tokens, reply.output_tokens]):
                call.actual_cost = ((Decimal(reply.input_tokens)*settings.model_input_rate+Decimal(reply.output_tokens)*settings.model_output_rate)/Decimal(1000000)).quantize(Decimal('.00000001'))
            if isinstance(reply.action, dict) and len(json.dumps(reply.action, ensure_ascii=False)) <= 50000:
                raw_decision = reply.action.get('decision')
                step.decision = raw_decision if isinstance(raw_decision, dict) else None
                child.payload = {**child.payload, 'model_proposal': step.decision}
        decision = None
        try:
            if error:
                raise BusinessError(*error)
            if call.state != 'RUNNING' or call.expires_at <= now():
                raise BusinessError('MODEL_INTERRUPTED', '模型决策已中断或过期。')
            guard(run, session)
            decision = decode_decision(reply.action)
            if knowledge_question(run.goal) and decision.kind not in {'answer_knowledge', 'clarify'}:
                raise BusinessError('KNOWLEDGE_DECISION_REQUIRED', '此问题需要依据知识库回答，不能用订单操作代替。')
            if distinct_city_question(run.goal) and decision.kind not in {'call_tool', 'finish_statistic', 'clarify'}:
                raise BusinessError('STATISTIC_DECISION_REQUIRED', '此问题需要城市去重统计，不能用普通查询或知识回答代替。')
            if distinct_city_question(run.goal) and decision.kind == 'call_tool' and decision.tool == 'query_orders':
                counted = constrain_action(run.goal, {'intent': 'query', 'filters': decision.arguments.filters.model_dump(mode='json')})
                decision = CountValuesDecision(kind='call_tool', tool='count_order_values',
                    arguments={'scope': counted['statistic_scope'], 'filters': counted.get('filters', {})})
        except BusinessError as exc:
            call.state, call.error_code, child.state = 'FAILED', exc.code, 'FAILED'
            child.result = {'error': exc.code}
            if run.state == 'RUNNING':
                stop(db, run, exc.code, exc.message)
            step.state, step.error = 'FAILED', exc.code
        else:
            call.state, child.state = 'SUCCEEDED', 'SUCCEEDED'
            step.decision = decision.model_dump(mode='json')
            child.result = step.decision
            step.state, run.phase = 'DECIDED', 'VALIDATING'
        return decision if run.state == 'RUNNING' else None


def act(factory, token, run_id, step_id, decision):
    with factory.begin() as db:
        session, run = owned_run(db, token, run_id)
        guard(run, session)
        step = db.get(AgentStep, step_id)
        if decision.kind == 'clarify':
            stop(db, run, None, decision.question, 'WAITING_CLARIFICATION')
            return None
        if decision.kind == 'answer_knowledge':
            if run.tool_calls:
                raise BusinessError('COMPLETION_EVIDENCE', '知识回答不能代替已经发起的订单操作终态。')
            parent = db.get(Plan, run.id)
            knowledge = grounded_answer(parent.payload.get('conversation', {}).get('knowledge', []),
                                         decision.answer, decision.citation_ids, generated=True)
            stop(db, run, None, knowledge['answer'], 'SUCCEEDED')
            parent.result = {**parent.result, 'knowledge': knowledge}
            step.state = 'SUCCEEDED'
            return None
        if decision.kind == 'finish_statistic':
            outcomes = [item.observation['output'] for item in db.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.ordinal))
                        if item.observation and item.observation.get('tool') == 'count_order_values' and item.observation.get('status') == 'SUCCEEDED']
            if not outcomes or outcomes[-1]['statistic_id'] != decision.statistic_id or run.result_set_id or run.export_info:
                raise BusinessError('COMPLETION_EVIDENCE', '统计结束依据必须绑定本任务的真实统计观察。')
            statistic = outcomes[-1]
            if statistic['scope'] == 'current':
                get_result(db, session, statistic['result_set_id'])
            stop(db, run, None, statistic_message(statistic), 'SUCCEEDED')
            parent = db.get(Plan, run.id)
            parent.result = {**parent.result, 'statistic': statistic}
            step.state = 'SUCCEEDED'
            return None
        if decision.kind == 'finish':
            if not run.result_set_id or decision.result_set_id != run.result_set_id:
                raise BusinessError('COMPLETION_EVIDENCE', '结束依据没有绑定本任务最近的查询结果。')
            _, orders = get_result(db, session, run.result_set_id)
            if session.result_set_id != run.result_set_id or decision.total != len(orders):
                raise BusinessError('COMPLETION_EVIDENCE', '结束依据的结果集或数量与真实状态不一致。')
            exported = [s.observation['output'] for s in db.scalars(select(AgentStep).where(AgentStep.run_id == run.id))
                        if s.observation and s.observation.get('tool') == 'export_orders' and s.observation.get('status') == 'SUCCEEDED']
            if exported:
                if len(exported) != 1 or decision.export_id != exported[0]['export_id']:
                    raise BusinessError('COMPLETION_EVIDENCE', '文件依据不一致或产生了多余导出。')
                item = db.get(Export, decision.export_id)
                if not item or item.session_id != session.id or item.result_set_id != run.result_set_id or item.row_count != len(orders):
                    raise BusinessError('COMPLETION_EVIDENCE', '导出文件没有绑定当前完整结果集。')
                run.export_info = export_info(item)
            elif decision.export_id is not None:
                raise BusinessError('COMPLETION_EVIDENCE', '未生成所声称的文件，不能报告导出成功。')
            message = f'已查询到{len(orders)}条订单，已按查询顺序导出全部结果。' if exported else f'已查询到{len(orders)}条订单，已展示列表，未生成导出文件。'
            stop(db, run, None, message, 'SUCCEEDED')
            if exported:
                parent = db.get(Plan, run.id)
                parent.result = {**parent.result, 'export': run.export_info}
            step.state = 'SUCCEEDED'
            return None
        if status_followup(run.goal) and decision.tool == 'export_orders' and '导出' not in run.goal:
            raise BusinessError('UNREQUESTED_EXPORT', '本轮只要求调整查询条件，未授权导出，已停止。')
        if run.tool_calls >= run.max_tool_calls:
            raise BusinessError('TOOL_CALL_LIMIT', '工具调用次数已到上限，已停止。')
        signature = digest({'tool': decision.tool, 'arguments': decision.arguments.model_dump(mode='json')})
        repeated = list(db.scalars(select(AgentStep.id).where(AgentStep.run_id == run.id, AgentStep.signature == signature)))
        if len(repeated) >= run.repeat_limit:
            raise BusinessError('REPEATED_TOOL_CALL', '相同工具参数重复调用，已停止。')
        step.signature, step.state, run.phase = signature, 'TOOL_RUNNING', 'TOOL_RUNNING'
        run.tool_calls += 1
        run.message = f'正在执行{decision.tool}。'
        return ToolContext(session.id, run.id, step.id, run.context_version, frozenset(run.observed_ids), run.deadline_at, run.id)


def fail_run(factory, token, run_id, code, message):
    with factory.begin() as db:
        _, run = owned_run(db, token, run_id, allow_expired=True)
        if run.state == 'RUNNING':
            stop(db, run, code, message)


def drive_run(factory, token, run_id, settings, adapter, request_id):
    try:
        with factory.begin() as db:
            session, run = owned_run(db, token, run_id)
            if run.state != 'RUNNING' or run.phase != 'QUEUED':
                return
            guard(run, session)
            run.phase = 'STARTING'
        while True:
            goal, state, step_id, remaining = prepare_decision(factory, token, run_id, settings, adapter, request_id)
            started, reply, error = time.perf_counter(), None, None
            try:
                output = adapter.decide(goal, settings.reference, state, timeout_seconds=min(settings.model_timeout, remaining))
                reply = output if isinstance(output, ModelReply) else ModelReply(output)
            except ModelError as exc:
                error = (exc.code, exc.message)
            except Exception as exc:
                logger.error('agent_decision_error run=%s type=%s', run_id, type(exc).__name__)
                error = ('MODEL_DECISION_FAILED', '模型决策异常，已停止。')
            decision = consume_reply(factory, token, run_id, step_id, settings, reply, error, int((time.perf_counter()-started)*1000))
            if decision is None:
                return
            context = act(factory, token, run_id, step_id, decision)
            if context is None:
                return
            result = execute_tool(factory, settings, decision.tool, decision.arguments.model_dump(mode='json'), context)
            if result.status == 'FAILED':
                with factory.begin() as db:
                    _, run = owned_run(db, token, run_id)
                    step = db.get(AgentStep, step_id)
                    if step.state == 'SUCCEEDED' and step.observation:
                        # A lost commit acknowledgement must not erase an atomic,
                        # already-durable tool observation or its actual artifact.
                        if run.state == 'RUNNING':
                            stop(db, run, 'TOOL_STATUS_UNCERTAIN', '提交响应异常；已保留真实观察，请核对结果。任务已停止。')
                    else:
                        step.observation, step.state, step.error = result.model_dump(mode='json'), 'FAILED', result.error.code
                        if run.state == 'RUNNING':
                            stop(db, run, result.error.code, result.error.message)
                return
    except (BusinessError, ModelError) as exc:
        fail_run(factory, token, run_id, exc.code, exc.message)
    except Exception as exc:
        logger.error('agent_runtime_error run=%s type=%s', run_id, type(exc).__name__)
        fail_run(factory, token, run_id, 'AGENT_INTERNAL_ERROR', '任务执行异常，请核对已保存步骤；未自动续跑。')


def view_run(db, run):
    steps = list(db.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.ordinal)))
    calls = [db.get(ModelCall, step.model_call_id) for step in steps if step.model_call_id]
    outcome = db.get(Plan, run.id).result or {}
    return {'id': run.id, 'goal': run.goal, 'state': run.state, 'phase': run.phase, 'adapter': run.adapter,
            'message': run.message, 'error': run.error, 'result_set_id': run.result_set_id, 'context_version': run.context_version,
            'model_calls': run.model_calls, 'tool_calls': run.tool_calls, 'max_model_calls': run.max_model_calls,
            'max_tool_calls': run.max_tool_calls, 'max_seconds': float(run.max_seconds),
            'input_tokens': sum(call.input_tokens or 0 for call in calls), 'output_tokens': sum(call.output_tokens or 0 for call in calls),
            'unknown_usage_calls': sum(call.input_tokens is None or call.output_tokens is None for call in calls),
            'estimated_cost_usd': str(sum((call.actual_cost or Decimal(0) for call in calls), Decimal(0))),
            'elapsed_ms': max(0, int(((run.finished_at or now())-run.started_at).total_seconds()*1000)),
            'export': run.export_info if run.state == 'SUCCEEDED' else None, 'knowledge': outcome.get('knowledge'), 'statistic': outcome.get('statistic'),
            'steps': [{'id': step.id, 'ordinal': step.ordinal, 'state': step.state, 'decision': step.decision,
                       'observation': step.observation, 'error': step.error, 'duration_ms': step.duration_ms} for step in steps]}


def get_run(factory, token, run_id):
    with factory.begin() as db:
        session, run = owned_run(db, token, run_id)
        if run.state == 'RUNNING':
            try:
                guard(run, session)
            except BusinessError as exc:
                stop(db, run, exc.code, exc.message)
        return view_run(db, run)


def list_runs(factory, token):
    with factory.begin() as db:
        session = authenticated_session(db, token)
        identifiers = list(db.scalars(select(AgentRun.id).where(AgentRun.session_id == session.id).order_by(AgentRun.started_at.desc()).limit(10)))
    return [get_run(factory, token, ident) for ident in identifiers]
