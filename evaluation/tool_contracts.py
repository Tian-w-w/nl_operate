"""Replay PREWRITTEN tool calls against static oracles, not an Agent evaluation.

No instruction parser, model, conditional planner or branch policy is used here.
The oracle specifies the reference calls; a future model runner must never read it.
"""
from contextlib import nullcontext
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app import service
from backend.app.config import ROOT, Settings, now
from backend.app.db import make_engine, session_factory
from backend.app.main import create_app
from backend.app.models import AuditLog, Base, Customer, Export, Operation, Order, OrderItem, Product, ResultSet, Session, Shipment
from backend.app.tools import ToolContext, execute_tool
from evaluation.branch_fixture import FIXTURE_PATH, load_fixture

TASK_PATH = ROOT / 'data/tasks/agent_branch.dev.jsonl'
ORACLE_PATH = ROOT / 'data/oracles/agent_branch_v1.json'


def cases():
    return [json.loads(line) for line in TASK_PATH.read_text(encoding='utf-8').splitlines()]


def oracles():
    return json.loads(ORACLE_PATH.read_text(encoding='utf-8'))['cases']


def snapshot_business(factory):
    def encode(value):
        return value.isoformat() if isinstance(value, datetime) else str(value) if value.__class__.__name__ == 'Decimal' else value
    with factory() as db:
        return {model.__tablename__: [{key: encode(value) for key, value in record.items()}
                for record in db.execute(select(model.__table__).order_by(model.id)).mappings()]
                for model in [Order, Customer, Product, OrderItem, Shipment]}


def check_final(oracle, observed):
    """No natural-language answer is used as a success signal."""
    checks = {
        'tool_sequence': observed['tools'] == oracle['tools'],
        'tool_status': observed['status'] == oracle['terminal_state'],
        'error': observed['error'] == oracle['error'],
        'query_membership_and_order': observed['query_ids'] == oracle['query_ids'],
        'visible_page': observed['visible_ids'] == oracle['visible_ids'],
        'visible_count': observed['visible_total'] == oracle['visible_total'],
        'visible_amount': observed['visible_amount'] == oracle['visible_amount'],
        'query_amount': observed['total_amount'] == oracle['total_amount'],
        'context_version': observed['context_version'] == oracle['expected_context_version'],
        'export_count': observed['export_count'] == oracle['export_count'],
        'export_order': observed['export_ids'] == oracle['export_ids'],
        'business_unchanged': observed['before'] == observed['after'],
        'no_business_operations': observed['operation_count'] == observed['audit_count'] == 0,
        'transaction_rollback': observed['result_count'] == oracle['result_set_count'],
        'page_error': observed['snapshot_error'] == ('RESULT_EXPIRED' if oracle['error'] == 'RESULT_EXPIRED' else None),
        'query_contract': observed['query_matches'],
    }
    if oracle['export_count']:
        checks.update(csv_columns=observed['csv_columns'] == oracle['columns'], csv_values=observed['csv_values_correct'],
                      csv_bom=observed['csv_bom'], csv_checksum=observed['csv_checksum'],
                      export_result_binding=observed['export_bound'])
    return {'passed': all(checks.values()), 'checks': checks}


