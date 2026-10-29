"""Offline, bounded rules adapter. It is not an LLM and supplies no model results."""
import re
from datetime import timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .models import PlanStep
from .schemas import AnalyticsRequest, AuditQuery, ExportRequest, Filters, Patch, PreviewRequest, QueryRequest, Sort
from .service import BusinessError, aggregate, cancel_pending, export_orders, preview, query_orders, refine, result_page


class ModelAdapter(Protocol):
    name: str

    def parse(self, instruction: str, reference, context: dict | None = None) -> dict: ...


class RulesAdapter:
    name = "rules-v1-offline"

    def parse(self, instruction, reference, context=None):
        text = re.sub(r"[\s，,。！!？?、]", "", instruction)
        if any(word in text for word in ["退款", "删除", "支付", "发货给", "取消订单", "执行代码", "SQL", "其他租户", "租户b", "全部租户"]):
            return {"intent": "refuse", "message": "该操作不在当前支持范围或权限内；可查询、统计、标记优先/待复核及导出。"}
        if text in {"取消", "取消修改", "不用了"}:
            return {"intent": "cancel"}
        if text == "第二页":
            return {"intent": "page", "page": 2}
        if text in {"确认", "确认修改", "是的", "confirmed=true"}:
            return {"intent": "clarify", "message": "请核对预览后点击页面上的确认按钮，聊天文字不会执行修改。"}
        detail = re.fullmatch(r"(?:查看|查询)订单(\d+)(?:的)?详情", text)
        if detail:
            return {"intent": "detail", "order_id": int(detail.group(1))}
        if text in {'查看操作审计','查看修改记录','操作审计'}:
            return {'intent':'audit'}
        audit_detail = re.fullmatch(r'查看订单(\d+)的修改记录',text)
        if audit_detail:
            return {'intent':'audit','order_id':int(audit_detail.group(1))}
        filters = {}
        for pattern,field in [(r'客户名称包含(.+?)的','customer_name'),(r'商品名称包含(.+?)的','product_keyword'),
                              (r'商品分类为(办公|数码|生活)的?','product_category'),(r'订单编号为(\d+)','order_id')]:
            match = re.search(pattern,text)
            if match:
                filters[field] = int(match.group(1)) if field == 'order_id' else match.group(1)
                text = text.replace(match.group(0),'',1)
        cities = [c for c in ["上海", "北京", "杭州", "深圳", "成都", "广州"] if c in text]
        if len(cities) > 1:
            return {"intent": "clarify", "message": "当前演示规则支持一个城市，请明确查询哪个城市。"}
        if cities:
            filters["shipping_city"] = cities[0]
        rest = text
        statuses = [("已付款但未发货", "paid"), ("已付款未发货", "paid"), ("已付款且未发货", "paid"),
                    ("已付款", "paid"), ("待发货", "paid"), ("待付款", "pending_payment"),
                    ("已发货", "shipped"), ("已完成", "completed"), ("已取消", "cancelled")]
        matched_status = []
        for label, value in statuses:
            if label in rest:
                matched_status.append(value)
                rest = rest.replace(label, "")
        if len(set(matched_status)) > 1:
            return {"intent": "clarify", "message": "检测到多个订单状态，请明确一个状态。"}
        if matched_status:
            filters["status"] = matched_status[0]
        if "上周" in text:
            local = reference.astimezone(ZoneInfo("Asia/Shanghai"))
            end = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            field = "paid_at" if "上周付款" in text else "created_at"
            filters[field + "_gte"] = (end - timedelta(days=7)).isoformat()
            filters[field + "_lt"] = end.isoformat()
            rest = rest.replace("上周付款", "").replace("上周", "")
        age = re.search(r"(?:等待(?:发货)?)?超过(\d+)小时", rest)
        if age:
            filters["waiting_hours_gt"] = int(age.group(1))
            rest = rest.replace(age.group(0), "")
        for label, field in [("金额不低于", "amount_gte"), ("金额不超过", "amount_lte")]:
            match = re.search(label + r"(\d+(?:\.\d{1,2})?)(?:元)?", rest)
            if match:
                filters[field] = match.group(1)
                rest = rest.replace(match.group(0), "")
        sorts, sort_explicit = [Sort()], False
        for label, direction in [("按金额从高到低", "desc"), ("按金额降序", "desc"), ("按金额从低到高", "asc"), ("按金额升序", "asc")]:
            if label in rest:
                sorts = [Sort(field="total_amount", direction=direction), Sort()]
                sort_explicit = True
                rest = rest.replace(label, "")
                break
        intent, patch, scope = "query", None, "all"
        if "标为优先处理" in rest or "标记优先处理" in rest:
            intent, patch = "update", {"priority": True}
            rest = rest.replace("标为优先处理", "").replace("标记优先处理", "")
        if "标为待复核" in rest or "标记待复核" in rest:
            if patch:
                return {"intent": "clarify", "message": "请一次指定一种修改。"}
            intent, patch = "update", {"review_status": "pending_review"}
            rest = rest.replace("标为待复核", "").replace("标记待复核", "")
        if "导出" in rest:
            if patch:
                return {"intent": "clarify", "message": "请先完成修改确认，再单独导出。"}
            intent = "export"
            rest = rest.replace("导出", "").replace("清单", "")
        group_by = None
        if '统计看板' in rest:
            intent = 'analytics'
            rest = rest.replace('统计看板','')
        for label, field in [("按城市统计", "shipping_city"), ("按状态统计", "status")]:
            if label in rest:
                intent, group_by = "aggregate", field
                rest = rest.replace(label, "")
        if intent != 'update':
            for label,field,value in [('优先处理','priority',True),('待复核','review_status','pending_review')]:
                if label in rest:
                    filters[field] = value
                    rest = rest.replace(label,'')
        if "选中的" in rest:
            scope = "selected"
            rest = rest.replace("选中的", "")
        if "第二条" in rest:
            scope = "second"
            rest = rest.replace("第二条", "")
        contextual = any(w in text for w in ["其中", "这些", "选中的", "第二条"])
        if contextual and scope != "all" and filters:
            return {"intent": "clarify", "message": "请先完成条件筛选，再勾选或指定当前页第二条进行修改。"}
        if contextual and scope != "all" and intent != "update":
            return {"intent": "clarify", "message": "当前规则仅支持对勾选项或第二条进行标记；导出和统计使用完整结果集。"}
        page = 1
        if "第二页" in rest:
            page = 2
            rest = rest.replace("第二页", "")
        for token in cities + ["找出", "查询", "查看", "展示", "显示", "订单", "其中", "这些", "所有", "全部", "的", "将", "把", "请", "并", "且"]:
            rest = rest.replace(token, "")
        if rest or not text or (intent == "query" and not filters and not contextual and not any(w in text for w in ["订单", "第二页"])):
            return {"intent": "clarify", "message": "未能完整理解条件，请明确城市、状态或操作。例如：找出上海已付款未发货的订单。"}
        return {"intent": intent, "filters": Filters.model_validate(filters).model_dump(mode="json", exclude_none=True),
                "sort": [s.model_dump() for s in sorts], "patch": patch, "scope": scope,
                "sort_explicit": sort_explicit, "contextual": contextual, "group_by": group_by, "page": page}


