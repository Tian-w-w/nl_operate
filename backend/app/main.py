from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
import time

from fastapi import BackgroundTasks, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .coordinator import authenticated_session, expire_attempts, run_chat
from .agent_runtime import available, drive_run, get_run, list_runs, start_run
from .schemas import AgentRunResponse, AgentStartRequest
from .model_adapter import create_adapter
from .workspace_api import workspace_router
from .schemas import AggregateResponse, ChatResponse, ExportResponse, OrderRow, PlanSummary, PreviewResponse, QueryResponse, SessionResponse
from .config import ROOT, Settings, now
from .db import make_engine, session_factory
from .models import Export, ModelCall, Operation, Order, Plan, Session, uid
from .schemas import ApplyRequest, ChatRequest, ConfirmRequest, DemoLogin, ExportRequest, PageRequest, PreviewRequest, QueryRequest, RefineRequest, SelectionRequest
from .service import BusinessError, aggregate, apply_update, cancel_pending, check_context, confirm, digest, export_orders, get_result, owned, preview, query_orders, refine, result_page, row, set_preview_state

ACTORS = {"operator_a": ("a", "operator"), "viewer_a": ("a", "viewer"), "operator_b": ("b", "operator")}


def create_app(settings=None, engine=None, adapter=None, serve_frontend=True):
    logger = logging.getLogger('llm4web')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    settings = settings or Settings()
    owns_engine = engine is None
    engine = engine or make_engine(settings.database_url)
    factory = session_factory(engine)
    @asynccontextmanager
    async def lifespan(app):
        yield
        if owns_engine:
            engine.dispose()
    app = FastAPI(title="LLM4Web · synthetic order lab", version="0.4.0", lifespan=lifespan)
    app.state.engine, app.state.settings = engine, settings
    app.state.adapter = adapter or create_adapter(settings)
    app.state.request_counts = {'total':0,'errors':0}
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver'])
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
                       allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-LLM4Web-Client"])

    @app.middleware("http")
    async def local_browser_guard(request, call_next):
        started = time.perf_counter()
        request.state.request_id = uid()
        if request.method == "POST" and request.url.path.startswith("/api/"):
            if request.headers.get("x-llm4web-client") != "web":
                return JSONResponse({"code": "CLIENT_HEADER_REQUIRED", "message": "缺少客户端标识"}, 403)
            origin = request.headers.get("origin")
            if origin and origin not in {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000",f'{request.url.scheme}://{request.url.netloc}'}:
                return JSONResponse({"code": "INVALID_ORIGIN", "message": "来源不允许"}, 403)
        response = await call_next(request)
        app.state.request_counts['total'] += 1
        app.state.request_counts['errors'] += int(response.status_code >= 500)
        response.headers['X-Request-ID'] = request.state.request_id
        logging.getLogger('llm4web').info(json.dumps({'event':'http_request','request_id':request.state.request_id,
            'method':request.method,'path':request.url.path,'status':response.status_code,
            'duration_ms':int((time.perf_counter()-started)*1000)}))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(BusinessError)
    async def business_error(request, error):
        return JSONResponse({"code": error.code, "message": error.message}, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"code": "VALIDATION_ERROR", "message": "请求参数无效", "fields": [".".join(str(v) for v in e["loc"]) for e in error.errors()]}, 422)

    @app.exception_handler(Exception)
    async def unexpected_error(request,error):
        request_id = getattr(request.state,'request_id','unknown')
        logger.error(json.dumps({'event':'unhandled_error','request_id':request_id,'type':type(error).__name__}))
        return JSONResponse({'code':'INTERNAL_ERROR','message':'服务暂时异常，请根据执行记录确认实际状态。','request_id':request_id},500,
                            headers={'X-Request-ID':request_id,'Cache-Control':'no-store'})

    def transaction():
        with factory() as db:
            with db.begin():
                yield db

    def authenticated(request: Request, db=Depends(transaction, scope="function")):
        token = request.cookies.get("llm4web_session")
        session = authenticated_session(db,token)
        expire_attempts(db,session)
        return session

    def session_view(db, session):
        result = {"actor": session.actor_id, "tenant": session.tenant_id, "role": session.role,
                  "context_version": session.context_version, "selected_ids": session.selected_ids,
                  "reference_time": settings.reference_time, "adapter": app.state.adapter.name, "auth_mode": settings.auth_mode,
                  "agent_available": available(app.state.adapter, settings)}
        if session.result_set_id:
            try:
                rs, orders = get_result(db, session, session.result_set_id)
                by_id = {o.id: o for o in orders}
                from decimal import Decimal
                result["current_result"] = {"result_set_id": rs.id, "query": rs.query, "total": len(orders),
                                            "total_amount": format(sum((o.total_amount for o in orders), Decimal(0)), ".2f"),
                                            "rows": [row(by_id[i]) for i in session.page_ids], "context_version": session.context_version}
            except BusinessError as e:
                result["snapshot_error"] = {"code": e.code, "message": e.message}
        if session.pending_plan_id:
            plan = owned(db, Plan, session.pending_plan_id, session)
            if plan.state == "WAITING_CONFIRMATION":
                result["preview"] = {"preview_id": plan.id, "digest": digest(plan.payload), "count": len(plan.payload["ids"]), **plan.payload}
        return result

    app.include_router(workspace_router(transaction,authenticated,settings))

    @app.get("/api/health")
    def health():
        return {"status": "ok", "adapter":app.state.adapter.name, "model_calls_enabled":settings.model_enabled}

    @app.get('/api/ready')
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text('SELECT id FROM model_calls LIMIT 1'))
                connection.execute(text('SELECT id FROM orders LIMIT 1'))
                connection.execute(text('SELECT id FROM saved_views LIMIT 1'))
                connection.execute(text('SELECT id FROM agent_runs LIMIT 1'))
            return {'status':'ready'}
        except Exception:
            return JSONResponse({'status':'not_ready','message':'数据库连接或迁移未就绪'},503)

    @app.post("/api/auth/demo", response_model=SessionResponse, response_model_exclude_unset=True)
    def demo_login(body: DemoLogin, response: Response, db=Depends(transaction, scope="function")):
        if settings.auth_mode != "demo":
            raise BusinessError("DEMO_DISABLED", "演示登录未启用", 403)
        tenant, role = ACTORS[body.actor]
        session = Session(actor_id=body.actor, tenant_id=tenant, role=role, expires_at=now() + timedelta(hours=12))
        db.add(session)
        db.flush()
        response.set_cookie("llm4web_session", session.id, httponly=True, samesite="strict", max_age=43200)
        return session_view(db, session)

    @app.get("/api/session", response_model=SessionResponse, response_model_exclude_unset=True)
    def current(session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return session_view(db, session)

    @app.post("/api/orders/query", response_model=QueryResponse, response_model_exclude_unset=True)
    def query(body: QueryRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return query_orders(db, session, body, settings)

    @app.post("/api/orders/refine", response_model=QueryResponse, response_model_exclude_unset=True)
    def refine_orders(body: RefineRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return refine(db, session, body.filters, body.expected_context_version, settings)

    @app.get("/api/orders/{order_id}", response_model=OrderRow)
    def detail(order_id: int, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        order = db.scalar(select(Order).where(Order.id == order_id, Order.tenant_id == session.tenant_id))
        if not order:
            raise BusinessError("NOT_FOUND", "订单不存在或无权访问", 404)
        return row(order)

    @app.post("/api/session/selection")
    def selection(body: SelectionRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        check_context(session, body.expected_context_version)
        if not set(body.ids).issubset(session.page_ids):
            raise BusinessError("INVALID_SELECTION", "只允许选择当前页中的订单", 422)
        cancel_pending(db, session)
        session.selected_ids = list(dict.fromkeys(body.ids))
        session.context_version += 1
        return {"selected_ids": session.selected_ids, "context_version": session.context_version}

    @app.post("/api/previews", response_model=PreviewResponse, response_model_exclude_unset=True)
    def make_preview(body: PreviewRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return preview(db, session, body, settings)

    @app.post("/api/previews/{plan_id}/confirm")
    def confirmation(plan_id: str, body: ConfirmRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return confirm(db, session, plan_id, body.digest, settings)

    @app.post("/api/previews/{plan_id}/apply")
    def apply(plan_id: str, body: ApplyRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return apply_update(db, session, plan_id, body.idempotency_key, settings)

    @app.post("/api/previews/{plan_id}/cancel")
    def cancel(plan_id: str, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        plan = owned(db, Plan, plan_id, session)
        if plan.state == "WAITING_CONFIRMATION":
            set_preview_state(db, plan, "CANCELLED")
            if session.pending_plan_id == plan_id:
                session.pending_plan_id = None
        return {"state": plan.state}

    @app.get("/api/operations/{operation_id}")
    def operation(operation_id: str, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return owned(db, Operation, operation_id, session).result

    @app.get("/api/plans", response_model=list[PlanSummary])
    def plans(session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        items = db.scalars(select(Plan).where(Plan.session_id == session.id, Plan.intent != 'agent_decision').order_by(Plan.created_at.desc()).limit(30))
        return [{"id": p.id, "intent": p.intent, "state": p.state, "instruction": p.instruction, "result": p.result, "created_at": p.created_at.isoformat()} for p in items]

    @app.post("/api/exports", response_model=ExportResponse)
    def export(body: ExportRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return export_orders(db, session, body)

    @app.get("/api/exports/{export_id}/download")
    def download(export_id: str, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        item = owned(db, Export, export_id, session)
        return Response(item.content.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="orders-{item.id[:8]}.csv"'})

    @app.get("/api/results/{result_id}/aggregate", response_model=AggregateResponse)
    def aggregation(result_id: str, group_by: str = "shipping_city", session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return aggregate(db, session, result_id, group_by)

    @app.post("/api/results/{result_id}/page", response_model=QueryResponse, response_model_exclude_unset=True)
    def pagination(result_id: str, body: PageRequest, session=Depends(authenticated, scope="function"), db=Depends(transaction, scope="function")):
        return result_page(db, session, result_id, body.page, body.expected_context_version)

    @app.post("/api/assistant", response_model=ChatResponse, response_model_exclude_unset=True)
    def assistant(body: ChatRequest, request: Request):
        return run_chat(factory,request.cookies.get('llm4web_session'),body.message,body.expected_context_version,
                        settings,app.state.adapter,request.state.request_id)

    @app.post('/api/agent/runs', response_model=AgentRunResponse, status_code=202)
    def agent_start(body: AgentStartRequest, request: Request, background: BackgroundTasks):
        token = request.cookies.get('llm4web_session')
        adapter = app.state.adapter
        result = start_run(factory, token, body.goal, body.expected_context_version, settings, adapter)
        background.add_task(drive_run, factory, token, result['id'], settings, adapter, request.state.request_id)
        return result

    @app.get('/api/agent/runs', response_model=list[AgentRunResponse])
    def agent_list(request: Request):
        return list_runs(factory, request.cookies.get('llm4web_session'))

    @app.get('/api/agent/runs/{run_id}', response_model=AgentRunResponse)
    def agent_status(run_id: str, request: Request):
        return get_run(factory, request.cookies.get('llm4web_session'), run_id)

    @app.get('/api/model-usage')
    def model_usage(session=Depends(authenticated, scope='function'), db=Depends(transaction, scope='function')):
        from decimal import Decimal
        calls = list(db.scalars(select(ModelCall).where(ModelCall.session_id == session.id).order_by(ModelCall.created_at.desc())))
        paid = [c for c in calls if c.billable]
        committed = sum((c.actual_cost if c.actual_cost is not None else c.reserved_cost for c in paid),Decimal(0))
        return {'model_calls':len(paid),'parser_calls':len(calls),'budget_usd':str(settings.model_budget_usd),
                'committed_usd':str(committed),'unknown_cost_calls':sum(c.actual_cost is None for c in paid),
                'remaining_usd':str(max(Decimal(0),settings.model_budget_usd-committed)),
                'calls':[{'plan_id':c.plan_id,'request_id':c.request_id,'adapter':c.adapter,'state':c.state,'duration_ms':c.duration_ms,
                          'input_tokens':c.input_tokens,'output_tokens':c.output_tokens,
                          'actual_cost_usd':str(c.actual_cost) if c.actual_cost is not None else None,
                          'reserved_cost_usd':str(c.reserved_cost),'error_code':c.error_code} for c in calls[:30]]}

    dist = ROOT / "frontend" / "dist"
    if serve_frontend and (dist/'assets').is_dir() and (dist/'index.html').is_file():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(dist / "index.html")
    return app


app = create_app()
