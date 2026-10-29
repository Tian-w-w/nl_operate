from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import event, func, select

from backend.app.config import now
from backend.app.models import Export, Order, Session
from backend.app.tools import ToolContext, execute_tool, tool_catalog
from evaluation.tool_contracts import cases, check_final, oracles, replay_case


def context(client, **changes):
    values = dict(session_token=client.cookies.get('llm4web_session'), task_id='contract-test', call_id='query-1',
                  expected_context_version=client.get('/api/session').json()['context_version'])
    return ToolContext(**{**values, **changes})


def test_exactly_two_tool_schemas_hide_server_fields():
    catalog = tool_catalog()
    assert [entry['name'] for entry in catalog] == ['query_orders', 'export_orders']
    for entry in catalog:
        schema = entry['input_schema']
        assert schema['additionalProperties'] is False
        assert not {'tenant_id', 'actor_id', 'expected_context_version', 'idempotency_key', 'ids'} & schema['properties'].keys()
    assert catalog[1]['input_schema']['required'] == ['result_set_id']


def test_query_updates_real_page_and_export_covers_all_pages(lab):
    client, factory, settings, _ = lab
    ctx = context(client)
    result = execute_tool(factory, settings, 'query_orders', {'filters': {'shipping_city': '上海', 'status': 'paid'},
                          'sort': [{'field': 'total_amount', 'direction': 'desc'}], 'page_size': 2}, ctx)
    assert result.status == 'SUCCEEDED' and result.output.total == 6
    assert [row.id for row in result.output.rows] == [12, 3]
    current = client.get('/api/session').json()['current_result']
    assert current['result_set_id'] == result.output.result_set_id
    assert [row['id'] for row in current['rows']] == [12, 3]
    export_context = replace(ctx, call_id='export-1', expected_context_version=result.context_version,
                             observed_result_set_ids={result.output.result_set_id})
    args = {'result_set_id': result.output.result_set_id, 'columns': ['id', 'total_amount']}
    exported = execute_tool(factory, settings, 'export_orders', args, export_context)
    assert exported.status == 'SUCCEEDED' and exported.output.row_count == 6
    content = client.get(exported.output.download_url).content.decode('utf-8-sig')
    assert [line.split(',')[0] for line in content.splitlines()[1:]] == ['12', '3', '1', '2', '9', '10']
    retry = execute_tool(factory, settings, 'export_orders', args, export_context)
    assert retry == exported
    conflict = execute_tool(factory, settings, 'export_orders', {**args, 'columns': ['id']}, export_context)
    assert conflict.status == 'FAILED' and conflict.error.code == 'IDEMPOTENCY_CONFLICT'
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Export)) == 1


@pytest.mark.parametrize('name,args,code', [
    ('delete_orders', {}, 'UNKNOWN_TOOL'),
    ('query_orders', {'tenant_id': 'b'}, 'INVALID_TOOL_ARGUMENTS'),
    ('query_orders', {'expected_context_version': 100}, 'INVALID_TOOL_ARGUMENTS'),
    ('query_orders', {'page': 0}, 'INVALID_TOOL_ARGUMENTS'),
    ('query_orders', {'filters': {'created_at_gte': '2026-10-26T00:00:00'}}, 'INVALID_TOOL_ARGUMENTS'),
    ('query_orders', {'filters': {'amount_gte': '100', 'amount_lte': '50'}}, 'INVALID_TOOL_ARGUMENTS'),
    ('query_orders', {'filters': {'unknown_field': 'x'}}, 'INVALID_TOOL_ARGUMENTS'),
    ('export_orders', {'result_set_id': 'invented', 'columns': ['note']}, 'INVALID_TOOL_ARGUMENTS'),
    ('export_orders', {'result_set_id': 'invented', 'ids': [1, 2]}, 'INVALID_TOOL_ARGUMENTS'),
    ('export_orders', {'result_set_id': 'invented'}, 'RESULT_NOT_OBSERVED'),
])
def test_invalid_calls_are_explicit_failures_without_page_effects(lab, name, args, code):
    client, factory, settings, _ = lab
    result = execute_tool(factory, settings, name, args, context(client))
    assert result.status == 'FAILED' and result.error.code == code
    assert 'output' not in result.model_dump() and not result.error.retryable
    assert 'current_result' not in client.get('/api/session').json()


def test_ownership_and_unobserved_results_are_both_enforced(lab):
    client, factory, settings, _ = lab
    found = execute_tool(factory, settings, 'query_orders', {}, context(client))
    ident = found.output.result_set_id
    unobserved = execute_tool(factory, settings, 'export_orders', {'result_set_id': ident}, context(client))
    assert unobserved.error.code == 'RESULT_NOT_OBSERVED'
    # Even erroneously adding a foreign ID to trusted observations cannot bypass ownership.
    client.post('/api/auth/demo', json={'actor': 'operator_b'})
    foreign = execute_tool(factory, settings, 'export_orders', {'result_set_id': ident},
                           context(client, observed_result_set_ids={ident}))
    assert foreign.error.code == 'NOT_FOUND'