TRANSITIONS = {
    "RECEIVED": {"PLANNED", "FAILED"},
    "PLANNED": {"VALIDATED", "WAITING_CLARIFICATION", "CANCELLED", "FAILED"},
    "VALIDATED": {"RUNNING", "FAILED"},
    "RUNNING": {"SUCCEEDED", "WAITING_CONFIRMATION", "FAILED"},
}


def transition(plan, state):
    if state not in TRANSITIONS.get(plan.state, set()):
        raise RuntimeError(f"Invalid transition: {plan.state} -> {state}")
    plan.state = state
    plan.payload = {**plan.payload, "trace": plan.payload.get("trace", []) + [state]}


def execute_plan(db, session, plan, parsed, settings):
    from .service import row
    from .models import Order
    from sqlalchemy import select
    step = None
    try:
        plan.intent = parsed["intent"]
        plan.payload = {**plan.payload, "parsed": parsed}
        transition(plan, "PLANNED")
        if parsed["intent"] in {"clarify", "refuse"}:
            transition(plan, "WAITING_CLARIFICATION" if parsed["intent"] == "clarify" else "CANCELLED")
            result = {"message": parsed["message"]}
        elif parsed["intent"] == "cancel":
            cancel_pending(db, session)
            transition(plan, "CANCELLED")
            result = {"message": "修改已取消，没有更新订单。"}
        else:
            transition(plan, "VALIDATED")
            transition(plan, "RUNNING")
            step = PlanStep(plan_id=plan.id, state="RUNNING", tool=parsed["intent"], args=parsed)
            db.add(step)
            db.flush()
            # Roll back intermediate refinements too when a later preview fails.
            with db.begin_nested():
                result = {}
                intent = parsed["intent"]
                filters = Filters.model_validate(parsed.get("filters", {}))
                if intent == "detail":
                    order = db.scalar(select(Order).where(Order.id == parsed["order_id"], Order.tenant_id == session.tenant_id))
                    if not order:
                        raise BusinessError("NOT_FOUND", "订单不存在或无权访问", 404)
                    result["detail"] = row(order)
                elif intent == "page":
                    if not session.result_set_id:
                        raise BusinessError("MISSING_RESULT", "请先查询订单再翻页", 422)
                    result["ui_patch"] = result_page(db, session, session.result_set_id, parsed["page"], session.context_version)
                elif intent == 'audit':
                    from .workspace import query_audit
                    result['audit'] = query_audit(db,session,AuditQuery(order_id=parsed.get('order_id')))
                    result['audit_order_id'] = parsed.get('order_id')
                else:
                    has_filters = bool(parsed.get("filters"))
                    if parsed["contextual"] and not session.result_set_id:
                        raise BusinessError("MISSING_RESULT", "请先查询订单，才能确定“这些订单”的范围", 422)
                    if intent == "query" or has_filters or (intent in {"aggregate",'analytics'} and not parsed["contextual"]):
                        if parsed["contextual"]:
                            result["ui_patch"] = refine(db, session, filters, session.context_version, settings, sort=parsed["sort"] if parsed["sort_explicit"] else None)
                        else:
                            req = QueryRequest(filters=filters, sort=parsed["sort"], page=parsed["page"], expected_context_version=session.context_version)
                            result["ui_patch"] = query_orders(db, session, req, settings)
                    if not session.result_set_id:
                        raise BusinessError("MISSING_RESULT", "请先查询或明确需要操作的订单范围", 422)
                    if intent == "update":
                        req = PreviewRequest(result_set_id=session.result_set_id, patch=Patch.model_validate(parsed["patch"]),
                                             scope=parsed["scope"], expected_context_version=session.context_version)
                        result["preview"] = preview(db, session, req, settings, plan.instruction, parent_plan_id=plan.id)
                    elif intent == "export":
                        result["export"] = export_orders(db, session, ExportRequest(result_set_id=session.result_set_id, idempotency_key=plan.id))
                    elif intent == "aggregate":
                        result["aggregate"] = aggregate(db, session, session.result_set_id, parsed["group_by"])
                    elif intent == 'analytics':
                        from .workspace import analytics
                        result['analytics'] = analytics(db,session,AnalyticsRequest(result_set_id=session.result_set_id),settings)
                result["message"] = {"analytics":"已按完整结果集打开统计看板。","audit":"已读取当前租户的真实修改记录。","page": f"已展示当前结果集的第{parsed['page']}页。", "query": "已按实际查询结果更新列表。", "detail": "已读取订单详情。", "update": "请检查具体订单和字段差异，再点击确认。", "export": "清单已生成，可以下载。", "aggregate": "已按当前结果集统计。"}[intent]
            transition(plan, "WAITING_CONFIRMATION" if parsed["intent"] == "update" else "SUCCEEDED")
            step.state, step.result = "SUCCEEDED", result
    except (BusinessError, ValidationError) as error:
        transition(plan, "FAILED")
        result = {"message": error.message if isinstance(error, BusinessError) else "参数超出支持范围，请调整查询条件。",
                  "error": error.code if isinstance(error, BusinessError) else "VALIDATION_ERROR"}
        if step:
            step.state, step.result = "FAILED", result
    plan.result = result
    return {"plan_id": plan.id, "state": plan.state, "adapter": plan.payload["adapter"],
            "context_version": session.context_version, **result}
