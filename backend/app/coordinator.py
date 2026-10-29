"""Persist intent -> release DB -> parse -> revalidate and execute a short transaction."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import logging
import time

from pydantic import ValidationError
from sqlalchemy import select

from .agent import execute_plan
from .config import now
from .model_adapter import ModelError, ModelReply
from .models import ModelBudgetLock, ModelCall, Plan, ResultSet, Session
from .schemas import Action
from .service import BusinessError, check_context

logger = logging.getLogger('llm4web')


def authenticated_session(db, token, lock=True):
    statement = select(Session).where(Session.id == token)
    session = db.scalar(statement.with_for_update() if lock else statement) if token else None
    if not session or session.expires_at <= now():
        raise BusinessError('UNAUTHENTICATED','请登录本地演示身份',401)
    return session


def fail_plan(plan, code, message):
    plan.state = 'FAILED'
    plan.payload = {**plan.payload,'trace':plan.payload.get('trace',[]) + ['FAILED']}
    plan.result = {'error':code,'message':message}


def expire_attempts(db, session):
    for call in db.scalars(select(ModelCall).where(ModelCall.session_id == session.id, ModelCall.state == 'RUNNING', ModelCall.expires_at <= now())):
        call.state, call.error_code = 'UNKNOWN', 'MODEL_INTERRUPTED'
        plan = db.get(Plan,call.plan_id)
        if plan.state == 'RECEIVED':
            fail_plan(plan,'MODEL_INTERRUPTED','解析过程已中断或超时；未执行业务操作，请重新发起任务。')


def lock_budget(db):
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(ModelBudgetLock).values(id='project').on_conflict_do_nothing(index_elements=['id']))
    db.scalar(select(ModelBudgetLock).where(ModelBudgetLock.id == 'project').with_for_update())


def run_chat(factory, token, message, version, settings, adapter, request_id):
    # Phase one is durable. Neither a session lock nor a checked-out connection spans parse().
    with factory.begin() as db:
        session = authenticated_session(db,token)
        expire_attempts(db,session)
        check_context(session,version)
        if db.scalar(select(ModelCall.id).where(ModelCall.session_id == session.id,ModelCall.state == 'RUNNING')):
            raise BusinessError('ASSISTANT_BUSY','该会话已有解析任务，请稍后重试。')
        context = {'has_result':bool(session.result_set_id),'visible_ids':session.page_ids,'selected_ids':session.selected_ids}
        if session.result_set_id:
            result = db.get(ResultSet,session.result_set_id)
            context.update({'query':result.query,'total':len(result.ids)})
        billable = bool(getattr(adapter,'billable',False))
        try:
            reserved = adapter.estimate_reservation(message,settings.reference,context) if billable else Decimal(0)
        except ModelError as exc:
            raise BusinessError(exc.code,exc.message,503) from None
        if billable:
            lock_budget(db)
        attempts = list(db.scalars(select(ModelCall).where(ModelCall.session_id == session.id,ModelCall.billable.is_(True))))
        committed = sum((c.actual_cost if c.actual_cost is not None else c.reserved_cost for c in attempts),Decimal(0))
        all_calls = list(db.scalars(select(ModelCall).where(ModelCall.billable.is_(True)))) if billable else []
        project_committed = sum((c.actual_cost if c.actual_cost is not None else c.reserved_cost for c in all_calls),Decimal(0))
        if billable and (len(attempts) >= settings.model_max_calls or committed + reserved > settings.model_budget_usd or project_committed + reserved > settings.model_project_budget_usd):
            raise BusinessError('MODEL_BUDGET_EXCEEDED','该会话模型调用次数或费用预算已用尽。',429)
        plan = Plan(session_id=session.id,instruction=message,state='RECEIVED',intent='unparsed',
                    payload={'adapter':adapter.name,'trace':['RECEIVED'],'request_id':request_id,'expected_context_version':version})
        db.add(plan)
        db.flush()
        call = ModelCall(plan_id=plan.id,session_id=session.id,request_id=request_id,adapter=adapter.name,
                         model=getattr(adapter,'model',None),billable=billable,state='RUNNING',reserved_cost=reserved,
                         actual_cost=None if billable else Decimal(0),
                         prompt_hash=hashlib.sha256(json.dumps(adapter.payload(message,settings.reference,context) if hasattr(adapter,'payload') else {'message':message,'context':context,'reference':settings.reference.isoformat(),'adapter':adapter.name},ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
                         expires_at=now()+timedelta(seconds=settings.model_timeout+30))
        db.add(call)
        db.flush()
        plan_id, call_id = plan.id, call.id
    start, reply, parsed, error = time.perf_counter(), None, None, None
    try:
        output = adapter.parse(message,settings.reference,context)
        reply = output if isinstance(output,ModelReply) else ModelReply(output)
        parsed = Action.model_validate(reply.action).model_dump(mode='json',exclude_none=True)
    except ModelError as exc:
        error = (exc.code,exc.message)
    except ValidationError:
        error = ('MODEL_INVALID_ACTION','解析结果不符合受限操作契约，未执行业务操作。')
    except Exception as exc:
        error = ('PARSER_ERROR','解析器异常，未执行业务操作。')
        logger.error(json.dumps({'event':'parser_error','request_id':request_id,'plan_id':plan_id,'type':type(exc).__name__}))
    elapsed = int((time.perf_counter()-start)*1000)
    with factory.begin() as db:
        session = db.scalar(select(Session).where(Session.id == token).with_for_update())
        plan, call = db.get(Plan,plan_id), db.get(ModelCall,call_id)
        call.duration_ms = elapsed
        if reply:
            call.input_tokens, call.output_tokens, call.response_hash = reply.input_tokens, reply.output_tokens, reply.response_hash
            if billable and reply.input_tokens is not None and reply.output_tokens is not None:
                call.actual_cost = ((Decimal(reply.input_tokens)*settings.model_input_rate + Decimal(reply.output_tokens)*settings.model_output_rate)/Decimal(1000000)).quantize(Decimal('.00000001'))
        if call.state != 'RUNNING' or call.expires_at <= now():
            error = ('MODEL_INTERRUPTED','解析结果已过期，未执行业务操作。')
        if not session or session.expires_at <= now():
            error = ('SESSION_EXPIRED','会话已过期，未执行业务操作。')
        elif session.context_version != version:
            error = ('STALE_CONTEXT','解析期间页面已变化，请基于当前结果重新发起任务。')
        if error:
            fail_plan(plan,*error)
            call.state,call.error_code = 'FAILED',error[0]
            response = {'plan_id':plan_id,'state':'FAILED','adapter':adapter.name,
                        'context_version':session.context_version if session else version,**plan.result}
        else:
            call.state = 'SUCCEEDED'
            response = execute_plan(db,session,plan,parsed,settings)
        response['model_usage'] = {'request_id':request_id,'billable':billable,'duration_ms':elapsed,
                                   'input_tokens':call.input_tokens,'output_tokens':call.output_tokens,
                                   'estimated_cost_usd':str(call.actual_cost) if call.actual_cost is not None else None,
                                   'reserved_cost_usd':str(call.reserved_cost)}
        logger.info(json.dumps({'event':'assistant_completed','request_id':request_id,'plan_id':plan_id,'state':response['state'],
                                'adapter':adapter.name,'duration_ms':elapsed,'error_code':response.get('error')},ensure_ascii=False))
        return response