def test_order_version_change_and_expired_session_stop_execution(lab):
    client, factory, settings, _ = lab
    ctx = context(client)
    found = execute_tool(factory, settings, 'query_orders', {}, ctx)
    ctx = replace(ctx, expected_context_version=found.context_version, observed_result_set_ids={found.output.result_set_id})
    with factory.begin() as db:
        db.get(Order, 1).version += 1
    result = execute_tool(factory, settings, 'export_orders', {'result_set_id': found.output.result_set_id}, ctx)
    assert result.error.code == 'VERSION_CONFLICT'
    with factory.begin() as db:
        db.get(Session, ctx.session_token).expires_at = now()-timedelta(seconds=1)
    result = execute_tool(factory, settings, 'query_orders', {}, ctx)
    assert result.error.code == 'UNAUTHENTICATED'


def test_commit_failure_never_reports_success(lab):
    client, factory, settings, engine = lab
    ctx = context(client)
    def deny_commit(connection):
        raise RuntimeError('private-commit-error')
    event.listen(engine, 'commit', deny_commit)
    try:
        result = execute_tool(factory, settings, 'query_orders', {}, ctx)
    finally:
        event.remove(engine, 'commit', deny_commit)
    assert result.status == 'FAILED' and result.error.code == 'TOOL_EXECUTION_FAILED'
    assert 'private-commit-error' not in result.model_dump_json()
    assert 'current_result' not in client.get('/api/session').json()


def test_invalid_export_output_rolls_back_instead_of_reporting_success(lab, monkeypatch):
    from backend.app import service
    client, factory, settings, _ = lab
    found = execute_tool(factory, settings, 'query_orders', {}, context(client))
    ctx = context(client, observed_result_set_ids={found.output.result_set_id})
    original = service.export_orders
    def wrong_output(*args, **kwargs):
        output = original(*args, **kwargs)
        return {**output, 'state': 'FAILED'}
    monkeypatch.setattr(service, 'export_orders', wrong_output)
    result = execute_tool(factory, settings, 'export_orders', {'result_set_id': found.output.result_set_id}, ctx)
    assert result.status == 'FAILED' and result.error.code == 'TOOL_EXECUTION_FAILED'
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Export)) == 0


CONTRACT_CASES = [task for task in cases() if task['phase'] == 'tool_contract']


@pytest.mark.parametrize('task', CONTRACT_CASES, ids=lambda task: task['task_id'])
def test_prewritten_contract_scenarios(task, tmp_path):
    result = replay_case(task, oracles()[task['task_id']], tmp_path)
    assert result['passed'], result['checks']
    assert result['model_calls'] == result['model_tokens'] == 0


def test_oracle_inventory_has_deferred_agent_standards_and_strict_boundaries():
    tasks, labels = cases(), oracles()
    assert len(tasks) == len(labels) == 24 and len(CONTRACT_CASES) == 20
    assert set(task['task_id'] for task in tasks) == set(labels)
    assert [len(labels[f'branch_{i:02}']['query_ids']) for i in [1, 2, 3, 4]] == [12, 9, 10, 0]
    assert [labels[f'branch_{i:02}']['export_count'] for i in [1, 2, 3, 4]] == [1, 0, 0, 0]
    assert 113 not in labels['branch_01']['query_ids']  # Exactly 48h is excluded.
    assert 104 not in labels['branch_05']['query_ids']  # Exactly 72h is excluded.
    assert 401 not in labels['branch_01']['query_ids']  # Foreign tenant.


@pytest.mark.parametrize('mutation', ['visible_ids', 'visible_total', 'visible_amount', 'query_ids', 'export_ids', 'csv_values_correct', 'export_count', 'business'])
def test_final_checker_rejects_wrong_page_files_and_extra_operations(tmp_path, mutation):
    task = cases()[0]
    oracle = oracles()[task['task_id']]
    result = replay_case(task, oracle, tmp_path)
    assert result['passed']
    observed = deepcopy(result['observed'])
    if mutation in {'visible_ids', 'query_ids', 'export_ids'}:
        observed[mutation] = list(reversed(observed[mutation]))
    elif mutation == 'business':
        observed['after']['orders'][0]['priority'] = True
    elif mutation == 'export_count':
        observed['export_count'] += 1
    elif mutation == 'visible_total':
        observed['visible_total'] += 1
    elif mutation == 'visible_amount':
        observed['visible_amount'] = '0.00'
    else:
        observed[mutation] = False
    assert not check_final(oracle, observed)['passed']
    assert 'private-simulated-backend-failure' not in json.dumps(result['events'])
