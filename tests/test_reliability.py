import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from backend.app.config import now
from backend.app.main import create_app
from backend.app.models import AuditLog, Export, Operation, Order, Plan, ResultSet
from backend.app.service import row
from conftest import confirm_apply, make_preview, query


def snapshot(factory):
    with factory() as db:
        return {o.id: row(o) for o in db.scalars(select(Order))}


def test_time_boundaries_stable_sort_and_precision(lab):
    client, _, _, _ = lab
    result = query(client, {"shipping_city": "上海", "status": "paid", "created_at_gte": "2026-10-19T00:00:00+08:00", "created_at_lt": "2026-10-26T00:00:00+08:00"}, sort=[{"field": "total_amount", "direction": "desc"}])
    assert [r['id'] for r in result['rows']] == [12, 3, 1, 2]
    assert result['total_amount'] == '2500.00'
    result = query(client, {"shipping_city": "上海", "waiting_hours_gt": 48})
    assert [r['id'] for r in result['rows']] == [1, 3, 10, 12]


def test_no_confirmation_no_write_and_digest_binding(lab):
    client, factory, _, _ = lab
    original = snapshot(factory)
    preview = make_preview(client, query(client, {"status": "paid"})).json()
    response = client.post(f"/api/previews/{preview['preview_id']}/apply", json={"idempotency_key": "missing-confirmation"})
    assert response.status_code == 403
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": "a"*64}).status_code == 409
    assert snapshot(factory) == original


def test_retry_after_lost_response_is_idempotent_and_survives_restart(lab):
    client, factory, settings, engine = lab
    result = query(client, {"status": "paid", "shipping_city": "上海"})
    preview = make_preview(client, result).json()
    first = confirm_apply(client, preview).json()  # Pretend HTTP response never reached the UI.
    with TestClient(create_app(settings, engine), headers={"X-LLM4Web-Client": "web"}) as restarted:
        restarted.cookies.update(client.cookies)
        repeat = restarted.post(f"/api/previews/{preview['preview_id']}/apply", json={"idempotency_key": "test-operation-key"})
        assert repeat.json() == first
        assert restarted.get(f"/api/operations/{first['operation_id']}").json() == first
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 1
        assert db.scalar(select(func.count()).select_from(AuditLog)) == 6
        assert [o.version for o in db.scalars(select(Order).where(Order.id.in_(first['affected_ids'])))] == [2]*6


def test_new_key_cannot_execute_same_preview_twice(lab):
    client, _, _, _ = lab
    preview = make_preview(client, query(client, {"status": "paid"})).json()
    assert confirm_apply(client, preview).status_code == 200
    assert client.post(f"/api/previews/{preview['preview_id']}/apply", json={"idempotency_key": "another-key"}).json()['code'] == 'ALREADY_APPLIED'


def test_same_key_different_request_rejected(lab):
    client, _, _, _ = lab
    preview = make_preview(client, query(client, {"status": "paid"})).json()
    assert confirm_apply(client, preview).status_code == 200
    other = make_preview(client, query(client, {"status": "paid"}), patch={"review_status": "pending_review"}).json()
    assert client.post(f"/api/previews/{other['preview_id']}/confirm", json={"digest": other['digest']}).status_code == 200
    assert client.post(f"/api/previews/{other['preview_id']}/apply", json={"idempotency_key": "test-operation-key"}).json()['code'] == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('phase', ['before_confirmation', 'after_confirmation'])
def test_version_conflict_rolls_back_every_target(lab, phase):
    client, factory, _, _ = lab
    preview = make_preview(client, query(client, {"status": "paid"})).json()
    if phase == 'after_confirmation':
        assert client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": preview['digest']}).status_code == 200
    with factory.begin() as db:
        order = db.get(Order, 1)
        order.priority, order.version = True, 2
    before = snapshot(factory)
    path = 'confirm' if phase == 'before_confirmation' else 'apply'
    body = {"digest": preview['digest']} if path == 'confirm' else {"idempotency_key": "conflict-key"}
    response = client.post(f"/api/previews/{preview['preview_id']}/{path}", json=body)
    assert response.status_code == 409
    assert snapshot(factory) == before
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 0


def test_result_expiry_and_confirmation_expiry(lab):
    client, factory, _, _ = lab
    result = query(client, {"status": "paid"})
    preview = make_preview(client, result).json()
    with factory.begin() as db:
        plan = db.get(Plan, preview['preview_id'])
        plan.payload = {**plan.payload, 'expires_at': (now()-timedelta(seconds=1)).isoformat()}
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": preview['digest']}).json()['code'] == 'CONFIRMATION_EXPIRED'
    with factory.begin() as db:
        db.get(ResultSet, result['result_set_id']).expires_at = now()-timedelta(seconds=1)
    assert make_preview(client, result).json()['code'] == 'RESULT_EXPIRED'


