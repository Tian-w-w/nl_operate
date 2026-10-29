from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest
from sqlalchemy import event, func, select

from backend.app import service
from backend.app.agent_runtime import drive_run, get_run, start_run
from backend.app.config import now
from backend.app.model_adapter import ModelReply, OpenAIAdapter
from backend.app.models import AgentRun, AgentStep, Export, ModelCall, Plan, Session
from evaluation.scripted_decisions import ScriptedDecisions

QUERY = {'kind': 'call_tool', 'tool': 'query_orders', 'arguments': {'filters': {'shipping_city': '上海', 'waiting_hours_gt': 48}, 'sort': [{'field': 'total_amount', 'direction': 'desc'}]}}
EXPORT = {'kind': 'call_tool', 'tool': 'export_orders', 'arguments': {'result_set_id': '$result_set'}}
FINISH = {'kind': 'finish', 'result_set_id': '$result_set', 'total': '$total', 'export_id': '$export'}


def run_script(lab, decisions, settings=None, hook=None):
    client, factory, default, _ = lab
    settings = settings or default
    adapter = ScriptedDecisions(decisions, hook)
    token = client.cookies.get('llm4web_session')
    version = client.get('/api/session').json()['context_version']
    run = start_run(factory, token, '测试只读查询与导出', version, settings, adapter)
    drive_run(factory, token, run['id'], settings, adapter, 'test-request')
    return get_run(factory, token, run['id']), adapter


@pytest.mark.parametrize('export', [False, True])
def test_loop_uses_real_observation_then_finishes_with_evidence(lab, export):
    client, factory, _, _ = lab
    result, adapter = run_script(lab, [QUERY, *([EXPORT] if export else []), FINISH])
    assert result['state'] == 'SUCCEEDED' and result['tool_calls'] == (2 if export else 1)
    assert result['model_calls'] == (3 if export else 2)
    assert result['steps'][0]['observation']['output']['total'] == 4
    observed = adapter.states[1]['observations'][0]['output']
    assert observed['total'] == 4 and 'rows' not in observed and 'ids' not in observed
    assert '4条' in result['message']
    assert client.get('/api/session').json()['current_result']['total'] == 4
    if export:
        downloaded = client.get(result['export']['download_url'])
        assert downloaded.status_code == 200 and result['export']['row_count'] == 4
    else:
        assert result['export'] is None
    with factory() as db:
        assert db.get(Plan, result['id']).state == 'SUCCEEDED'
        assert len(list(db.scalars(select(AgentStep)))) == result['model_calls']


@pytest.mark.parametrize('decision,error', [
    ({'kind': 'call_tool', 'tool': 'delete_orders', 'arguments': {}}, 'UNKNOWN_TOOL'),
    ({'kind': 'call_tool', 'tool': 'query_orders', 'arguments': {'tenant_id': 'b'}}, 'INVALID_TOOL_ARGUMENTS'),
    ({'kind': 'call_tool', 'tool': 'export_orders', 'arguments': {'result_set_id': 'fabricated'}}, 'RESULT_NOT_OBSERVED'),
    ({'kind': 'finish', 'result_set_id': 'fabricated', 'total': 0}, 'COMPLETION_EVIDENCE'),
    ({'kind': 'finish', 'result_set_id': '$result_set', 'total': 999}, 'COMPLETION_EVIDENCE'),
    ({'kind': 'finish', 'result_set_id': '$result_set', 'total': '$total', 'export_id': 'fake-file'}, 'COMPLETION_EVIDENCE'),
    ({'kind': 'finish', 'result_set_id': '$result_set', 'total': '$total', 'extra_identity': 'b'}, 'MODEL_INVALID_DECISION'),
])
def test_bad_decision_never_becomes_a_success(lab, decision, error):
    result, _ = run_script(lab, [QUERY, decision])
    assert result['state'] == 'FAILED' and result['error'] == error
    assert result['export'] is None


def test_clarification_stops_without_query_and_cannot_resume_implicitly(lab):
    result, adapter = run_script(lab, [{'kind': 'clarify', 'question': '请指定等待小时数和导出数量阈值。'}, QUERY])
    assert result['state'] == 'WAITING_CLARIFICATION' and result['tool_calls'] == 0
    assert len(adapter.states) == 1


