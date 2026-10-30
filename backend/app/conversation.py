"""Bounded persisted conversation memory and explicit query revisions."""
import re

from sqlalchemy import select

from .knowledge import retrieve
from .models import Plan
from .schemas import Filters, QueryOrdersInput, QueryRequest
from . import service
from .service import BusinessError


def recent_plans(db, session, limit=12):
    return list(reversed(list(db.scalars(select(Plan).where(Plan.session_id == session.id,
        Plan.intent.not_in(['agent_decision', 'apply_order_update'])).order_by(Plan.created_at.desc(), Plan.id.desc()).limit(limit)))))


def messages(db, session):
    result = []
    for plan in recent_plans(db, session):
        result.append({'role': 'user', 'text': plan.instruction})
        outcome = plan.result or {}
        if outcome.get('message'):
            item = {'role': 'assistant', 'text': outcome['message'], 'state': plan.state}
            for key in ['knowledge', 'statistic']:
                if outcome.get(key):
                    item[key] = outcome[key]
            if outcome.get('export'):
                item['download'] = outcome['export']
            if outcome.get('model_usage'):
                item['usage'] = outcome['model_usage']
            result.append(item)
    return result


def context_for(db, session, question):
    context = {'has_result': False, 'history': []}
    plans = recent_plans(db, session, 6)
    for plan in plans:
        context['history'].append({'user': plan.instruction[:500], 'assistant': (plan.result or {}).get('message', '')[:450], 'state': plan.state})
    retrieval_question = question
    if (plans and (plans[-1].result or {}).get('knowledge') and len(normalize(question)) <= 15
            and re.match(r'^(那|这个|这种|它|如果)', normalize(question)) and not knowledge_question(question)):
        retrieval_question = plans[-1].instruction+' '+question
    context['knowledge'] = retrieve(retrieval_question, session.tenant_id)
    if session.result_set_id:
        try:
            result, _ = service.get_result(db, session, session.result_set_id)
            context.update({'has_result': True, 'result_set_id': result.id, 'query': result.query, 'total': len(result.ids)})
        except BusinessError as exc:
            context['query_error'] = exc.code
    hint = status_followup(question)
    if hint:
        context['query_revision'] = hint
    return context


def normalize(text):
    return re.sub(r'[\s，,。！!？?、；;]', '', text).replace('收获城市', '收货城市')


def distinct_city_question(text):
    return bool(re.search(r'(?:多少(?:个|种)?|几(?:个|种)).{0,4}(?:收货)?城市|(?:收货)?城市.{0,4}(?:数量|个数|种数|总数)', normalize(text)))


def knowledge_question(text):
    return any(word in normalize(text) for word in ['发货时间', '发货时限', '多久发货', '什么时候发货', '默认快递', '默认发', '什么快递', '哪家快递', '运费', '包邮', '发货规定', '业务规则', '退货政策', '节假日'])


def statistic_scope(text):
    return 'current' if any(word in text for word in ['这些', '其中', '当前结果', '当前列表', '这批']) else 'tenant'


def status_followup(instruction):
    text = normalize(instruction)
    if any(word in text for word in ['删除', '退款', '取消订单', '重新查询', '从头查询', '不包含', '不要包含', '不包括']):
        return None
    if re.search(r'\d', text) or any(word in text for word in ['上海', '北京', '杭州', '深圳', '成都', '广州', '金额', '排序', '升序', '降序', '从高到低', '从低到高', '上周', '本周', '今天', '商品', '客户', '优先', '复核']):
        return None
    states = [value for label, value in [('已发货', 'shipped'), ('已完成', 'completed'), ('待付款', 'pending_payment'), ('已取消', 'cancelled')] if label in text]
    if not states:
        return None
    if any(word in text for word in ['一并', '一起', '也查', '也加', '加上', '也包含', '也包括', '算上']) or ('也' in text and any(word in text for word in ['查', '看', '包括', '包含'])):
        return {'operation': 'append_status', 'statuses': states}
    if any(word in text for word in ['只看', '只查', '改为', '换成', '换为', '那已']):
        return {'operation': 'replace_status', 'statuses': states}
    return None