def replay_case(task, oracle, root):
    """Only contract cases; Agent-only standards are explicitly deferred."""
    if task['phase'] != 'tool_contract':
        return {'task_id': task['task_id'], 'status': 'NOT_RUN', 'reason': 'Requires the next Agent-loop milestone'}
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{task['task_id']}.db"
    if path.exists():
        raise ValueError('Refusing to reuse a contract database')
    fixture = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    settings = Settings(database_url=f'sqlite:///{path.as_posix()}', reference_time=fixture['reference_time'],
                        auth_mode='demo', model_adapter='rules', model_enabled=False)
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    with factory.begin() as db:
        load_fixture(db)
    before = snapshot_business(factory)
    started = time.perf_counter()
    events, observed_ids, version, last, query_id = [], frozenset(), 0, None, None
    observed = {'query_ids': [], 'total_amount': '0.00', 'query_matches': True, 'export_ids': [], 'csv_columns': [],
                'csv_bom': False, 'csv_checksum': False, 'csv_values_correct': False, 'export_bound': False}
    try:
        with TestClient(create_app(settings, engine), headers={'X-LLM4Web-Client': 'web'}) as client:
            client.post('/api/auth/demo', json={'actor': task['actor']}).raise_for_status()
            token = client.cookies.get('llm4web_session')
            for index, name in enumerate(oracle['tools']):
                args = dict(oracle['query']) if name == 'query_orders' else {'result_set_id': query_id, 'columns': oracle['columns']}
                if task['fault'] == 'invalid_arguments':
                    args = {**args, 'sort': [{'field': 'arbitrary_sql', 'direction': 'desc'}]}
                if index > 0 and task['fault'] == 'result_expired':
                    with factory.begin() as db:
                        db.get(ResultSet, query_id).expires_at = now()-timedelta(seconds=1)
                if index > 0 and task['fault'] == 'page_changed':
                    with factory.begin() as db:
                        service.query_orders(db, db.get(Session, token), service.QueryRequest(
                            filters={'shipping_city': '北京'}, expected_context_version=version), settings)
                fault_name = {'query_failure': 'query_orders', 'export_failure': 'export_orders'}.get(task['fault'])
                original = getattr(service, name, None)
                def fail_after_effect(*args, **kwargs):
                    original(*args, **kwargs)
                    raise RuntimeError('private-simulated-backend-failure')
                injected = patch.object(service, name, fail_after_effect) if name == fault_name else nullcontext()
                with injected:
                    last = execute_tool(factory, settings, name, args, ToolContext(token, task['task_id'], str(index), version, observed_ids))
                events.append({'tool': name, 'arguments': args, 'observation': last.model_dump(mode='json')})
                if last.status == 'FAILED':
                    break
                version = last.context_version
                if name == 'query_orders':
                    query_id = last.output.result_set_id
                    observed_ids = observed_ids | {query_id}
                    with factory() as db:
                        observed['query_ids'] = db.get(ResultSet, query_id).ids
                    observed['total_amount'] = last.output.total_amount
                    actual = last.output.query.model_dump(mode='json')
                    actual['filters'] = {k: v for k, v in actual['filters'].items() if v is not None}
                    observed['query_matches'] = actual == oracle['query'] and last.output.total == len(oracle['query_ids'])
            page = client.get('/api/session').json()
            observed.update(context_version=page['context_version'],
                            visible_ids=[row['id'] for row in page.get('current_result', {}).get('rows', [])],
                            visible_total=page.get('current_result', {}).get('total'),
                            visible_amount=page.get('current_result', {}).get('total_amount'),
                            snapshot_error=page.get('snapshot_error', {}).get('code'))
            with factory() as db:
                exports = list(db.scalars(select(Export)))
                observed.update(export_count=len(exports), operation_count=db.scalar(select(func.count()).select_from(Operation)),
                                audit_count=db.scalar(select(func.count()).select_from(AuditLog)),
                                result_count=db.scalar(select(func.count()).select_from(ResultSet)))
            if exports:
                item = exports[0]
                response = client.get(f'/api/exports/{item.id}/download')
                response.raise_for_status()
                content = response.content
                (root/f"{task['task_id']}.csv").write_bytes(content)
                reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
                records = list(reader)
                by_id = {row['id']: row for row in before['orders']}
                expected_records = [{col: str(by_id[ident][col]) for col in oracle['columns']} for ident in oracle['export_ids']]
                observed.update(export_ids=[int(row['id']) for row in records], csv_columns=reader.fieldnames,
                                csv_bom=content.startswith(b'\xef\xbb\xbf'),
                                csv_checksum=hashlib.sha256(content).hexdigest() == item.checksum,
                                csv_values_correct=records == expected_records,
                                export_bound=item.result_set_id == query_id and item.row_count == len(oracle['export_ids']))
    finally:
        engine.dispose()
    observed.update(before=before, after=snapshot_business(factory), tools=[event['tool'] for event in events],
                    status=last.status, error=last.error.code if last.status == 'FAILED' else None)
    engine.dispose()
    verdict = check_final(oracle, observed)
    result = {'task_id': task['task_id'], 'scope': 'scripted tool contract replay; no Agent decision',
              'status': observed['status'], **verdict, 'elapsed_seconds': time.perf_counter()-started,
              'tool_calls': len(events), 'model_calls': 0, 'model_tokens': 0, 'events': events, 'observed': observed}
    (root/f"{task['task_id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def run_contracts(run_dir=None):
    run_dir = Path(run_dir) if run_dir else ROOT/'experiments/runs'/('tools-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    run_dir.mkdir(parents=True, exist_ok=False)
    labels = oracles()
    results = [replay_case(task, labels[task['task_id']], run_dir/'artifacts') for task in cases()]
    evaluated = [result for result in results if result['status'] != 'NOT_RUN']
    summary = {'scope': 'tool contracts only; scripted reference calls, not model task success',
               'prepared_tasks': len(results), 'contract_cases': len(evaluated), 'passed': sum(r['passed'] for r in evaluated),
               'agent_cases_not_run': len(results)-len(evaluated), 'model_calls': 0, 'model_tokens': 0,
               'tool_calls': sum(r['tool_calls'] for r in evaluated), 'elapsed_seconds': sum(r['elapsed_seconds'] for r in evaluated),
               'run_dir': str(run_dir.resolve())}
    summary['expected_error_cases'] = {code: sum(r['observed']['error'] == code for r in evaluated)
                                       for code in sorted({r['observed']['error'] for r in evaluated if r['observed']['error']})}
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in [TASK_PATH, ORACLE_PATH, FIXTURE_PATH, ROOT/'backend/app/tools.py', ROOT/'backend/app/schemas.py',
                           ROOT/'backend/app/service.py', ROOT/'evaluation/branch_fixture.py', Path(__file__)]}
    (run_dir/'manifest.json').write_text(json.dumps({'scope': summary['scope'], 'source_sha256': hashes,
        'fixture': 'agent_branch_fixture_v1', 'reference_time': '2026-10-26T12:00:00+08:00',
        'oracle_independently_reviewed': False}, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir/'tasks.jsonl').write_text(''.join(json.dumps({k:v for k,v in r.items() if k not in {'observed','events'}}, ensure_ascii=False)+'\n' for r in results), encoding='utf-8')
    (run_dir/'events.jsonl').write_text(''.join(json.dumps({'task_id':r['task_id'], **e}, ensure_ascii=False)+'\n' for r in evaluated for e in r['events']), encoding='utf-8')
    return summary


if __name__ == '__main__':
    summary = run_contracts()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(int(summary['passed'] != summary['contract_cases']))