def test_cancel_and_new_query_invalidate_confirmation(lab):
    client, _, _, _ = lab
    preview = make_preview(client, query(client, {"status": "paid"})).json()
    assert client.post(f"/api/previews/{preview['preview_id']}/cancel").status_code == 200
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": preview['digest']}).json()['code'] == 'PLAN_INACTIVE'
    preview = make_preview(client, query(client, {"status": "paid"})).json()
    query(client, {"status": "paid"})
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": preview['digest']}).json()['code'] == 'PLAN_INACTIVE'


def test_readonly_role_and_illegal_status_leave_business_unchanged(lab):
    client, factory, _, _ = lab
    before = snapshot(factory)
    assert make_preview(client, query(client)).json()['code'] == 'INVALID_ORDER_STATE'
    client.post('/api/auth/demo', json={"actor": "viewer_a"})
    assert make_preview(client, query(client, {"status": "paid"})).status_code == 403
    assert snapshot(factory) == before


def test_cross_tenant_and_session_ownership(lab):
    client, _, settings, engine = lab
    result = query(client, {"status": "paid"})
    preview = make_preview(client, result).json()
    export = client.post('/api/exports', json={"result_set_id": result['result_set_id'], "idempotency_key": "export-tenant-check"}).json()
    assert client.get('/api/orders/13').status_code == 404
    with TestClient(create_app(settings, engine), headers={"X-LLM4Web-Client": "web"}) as other:
        other.post('/api/auth/demo', json={"actor": "operator_b"})
        assert [r['id'] for r in query(other)['rows']] == [13,14,15]
        assert other.get(export['download_url']).status_code == 404
        assert other.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": preview['digest']}).status_code == 404
        assert other.get(f"/api/results/{result['result_set_id']}/aggregate").status_code == 404
    assert client.post('/api/orders/query', json={"filters": {"tenant_id": "b"}, "expected_context_version": result['context_version']}).status_code == 422


def test_export_actual_content_bom_checksum_and_retry(lab):
    import hashlib
    client, factory, _, _ = lab
    result = query(client, {"status": "paid"}, page_size=2)
    request = {"result_set_id": result['result_set_id'], "columns": ["id", "total_amount"], "idempotency_key": "csv-retry-key"}
    first = client.post('/api/exports', json=request).json()
    assert client.post('/api/exports', json=request).json() == first
    content = client.get(first['download_url']).content
    assert content.startswith(b'\xef\xbb\xbf')
    assert hashlib.sha256(content).hexdigest() == first['checksum']
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    assert [int(r['id']) for r in rows] == [1,2,3,4,9,10,11,12]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Export)) == 1


def test_selection_and_ordinal_bind_to_visible_page(lab):
    client, factory, _, _ = lab
    result = query(client, {"status": "paid"}, page_size=2)
    page = client.post(f"/api/results/{result['result_set_id']}/page", json={"page": 2, "expected_context_version": result['context_version']}).json()
    assert [r['id'] for r in page['rows']] == [3,4]
    assert client.post('/api/session/selection', json={"ids": [1], "expected_context_version": page['context_version']}).status_code == 422
    preview = make_preview(client, result, scope='second').json()
    assert preview['ids'] == [4]
    assert confirm_apply(client, preview).status_code == 200
    assert snapshot(factory)[4]['priority'] is True
    assert snapshot(factory)[3]['priority'] is False


def test_stale_ui_rejected_and_aggregate_no_join_double_count(lab):
    client, _, _, _ = lab
    result = query(client)
    assert client.post('/api/orders/query', json={"expected_context_version": 0}).json()['code'] == 'STALE_CONTEXT'
    aggregate = client.get(f"/api/results/{result['result_set_id']}/aggregate").json()
    assert aggregate['groups'] == [
        {'group': '上海', 'count': 8, 'total_amount': '3350.00'},
        {'group': '北京', 'count': 1, 'total_amount': '120.00'},
        {'group': '杭州', 'count': 2, 'total_amount': '480.00'},
        {'group': '深圳', 'count': 1, 'total_amount': '700.00'}]