def revise_spec(spec, context, instruction):
    """Explicit additive follow-ups cannot lose the previous city, amount or order."""
    hint = status_followup(instruction)
    if not hint and spec.context_mode == 'new':
        return spec
    if not context.get('has_result'):
        raise BusinessError(context.get('query_error', 'MISSING_RESULT'), '当前查询不可用，请先查询订单，再追加或修改条件。', 422)
    previous = context['query']
    if hint:
        merged = {key: value for key, value in previous['filters'].items() if value is not None}
        states = hint['statuses']
        if hint['operation'] == 'append_status':
            if merged.get('waiting_hours_gt') is not None:
                if not merged.get('status') and not merged.get('statuses'):
                    merged['status'] = 'paid'
                merged['additional_statuses'] = list(dict.fromkeys([*merged.get('additional_statuses', []), *states]))
            else:
                existing = merged.get('statuses') or ([merged['status']] if merged.get('status') else [])
                if existing:
                    merged.pop('status', None)
                    merged['statuses'] = list(dict.fromkeys([*existing, *merged.pop('additional_statuses', []), *states]))
        else:
            for key in ['status', 'statuses', 'additional_statuses', 'waiting_hours_gt']:
                merged.pop(key, None)
            merged['statuses'] = states
        # A status-only follow-up preserves the previous ordering by construction.
        return QueryOrdersInput(filters=merged, sort=previous['sort'], page=1, page_size=previous['page_size'])
    if spec.context_mode == 'refine':
        return spec
    merged = {key: value for key, value in previous['filters'].items() if value is not None}
    for key in spec.clear_filters:
        merged.pop(key, None)
    delta = spec.filters.model_dump(mode='json', exclude_none=True)
    for left, right in [('shipping_city', 'shipping_cities'), ('status', 'statuses')]:
        if left in delta:
            merged.pop(right, None)
        if right in delta:
            merged.pop(left, None)
    merged.update(delta)
    if ('status' in delta or 'statuses' in delta) and not set(delta.get('statuses') or [delta.get('status')]).intersection({'paid'}):
        merged.pop('waiting_hours_gt', None)
        merged.pop('additional_statuses', None)
    explicit_sort = bool(re.search(r'排序|升序|降序|从高到低|从低到高', instruction))
    return QueryOrdersInput(filters=Filters.model_validate(merged), sort=spec.sort if explicit_sort else previous['sort'],
                            page=spec.page, page_size=previous['page_size'])


def execute_query(db, session, spec, settings, instruction='', context=None):
    if spec.context_mode == 'refine' and not status_followup(instruction):
        return service.refine(db, session, spec.filters, session.context_version, settings,
                      sort=spec.sort if re.search(r'排序|升序|降序|从高到低|从低到高', instruction) else None)
    context = context or context_for(db, session, instruction)
    resolved = revise_spec(spec, context, instruction)
    request = QueryRequest(**resolved.model_dump(exclude={'context_mode', 'clear_filters'}), expected_context_version=session.context_version)
    return service.query_orders(db, session, request, settings)


def constrain_action(instruction, proposal):
    """Disambiguate explicit counting and additive wording before business execution."""
    if not isinstance(proposal, dict):
        return proposal
    result = dict(proposal)
    if distinct_city_question(instruction) and result.get('intent') != 'refuse':
        result.update(intent='count_distinct', statistic_scope=statistic_scope(instruction), contextual=False,
                      context_mode='new', patch=None, scope='all')
        # A global count without stated conditions must not inherit a model's copied filters.
        remainder = normalize(instruction)
        for filler in ['当前租户', '当前结果', '当前列表', '这些订单', '收货城市', '总共有', '一共有', '有多少', '总共', '一共', '共有', '统计', '一下', '是多少', '多少', '几个', '城市', '数量', '个数', '种数', '总数', '订单', '所有', '全部', '这些', '其中', '这批', '几个', '的', '有', '个', '种']:
            remainder = remainder.replace(filler, '')
        if not remainder:
            result['filters'] = {}
    elif knowledge_question(instruction) and result.get('intent') not in {'knowledge', 'refuse', 'clarify'}:
        result.update(intent='knowledge', contextual=False, context_mode='new', patch=None, scope='all')
    elif status_followup(instruction) and result.get('intent') in {'query', 'clarify', 'export', 'aggregate'}:
        result.update(intent='query', contextual=False, context_mode='revise')
    return result
