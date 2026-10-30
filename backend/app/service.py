import csv
import hashlib
import io
import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, update

from .config import now
from .models import AuditLog, Confirmation, Customer, Export, Operation, Order, OrderItem, Plan, Product, ResultSet, uid
from .schemas import Filters, QueryRequest


class BusinessError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def row(order):
    return {"id": order.id, "shipping_city": order.shipping_city, "status": order.status,
            "total_amount": format(order.total_amount, ".2f"),
            "created_at": order.created_at.isoformat(), "paid_at": order.paid_at.isoformat() if order.paid_at else None,
            "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
            "priority": order.priority, "review_status": order.review_status, "version": order.version}


def check_context(session, version):
    if session.context_version != version:
        raise BusinessError("STALE_CONTEXT", "页面已变化，请刷新后再操作")


def filtered(statement, filters, reference):
    status_conditions = []
    if filters.status is not None:
        status_conditions.append(Order.status == filters.status)
    if filters.statuses is not None:
        status_conditions.append(Order.status.in_(filters.statuses))
    if filters.waiting_hours_gt is not None:
        status_conditions.extend([Order.status == 'paid', Order.shipped_at.is_(None),
                                  Order.paid_at < reference-timedelta(hours=filters.waiting_hours_gt)])
    if status_conditions:
        base_status = and_(*status_conditions)
        statement = statement.where(or_(base_status, Order.status.in_(filters.additional_statuses)) if filters.additional_statuses else base_status)
    for key, value in filters.model_dump(exclude_none=True).items():
        if key in {'status', 'statuses', 'waiting_hours_gt', 'additional_statuses'}:
            continue
        if key == 'order_id':
            statement = statement.where(Order.id == value)
        elif key == 'shipping_cities':
            statement = statement.where(Order.shipping_city.in_(value))
        elif key == 'customer_name':
            statement = statement.where(select(Customer.id).where(Customer.id == Order.customer_id,Customer.tenant_id == Order.tenant_id,
                                       Customer.name.contains(value,autoescape=True)).exists())
        elif key in {'product_keyword','product_category'}:
            if key == 'product_category' and filters.product_keyword:
                continue
            conditions = []
            if filters.product_keyword:
                conditions.append(Product.name.contains(filters.product_keyword,autoescape=True))
            if filters.product_category:
                conditions.append(Product.category == filters.product_category)
            statement = statement.where(select(OrderItem.id).join(Product,Product.id == OrderItem.product_id)
                .where(OrderItem.order_id == Order.id,OrderItem.tenant_id == Order.tenant_id,Product.tenant_id == Order.tenant_id,*conditions).exists())
        elif key in {"shipping_city", "priority", "review_status"}:
            statement = statement.where(getattr(Order, key) == value)
        elif key == "amount_gte":
            statement = statement.where(Order.total_amount >= value)
        elif key == "amount_lte":
            statement = statement.where(Order.total_amount <= value)
        elif key.endswith("_gte"):
            statement = statement.where(getattr(Order, key[:-4]) >= value)
        elif key.endswith("_lt"):
            statement = statement.where(getattr(Order, key[:-3]) < value)
    return statement


def owned(db, model, object_id, session):
    obj = db.scalar(select(model).where(model.id == object_id, model.session_id == session.id))
    if not obj:
        raise BusinessError("NOT_FOUND", "对象不存在或无权访问", 404)
    return obj


def get_result(db, session, result_id, fresh=True):
    result = owned(db, ResultSet, result_id, session)
    if result.expires_at <= now():
        raise BusinessError("RESULT_EXPIRED", "结果集已过期，请重新查询")
    orders = list(db.scalars(select(Order).where(Order.tenant_id == session.tenant_id, Order.id.in_(result.ids))))
    by_id = {o.id: o for o in orders}
    if len(by_id) != len(result.ids):
        raise BusinessError("RESULT_CHANGED", "结果集成员发生变化，请重新查询")
    ordered = [by_id[i] for i in result.ids]
    if fresh and any(o.version != result.versions[str(o.id)] for o in ordered):
        raise BusinessError("VERSION_CONFLICT", "订单已被修改，请重新查询并生成预览")
    return result, ordered