@pytest.mark.parametrize('settings,decisions,error,tools', [
    ({'agent_max_model_calls': 1}, [QUERY, EXPORT], 'MODEL_CALL_LIMIT', 1),
    ({'agent_max_tool_calls': 1}, [QUERY, EXPORT], 'TOOL_CALL_LIMIT', 1),
    ({}, [QUERY, QUERY, FINISH], 'REPEATED_TOOL_CALL', 1),
])
def test_execution_limits_are_durable_and_stop_before_extra_tool(lab, settings, decisions, error, tools):
    result, _ = run_script(lab, decisions, settings=replace(lab[2], **settings))
    assert result['state'] == 'FAILED' and result['error'] == error and result['tool_calls'] == tools
    with lab[1]() as db:
        assert db.scalar(select(func.count()).select_from(Export)) == 0


def test_model_wait_has_no_db_transaction_and_page_change_cancels_task(lab):
    client, factory, _, _ = lab
    token = client.cookies.get('llm4web_session')
    def change(index, state):
        if index == 1:
            with factory.begin() as db:
                run = db.scalar(select(AgentRun))
                assert run.phase == 'DECIDING' and run.context_version == 1
                prior = db.scalar(select(AgentStep).where(AgentStep.ordinal == 1))
                assert run.observed_ids == [prior.observation['output']['result_set_id']]
                service.query_orders(db, db.get(Session, token), service.QueryRequest(filters={'shipping_city': '北京'}, expected_context_version=1), lab[2])
    result, _ = run_script(lab, [QUERY, EXPORT, FINISH], hook=change)
    assert result['error'] == 'STALE_CONTEXT'
    assert client.get('/api/session').json()['current_result']['total'] == 1


@pytest.mark.parametrize('stage', ['model', 'tool'])
def test_total_deadline_prevents_late_success_or_tool_commit(lab, monkeypatch, stage):
    factory = lab[1]
    def expire():
        # Within a tool's transaction, use that same session to avoid locking.
        with factory.begin() as db:
            db.scalar(select(AgentRun)).deadline_at = now()-timedelta(seconds=1)
    def hook(index, state):
        if stage == 'model':
            expire()
    if stage == 'tool':
        original = service.query_orders
        def slow(db, *args, **kwargs):
            output = original(db, *args, **kwargs)
            db.scalar(select(AgentRun)).deadline_at = now()-timedelta(seconds=1)
            return output
        monkeypatch.setattr(service, 'query_orders', slow)
    result, _ = run_script(lab, [QUERY, FINISH], hook=hook)
    assert result['state'] == 'FAILED' and result['error'] in {'TIME_LIMIT', 'MODEL_INTERRUPTED'}
    if stage == 'tool':
        assert 'current_result' not in lab[0].get('/api/session').json()


def test_expired_session_during_model_wait_still_persists_failure(lab):
    client, factory, _, _ = lab
    token = client.cookies.get('llm4web_session')
    def hook(index, state):
        with factory.begin() as db:
            db.get(Session, token).expires_at = now()-timedelta(seconds=1)
    # Public reads require auth; inspect durable failure through the database.
    adapter = ScriptedDecisions([QUERY], hook)
    run = start_run(factory, token, '测试过期', 0, lab[2], adapter)
    drive_run(factory, token, run['id'], lab[2], adapter, 'test')
    with factory() as db:
        assert db.get(AgentRun, run['id']).error == 'SESSION_EXPIRED'


def test_background_api_state_and_history_are_owned_and_reloadable(lab):
    client, _, _, _ = lab
    assert client.post('/api/agent/runs', json={'goal': '测试', 'expected_context_version': 0}).status_code == 503
    client.app.state.adapter = ScriptedDecisions([QUERY, EXPORT, FINISH])
    assert client.get('/api/session').json()['agent_available']
    response = client.post('/api/agent/runs', json={'goal': '测试', 'expected_context_version': 0})
    assert response.status_code == 202
    ident = response.json()['id']
    final = client.get('/api/agent/runs/'+ident).json()
    assert final['state'] == 'SUCCEEDED' and len(final['steps']) == 3
    assert client.get('/api/agent/runs').json()[0] == final
    assert len(client.get('/api/plans').json()) == 1
    client.post('/api/auth/demo', json={'actor': 'operator_b'})
    assert client.get('/api/agent/runs/'+ident).status_code == 404
    assert client.get('/api/agent/runs').json() == []