def test_chat_history_restore_untrusted_notes_and_unsupported_constraints(lab):
    client, factory, settings, engine = lab
    result = client.post('/api/assistant', json={"message": "找出上海已付款未发货的订单", "expected_context_version": 0}).json()
    assert result['state'] == 'SUCCEEDED'
    assert 12 in [r['id'] for r in result['ui_patch']['rows']]
    version = result['context_version']
    reply = client.post('/api/assistant', json={"message": "找出上海金额大于100元的订单", "expected_context_version": version}).json()
    assert reply['state'] == 'WAITING_CLARIFICATION'
    with TestClient(create_app(settings, engine), headers={"X-LLM4Web-Client": "web"}) as restarted:
        restarted.cookies.update(client.cookies)
        assert len(restarted.get('/api/plans').json()) == 2
        assert restarted.get('/api/session').json()['current_result']['result_set_id'] == result['ui_patch']['result_set_id']
    assert all(not r['priority'] for r in snapshot(factory).values())


def test_assistant_parent_plan_tracks_confirmation_completion(lab):
    client, _, _, _ = lab
    query(client, {"status": "paid", "shipping_city": "北京"})
    version = client.get('/api/session').json()['context_version']
    reply = client.post('/api/assistant', json={"message": "这些订单标为优先处理", "expected_context_version": version}).json()
    assert reply['state'] == 'WAITING_CONFIRMATION'
    assert confirm_apply(client, reply['preview']).status_code == 200
    plans = {p['id']: p for p in client.get('/api/plans').json()}
    assert plans[reply['plan_id']]['state'] == 'SUCCEEDED'
    assert plans[reply['plan_id']]['result']['operation']['affected_ids'] == [4]


def test_concurrent_duplicate_apply_one_operation(lab):
    client, factory, settings, engine = lab
    preview = make_preview(client, query(client, {"status": "paid"})).json()
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": preview['digest']}).status_code == 200
    def run(_):
        with TestClient(create_app(settings, engine), headers={"X-LLM4Web-Client": "web"}) as worker:
            worker.cookies.update(client.cookies)
            return worker.post(f"/api/previews/{preview['preview_id']}/apply", json={"idempotency_key": "concurrent-key"}).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0] == results[1]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 1


def test_demo_disabled_csrf_guard_and_strict_schema(lab):
    client, _, settings, engine = lab
    assert client.post('/api/auth/demo', json={}, headers={"X-LLM4Web-Client": ""}).status_code == 403
    assert client.post('/api/auth/demo', json={}, headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.post('/api/auth/demo', json={"actor": "admin"}).status_code == 422
    assert client.post('/api/orders/query', json={"expected_context_version": 0, "filters": {"created_at_gte": "2026-10-19T00:00:00"}}).status_code == 422
    settings.auth_mode = 'disabled'
    with TestClient(create_app(settings, engine), headers={"X-LLM4Web-Client": "web"}) as other:
        assert other.post('/api/auth/demo', json={}).status_code == 403


def test_refinement_preserves_scope_and_applies_explicit_sort(lab):
    client, _, _, _ = lab
    result = query(client, {"shipping_city": "上海", "status": "paid"})
    reply = client.post('/api/assistant', json={"message": "这些订单按金额从高到低展示", "expected_context_version": result['context_version']}).json()
    assert [r['id'] for r in reply['ui_patch']['rows']] == [12,3,1,2,9,10]
    conflict = client.post('/api/orders/refine', json={"filters":{"shipping_city":"北京"},"expected_context_version":reply['context_version']})
    assert conflict.json()['code'] == 'FILTER_CONFLICT'


def test_natural_language_pagination_retains_result_set(lab):
    client, _, _, _ = lab
    result = query(client, {"status":"paid"}, page_size=2)
    reply = client.post('/api/assistant', json={"message":"第二页", "expected_context_version":result['context_version']}).json()
    assert reply['ui_patch']['result_set_id'] == result['result_set_id']
    assert [r['id'] for r in reply['ui_patch']['rows']] == [3,4]


def test_oversized_batch_refused_without_silent_truncation(lab):
    client, factory, _, _ = lab
    from decimal import Decimal
    with factory.begin() as db:
        source = db.get(Order, 1)
        for i in range(1000,1501):
            db.add(Order(id=i, tenant_id='a', customer_id=1, shipping_city='成都', status='paid',
                         total_amount=Decimal('0.10'), created_at=source.created_at, paid_at=source.paid_at,
                         priority=False, review_status='normal', version=1))
    result = query(client, {"shipping_city":"成都"})
    assert result['total'] == 501
    before = snapshot(factory)
    assert make_preview(client,result).json()['code'] == 'TOO_MANY_TARGETS'
    assert snapshot(factory) == before