def query_orders(db, session, request, settings, source_ids=None):
    check_context(session, request.expected_context_version)
    statement = filtered(select(Order).where(Order.tenant_id == session.tenant_id), request.filters, settings.reference)
    if source_ids is not None:
        statement = statement.where(Order.id.in_(source_ids))
    sorts = list(request.sort)
    for sort in sorts:
        column = getattr(Order, sort.field)
        statement = statement.order_by(column.desc().nulls_last() if sort.direction == "desc" else column.asc().nulls_last())
    if not any(s.field == "id" for s in sorts):
        statement = statement.order_by(Order.id.asc())
    orders = list(db.scalars(statement))
    if request.page > max(1, (len(orders) + request.page_size - 1) // request.page_size):
        raise BusinessError("INVALID_PAGE", "页码超出结果范围", 422)
    result = ResultSet(session_id=session.id, tenant_id=session.tenant_id,
                       query=request.model_dump(mode="json", exclude={"expected_context_version"}),
                       ids=[o.id for o in orders], versions={str(o.id): o.version for o in orders},
                       expires_at=now() + timedelta(seconds=settings.result_ttl))
    db.add(result)
    db.flush()
    start = (request.page - 1) * request.page_size
    page_rows = orders[start:start + request.page_size]
    # New query supersedes any earlier preview, even when member IDs happen to match.
    cancel_pending(db, session)
    session.context_version += 1
    session.result_set_id = result.id
    session.page_ids = [o.id for o in page_rows]
    session.selected_ids = []
    return {"result_set_id": result.id, "rows": [row(o) for o in page_rows], "total": len(orders),
            "total_amount": format(sum((o.total_amount for o in orders), Decimal(0)), ".2f"),
            "query": result.query, "context_version": session.context_version,
            "reference_time": settings.reference_time, "expires_at": result.expires_at.isoformat()}


def refine(db, session, filters, version, settings, sort=None):
    check_context(session, version)
    if not session.result_set_id:
        raise BusinessError("MISSING_RESULT", "请先查询订单", 422)
    result, _ = get_result(db, session, session.result_set_id)
    previous = result.query
    # Membership intersection prevents overwriting an earlier filter from expanding scope.
    merged = dict(previous["filters"])
    for key, value in filters.model_dump(mode="json", exclude_none=True).items():
        old = merged.get(key)
        if old is not None and old != value:
            if key in {'shipping_cities','statuses'}:
                value = [entry for entry in old if entry in value]
                if not value:
                    raise BusinessError('FILTER_CONFLICT','追加条件与原条件没有交集，请重新查询',422)
            elif key in {"amount_gte", "amount_lte"}:
                value = str((max if key.endswith("gte") else min)(Decimal(old), Decimal(value)))
            elif key == "waiting_hours_gt":
                value = max(old, value)
            elif key.endswith("_gte") or key.endswith("_lt"):
                from datetime import datetime
                value = (max if key.endswith("gte") else min)(datetime.fromisoformat(old), datetime.fromisoformat(value)).isoformat()
            else:
                raise BusinessError("FILTER_CONFLICT", "追加条件与原条件冲突，请明确发起新的查询", 422)
        merged[key] = value
    request = QueryRequest(filters=Filters.model_validate(merged), sort=sort if sort is not None else previous["sort"], page=1,
                           page_size=previous["page_size"], expected_context_version=version)
    return query_orders(db, session, request, settings, source_ids=result.ids)


def result_page(db, session, result_id, page, version):
    check_context(session, version)
    if result_id != session.result_set_id:
        raise BusinessError("STALE_CONTEXT", "当前结果集已变化")
    result, orders = get_result(db, session, result_id)
    page_size = result.query["page_size"]
    if page > max(1, (len(orders) + page_size - 1) // page_size):
        raise BusinessError("INVALID_PAGE", "页码超出结果范围", 422)
    cancel_pending(db, session)
    start = (page - 1) * page_size
    page_rows = orders[start:start + page_size]
    result.query = {**result.query, "page": page}
    session.page_ids, session.selected_ids = [o.id for o in page_rows], []
    session.context_version += 1
    return {"result_set_id": result.id, "rows": [row(o) for o in page_rows], "total": len(orders),
            "total_amount": format(sum((o.total_amount for o in orders), Decimal(0)), ".2f"),
            "query": result.query, "context_version": session.context_version}


def cancel_pending(db, session):
    if session.pending_plan_id:
        plan = db.get(Plan, session.pending_plan_id)
        if plan and plan.state == "WAITING_CONFIRMATION":
            set_preview_state(db, plan, "CANCELLED")
        session.pending_plan_id = None


def set_preview_state(db, plan, state, result=None):
    plan.state = state
    if result is not None:
        plan.result = result
    parent_id = plan.payload.get("parent_plan_id")
    if parent_id:
        parent = db.get(Plan, parent_id)
        if parent and parent.state == "WAITING_CONFIRMATION":
            parent.state = state
            parent.payload = {**parent.payload, "trace": parent.payload.get("trace", []) + [state]}
            if result is not None:
                parent.result = {**(parent.result or {}), "operation": result,
                                 "message": f"已更新{len(result['affected_ids'])}条订单，操作已提交。"}


def check_update_permission(session, orders):
    if session.role != "operator":
        raise BusinessError("FORBIDDEN", "当前角色只有读取权限", 403)
    if not orders:
        raise BusinessError("EMPTY_TARGET", "没有可修改的订单", 422)
    if len(orders) > 500:
        raise BusinessError("TOO_MANY_TARGETS", "批量修改上限为500条，请缩小范围", 422)
    if any(o.status != "paid" or o.shipped_at is not None for o in orders):
        raise BusinessError("INVALID_ORDER_STATE", "仅允许修改已付款且未发货订单的优先级和复核状态", 422)


def preview(db, session, request, settings, instruction="手工修改", parent_plan_id=None):
    check_context(session, request.expected_context_version)
    if request.result_set_id != session.result_set_id:
        raise BusinessError("STALE_CONTEXT", "只能修改当前明确结果集")
    result, orders = get_result(db, session, request.result_set_id)
    if request.scope == "selected":
        if not session.selected_ids:
            raise BusinessError("MISSING_SELECTION", "请先勾选订单", 422)
        orders = [o for o in orders if o.id in session.selected_ids]
    elif request.scope == "second":
        if len(session.page_ids) < 2:
            raise BusinessError("MISSING_ROW", "当前页没有第二条订单", 422)
        orders = [o for o in orders if o.id == session.page_ids[1]]
    check_update_permission(session, orders)
    patch = request.patch.model_dump(exclude_none=True)
    payload = {"result_set_id": result.id, "ids": [o.id for o in orders],
               "versions": {str(o.id): o.version for o in orders}, "patch": patch,
               "actor_id": session.actor_id, "context_version": session.context_version,
               "expires_at": (now() + timedelta(seconds=settings.confirm_ttl)).isoformat(),
               "changes": [{"id": o.id, "before": {k: getattr(o, k) for k in patch}, "after": patch} for o in orders]}
    if parent_plan_id:
        payload["parent_plan_id"] = parent_plan_id
    cancel_pending(db, session)
    plan = Plan(session_id=session.id, state="WAITING_CONFIRMATION", intent="apply_order_update", instruction=instruction, payload=payload)
    db.add(plan)
    db.flush()
    session.pending_plan_id = plan.id
    return {"preview_id": plan.id, "digest": digest(payload), "count": len(orders), **payload}


def check_plan(db, session, plan, settings):
    from datetime import datetime
    if plan.state != "WAITING_CONFIRMATION" or session.pending_plan_id != plan.id:
        raise BusinessError("PLAN_INACTIVE", "该预览已取消、已执行或被新查询替代")
    if datetime.fromisoformat(plan.payload["expires_at"]) <= now():
        raise BusinessError("CONFIRMATION_EXPIRED", "预览已过期，请重新生成")
    check_context(session, plan.payload["context_version"])
    _, all_orders = get_result(db, session, plan.payload["result_set_id"])
    by_id = {o.id: o for o in all_orders}
    orders = [by_id[i] for i in plan.payload["ids"]]
    check_update_permission(session, orders)
    if any(o.version != plan.payload["versions"][str(o.id)] for o in orders):
        raise BusinessError("VERSION_CONFLICT", "订单版本变化，请重新预览")
    return orders


def confirm(db, session, plan_id, supplied_digest, settings):
    plan = owned(db, Plan, plan_id, session)
    check_plan(db, session, plan, settings)
    if supplied_digest != digest(plan.payload):
        raise BusinessError("DIGEST_MISMATCH", "确认摘要与预览不一致")
    confirmation = db.scalar(select(Confirmation).where(Confirmation.plan_id == plan.id))
    if not confirmation:
        from datetime import datetime
        confirmation = Confirmation(plan_id=plan.id, session_id=session.id, digest=supplied_digest,
                                    expires_at=datetime.fromisoformat(plan.payload["expires_at"]))
        db.add(confirmation)
    return {"preview_id": plan.id, "confirmed": True}


def apply_update(db, session, plan_id, key, settings):
    plan = owned(db, Plan, plan_id, session)
    content_digest = digest(plan.payload)
    prior = db.scalar(select(Operation).where(Operation.session_id == session.id, Operation.idempotency_key == key))
    if prior:
        if prior.plan_id != plan.id or prior.digest != content_digest:
            raise BusinessError("IDEMPOTENCY_CONFLICT", "幂等键已绑定其他操作")
        return prior.result
    prior_plan = db.scalar(select(Operation).where(Operation.plan_id == plan.id))
    if prior_plan:
        raise BusinessError("ALREADY_APPLIED", "该预览已执行，请查询原操作结果")
    orders = check_plan(db, session, plan, settings)
    confirmation = db.scalar(select(Confirmation).where(Confirmation.plan_id == plan.id, Confirmation.session_id == session.id))
    if not confirmation or confirmation.digest != content_digest or confirmation.expires_at <= now():
        raise BusinessError("CONFIRMATION_REQUIRED", "必须通过页面确认当前预览", 403)
    operation_id = uid()
    response = {"operation_id": operation_id, "state": "SUCCEEDED", "affected_ids": plan.payload["ids"]}
    db.add(Operation(id=operation_id, session_id=session.id, plan_id=plan.id, idempotency_key=key,
                     digest=content_digest, state="SUCCEEDED", result=response))
    db.flush()
    for order in orders:
        before = {k: getattr(order, k) for k in plan.payload["patch"]}
        count = db.execute(update(Order).where(Order.id == order.id, Order.tenant_id == session.tenant_id,
                                                Order.version == plan.payload["versions"][str(order.id)])
                           .values(**plan.payload["patch"], version=Order.version + 1),
                           execution_options={"synchronize_session": False}).rowcount
        if count != 1:
            raise BusinessError("VERSION_CONFLICT", "订单已变化，整批操作已撤销")
        db.add(AuditLog(operation_id=operation_id, actor_id=session.actor_id, tenant_id=session.tenant_id,
                        order_id=order.id, before=before, after=plan.payload["patch"]))
    set_preview_state(db, plan, "SUCCEEDED", response)
    session.pending_plan_id = None
    # Keep the same explicit membership but refresh versions after our committed patch.
    result = db.get(ResultSet, plan.payload["result_set_id"])
    result.versions = {k: v + (1 if int(k) in plan.payload["ids"] else 0) for k, v in result.versions.items()}
    session.context_version += 1
    return response


def csv_safe(value):
    text = str(value if value is not None else "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def export_orders(db, session, request):
    request_digest = digest({"result_set_id": request.result_set_id, "columns": request.columns})
    prior = db.scalar(select(Export).where(Export.session_id == session.id, Export.idempotency_key == request.idempotency_key))
    if prior:
        if prior.digest != request_digest:
            raise BusinessError("IDEMPOTENCY_CONFLICT", "导出幂等键已绑定不同参数")
        return export_info(prior)
    result, orders = get_result(db, session, request.result_set_id)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(request.columns)
    for order in orders:
        values = row(order)
        writer.writerow([csv_safe(values[c]) for c in request.columns])
    content = output.getvalue()
    item = Export(session_id=session.id, result_set_id=result.id, idempotency_key=request.idempotency_key,
                  digest=request_digest, columns=request.columns, row_count=len(orders), state="SUCCEEDED", content=content,
                  checksum=hashlib.sha256(content.encode("utf-8-sig")).hexdigest())
    db.add(item)
    db.flush()
    return export_info(item)


def export_info(item):
    return {"export_id": item.id, "state": item.state, "row_count": item.row_count, "columns": item.columns,
            "checksum": item.checksum, "download_url": f"/api/exports/{item.id}/download"}


def aggregate(db, session, result_id, group_by):
    if group_by not in {"shipping_city", "status"}:
        raise BusinessError("INVALID_GROUP", "仅支持按城市或订单状态统计", 422)
    _, orders = get_result(db, session, result_id)
    groups = {}
    for order in orders:
        key = getattr(order, group_by)
        count, amount = groups.get(key, (0, Decimal(0)))
        groups[key] = (count + 1, amount + order.total_amount)
    return {"group_by": group_by, "result_set_id": result_id,
            "groups": [{"group": k, "count": v[0], "total_amount": format(v[1], ".2f")} for k, v in sorted(groups.items())],
            "total": len(orders), "basis": "按订单去重；金额单位CNY；范围为明确结果集"}


def count_order_values(db, session, request, settings):
    statement = filtered(select(Order.shipping_city, func.count(Order.id)).where(Order.tenant_id == session.tenant_id), request.filters, settings.reference)
    result_id = None
    if request.scope == 'current':
        if not session.result_set_id:
            raise BusinessError('MISSING_RESULT', '请先查询订单，才能统计当前结果中的城市。', 422)
        result, _ = get_result(db, session, session.result_set_id)
        result_id = result.id
        statement = statement.where(Order.id.in_(result.ids))
    pairs = list(db.execute(statement.group_by(Order.shipping_city).order_by(Order.shipping_city)))
    return {'statistic_id': uid(), 'field': 'shipping_city', 'scope': request.scope, 'result_set_id': result_id,
            'distinct_count': len(pairs), 'order_count': sum(count for _, count in pairs),
            'values': [{'value': city, 'order_count': count} for city, count in pairs],
            'basis': '按收货城市去重计数；范围为'+('当前完整结果集' if result_id else '当前租户符合条件的全部订单')+'；不受分页影响，不修改列表。'}


def statistic_message(statistic):
    scope = '当前结果中' if statistic['scope'] == 'current' else '当前租户符合条件的订单中'
    return f"{scope}共有{statistic['distinct_count']}个不同的收货城市，涉及{statistic['order_count']}条订单。"