def test_real_adapter_decision_protocol_with_mock_http(lab):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        payload = json.loads(body['input'])
        query = next((o['output'] for o in payload['state']['observations'] if o['tool'] == 'query_orders'), None)
        decision = QUERY if query is None else {'kind': 'finish', 'result_set_id': query['result_set_id'], 'total': query['total'], 'export_id': None}
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'function_call', 'name': 'decide_next', 'arguments': json.dumps({'decision': decision})}],
                                         'usage': {'input_tokens': 100, 'output_tokens': 50}})
    settings = replace(lab[2], model_adapter='openai', model_enabled=True, model_name='mock-model', model_api_key='mock-key',
                       model_budget_usd=Decimal('1'), model_project_budget_usd=Decimal('10'),
                       model_input_rate=Decimal('1'), model_output_rate=Decimal('1'))
    adapter = OpenAIAdapter(settings, transport=httpx.MockTransport(respond))
    token = lab[0].cookies.get('llm4web_session')
    run = start_run(lab[1], token, '上海等待超过48小时，只展示', 0, settings, adapter)
    drive_run(lab[1], token, run['id'], settings, adapter, 'mock-http')
    final = get_run(lab[1], token, run['id'])
    assert final['state'] == 'SUCCEEDED' and final['input_tokens'] == 200 and final['output_tokens'] == 100
    assert all(request['tools'][0]['name'] == 'decide_next' and request['tools'][0]['strict'] for request in requests)
    assert 'rows' not in json.loads(requests[1]['input'])['state']['observations'][0]['output']


def test_unknown_cost_preserves_reservation_across_sessions(lab):
    class BillableTestDouble(ScriptedDecisions):
        billable = True
        def estimate_decision_reservation(self, *args):
            return Decimal('0.60')
        def decide(self, *args, **kwargs):
            return ModelReply(super().decide(*args, **kwargs).action)
    client, factory, defaults, _ = lab
    settings = replace(defaults, model_enabled=True, model_budget_usd=Decimal('0.70'), model_project_budget_usd=Decimal('1'))
    adapter = BillableTestDouble([QUERY, FINISH])
    token = client.cookies.get('llm4web_session')
    run = start_run(factory, token, '测试预算', 0, settings, adapter)
    drive_run(factory, token, run['id'], settings, adapter, 'cost-test')
    final = get_run(factory, token, run['id'])
    assert final['error'] == 'MODEL_BUDGET_EXCEEDED' and final['unknown_usage_calls'] == 1
    client.post('/api/auth/demo', json={'actor': 'operator_a'})
    token = client.cookies.get('llm4web_session')
    second = start_run(factory, token, '测试跨会话预算', 0, settings, adapter)
    drive_run(factory, token, second['id'], settings, adapter, 'cost-test')
    assert get_run(factory, token, second['id'])['error'] == 'MODEL_BUDGET_EXCEEDED'
    with factory() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 1 and calls[0].reserved_cost == Decimal('0.60') and calls[0].actual_cost is None


def test_failed_finish_preserves_actual_export_without_claiming_success(lab):
    result, _ = run_script(lab, [QUERY, EXPORT, {'kind': 'finish', 'result_set_id': '$result_set', 'total': 999}])
    assert result['state'] == 'FAILED' and result['export'] is None
    output = result['steps'][1]['observation']['output']
    assert lab[0].get(output['download_url']).status_code == 200


def test_duplicate_worker_cannot_repeat_a_finished_task(lab):
    result, adapter = run_script(lab, [QUERY, EXPORT, FINISH])
    token = lab[0].cookies.get('llm4web_session')
    drive_run(lab[1], token, result['id'], lab[2], adapter, 'duplicate-worker')
    assert len(adapter.states) == 3
    with lab[1]() as db:
        assert db.scalar(select(func.count()).select_from(Export)) == 1


def test_uncertain_commit_preserves_durable_observation_and_stops(lab, monkeypatch):
    original = service.query_orders
    def query_with_lost_ack(db, *args, **kwargs):
        result = original(db, *args, **kwargs)
        def lose_ack(session):
            raise RuntimeError('lost-commit-acknowledgement')
        event.listen(db, 'after_commit', lose_ack, once=True)
        return result
    monkeypatch.setattr(service, 'query_orders', query_with_lost_ack)
    result, adapter = run_script(lab, [QUERY, FINISH])
    assert result['state'] == 'FAILED' and result['error'] == 'TOOL_STATUS_UNCERTAIN'
    assert result['steps'][0]['observation']['status'] == 'SUCCEEDED'
    assert lab[0].get('/api/session').json()['current_result']['total'] == 4
    assert len(adapter.states) == 1


@pytest.mark.parametrize('decision', [[], 'not-json-decision', None, {'kind': 'unsupported'}])
def test_malformed_decisions_still_have_readable_failed_status(lab, decision):
    result, _ = run_script(lab, [decision])
    assert result['state'] == 'FAILED' and result['error'] == 'MODEL_INVALID_DECISION'
    assert result['tool_calls'] == 0
