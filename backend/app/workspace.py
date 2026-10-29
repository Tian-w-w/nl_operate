"""Read-only business insight, full details, audit and personal query views."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from .config import now
from .models import AuditLog, Customer, Order, OrderItem, Product, SavedView, Shipment
from .schemas import AuditQuery, QueryRequest
from .service import BusinessError, check_context, filtered, get_result, query_orders, row


def money(value):
    return format(Decimal(value or 0).quantize(Decimal('.01'),rounding=ROUND_HALF_UP),'.2f')


def audit_entry(item):
    return {'id':item.id,'order_id':item.order_id,'operation_id':item.operation_id,'actor_id':item.actor_id,
            'before':item.before,'after':item.after,'created_at':item.created_at.isoformat()}


def query_audit(db,session,request):
    statement = select(AuditLog).where(AuditLog.tenant_id == session.tenant_id)
    for field in ['order_id','actor_id']:
        if getattr(request,field) is not None:
            statement = statement.where(getattr(AuditLog,field) == getattr(request,field))
    if request.created_at_gte:
        statement = statement.where(AuditLog.created_at >= request.created_at_gte)
    if request.created_at_lt:
        statement = statement.where(AuditLog.created_at < request.created_at_lt)
    total = db.scalar(select(func.count()).select_from(statement.subquery()))
    if request.page > max(1,(total+request.page_size-1)//request.page_size):
        raise BusinessError('INVALID_PAGE','页码超出审计记录范围',422)
    rows = db.scalars(statement.order_by(AuditLog.created_at.desc(),AuditLog.id.asc())
                      .offset((request.page-1)*request.page_size).limit(request.page_size))
    return {'rows':[audit_entry(item) for item in rows],'total':total,'page':request.page,'page_size':request.page_size}


def full_detail(db,session,order_id,settings):
    order = db.scalar(select(Order).where(Order.id == order_id,Order.tenant_id == session.tenant_id))
    if not order:
        raise BusinessError('NOT_FOUND','订单不存在或无权访问',404)
    customer = db.scalar(select(Customer).where(Customer.id == order.customer_id,Customer.tenant_id == session.tenant_id))
    items = db.execute(select(OrderItem,Product).join(Product,Product.id == OrderItem.product_id)
        .where(OrderItem.order_id == order.id,OrderItem.tenant_id == session.tenant_id,Product.tenant_id == session.tenant_id)
        .order_by(OrderItem.id.asc())).all()
    shipment = db.scalar(select(Shipment).where(Shipment.order_id == order.id,Shipment.tenant_id == session.tenant_id))
    return {'order':row(order),'customer':{'id':customer.id,'name':customer.name} if customer else None,
            'items':[{'id':item.id,'product_id':product.id,'product_name':product.name,'category':product.category,
                      'quantity':item.quantity,'unit_price':money(item.unit_price),'line_total':money(item.unit_price*item.quantity)} for item,product in items],
            'shipment':{'carrier':shipment.carrier,'tracking_no':shipment.tracking_no,'shipped_at':shipment.shipped_at.isoformat()} if shipment else None,
            'note':order.note,'audit':query_audit(db,session,AuditQuery(order_id=order.id,page_size=50)),
            'reference_time':settings.reference_time}


def view_response(view):
    return {'id':view.id,'name':view.name,'query':view.query,'version':view.version,
            'created_at':view.created_at.isoformat(),'updated_at':view.updated_at.isoformat()}


def own_view(db,session,view_id):
    item = db.scalar(select(SavedView).where(SavedView.id == view_id,SavedView.tenant_id == session.tenant_id,
                                           SavedView.actor_id == session.actor_id).with_for_update())
    if not item:
        raise BusinessError('NOT_FOUND','常用查询不存在或无权访问',404)
    return item


def create_view(db,session,request):
    check_context(session,request.expected_context_version)
    if request.result_set_id != session.result_set_id:
        raise BusinessError('STALE_CONTEXT','请先应用筛选条件再保存当前查询')
    result,_ = get_result(db,session,request.result_set_id)
    item = SavedView(tenant_id=session.tenant_id,actor_id=session.actor_id,name=request.name,
                     query={**result.query,'page':1},version=1,updated_at=now())
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError:
        raise BusinessError('VIEW_NAME_EXISTS','已有同名常用查询，请使用其他名称') from None
    return view_response(item)


def rename_view(db,session,view_id,request):
    item = own_view(db,session,view_id)
    if item.version != request.expected_version:
        raise BusinessError('VIEW_VERSION_CONFLICT','常用查询已变化，请刷新列表')
    try:
        with db.begin_nested():
            item.name,item.updated_at = request.name,now()
            item.version += 1
            db.flush()
    except IntegrityError:
        raise BusinessError('VIEW_NAME_EXISTS','已有同名常用查询，请使用其他名称') from None
    return view_response(item)


def apply_view(db,session,view_id,version,settings):
    view = own_view(db,session,view_id)
    request = QueryRequest(**{**view.query,'page':1},expected_context_version=version)
    return query_orders(db,session,request,settings)


def analytics(db,session,request,settings):
    base = filtered(select(Order.id).where(Order.tenant_id == session.tenant_id),request.filters,settings.reference)
    if request.result_set_id:
        result,_ = get_result(db,session,request.result_set_id)
        base = base.where(Order.id.in_(result.ids))
    within = Order.id.in_(base)
    paid = Order.status.in_(['paid','shipped','completed'])
    pending = (Order.status == 'paid') & Order.shipped_at.is_(None)
    def count_where(condition):
        return func.coalesce(func.sum(case((condition,1),else_=0)),0)
    count,total,paid_amount,pending_amount,shipment_count,overdue,priority,review = db.execute(select(
        func.count(Order.id),func.coalesce(func.sum(Order.total_amount),0),
        func.coalesce(func.sum(case((paid,Order.total_amount),else_=Decimal(0))),0),
        func.coalesce(func.sum(case((Order.status == 'pending_payment',Order.total_amount),else_=Decimal(0))),0),
        count_where(pending),count_where(pending & (Order.paid_at < settings.reference-timedelta(hours=48))),
        count_where(Order.priority.is_(True)),count_where(Order.review_status == 'pending_review')).where(within)).one()
    def groups(field):
        column = getattr(Order,field)
        records = db.execute(select(column,func.count(Order.id),func.sum(Order.total_amount)).where(within)
                             .group_by(column).order_by(func.sum(Order.total_amount).desc(),column.asc()))
        return [{'group':name,'count':number,'total_amount':money(amount)} for name,number,amount in records]
    local = settings.reference.astimezone(ZoneInfo('Asia/Shanghai'))
    end = local.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
    start = end-timedelta(days=request.trend_days)
    trend = {(start+timedelta(days=i)).date().isoformat():{'date':(start+timedelta(days=i)).date().isoformat(),'count':0,'amount':Decimal(0)}
             for i in range(request.trend_days)}
    for created,amount in db.execute(select(Order.created_at,Order.total_amount).where(within,Order.created_at >= start,Order.created_at < end)):
        bucket = trend[created.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()]
        bucket['count'] += 1
        bucket['amount'] += amount
    age_limits = [settings.reference-timedelta(hours=hours) for hours in [24,48,72]]
    age_counts = db.execute(select(
        count_where(pending & (Order.paid_at >= age_limits[0])),
        count_where(pending & (Order.paid_at < age_limits[0]) & (Order.paid_at >= age_limits[1])),
        count_where(pending & (Order.paid_at < age_limits[1]) & (Order.paid_at >= age_limits[2])),
        count_where(pending & (Order.paid_at < age_limits[2]))).where(within)).one()
    return {'scope':'result_set' if request.result_set_id else 'tenant','result_set_id':request.result_set_id,
            'metrics':{'order_count':count,'total_amount':money(total),'average_amount':money(Decimal(total)/count) if count else '0.00',
                       'paid_amount':money(paid_amount),'pending_amount':money(pending_amount),'pending_shipment_count':shipment_count,
                       'overdue_48h_count':overdue,'priority_count':priority,'review_count':review},
            'cities':groups('shipping_city'),'statuses':groups('status'),
            'trend':[{'date':point['date'],'count':point['count'],'total_amount':money(point['amount'])} for point in trend.values()],
            'waiting_buckets':[{'label':label,'count':number} for label,number in zip(['≤24小时','>24–48小时','>48–72小时','>72小时'],age_counts)],
            'reference_time':settings.reference_time,
            'basis':f'订单去重；金额单位CNY；分组和指标覆盖完整筛选范围，趋势为业务时区最近{request.trend_days}个自然日；未发货时长从付款时刻严格计算。'}
