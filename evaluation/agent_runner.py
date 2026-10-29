"""Agent-loop evaluator: real decisions or explicitly scripted regression.

The real adapter receives only goal + runtime observations. Labels are held by
the evaluator, never sent in model state. Fault injection cases are tagged.
"""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
import hashlib
import io
import json
import platform
from importlib.metadata import version
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app import service
from backend.app.agent_runtime import drive_run, get_run, start_run
from backend.app.config import ROOT, Settings, now
from backend.app.db import make_engine, session_factory
from backend.app.main import create_app
from backend.app.model_adapter import create_adapter
from backend.app.models import AgentRun, AuditLog, Base, Export, ModelCall, Operation, ResultSet, Session
from evaluation.branch_fixture import FIXTURE_PATH, load_fixture
from evaluation.scripted_decisions import ScriptedDecisions
from evaluation.tool_contracts import ORACLE_PATH, TASK_PATH, cases, check_final, oracles, snapshot_business


def equivalent_query(actual, expected):
    """Permit only known redundancies, not arbitrary extra filters."""
    def normalize(query):
        query = {**query, 'filters': {key: value for key, value in query['filters'].items() if value is not None}, 'sort': list(query['sort'])}
        filters = query['filters']
        if filters.get('waiting_hours_gt') is not None:
            if filters.get('status') == 'paid':
                filters.pop('status')
            if filters.get('statuses') == ['paid']:
                filters.pop('statuses')
        for field in ['amount_gte', 'amount_lte']:
            if field in filters:
                filters[field] = Decimal(str(filters[field]))
        if len(query['sort']) > 1 and query['sort'][-1] == {'field': 'id', 'direction': 'asc'}:
            query['sort'].pop()
        return query
    return normalize(actual) == normalize(expected)


def failure_category(error, failed):
    if not failed:
        return None
    if error in {'STALE_CONTEXT', 'RESULT_EXPIRED', 'VERSION_CONFLICT', 'SESSION_EXPIRED'}:
        return 'context'
    if error in {'INVALID_TOOL_ARGUMENTS', 'UNKNOWN_TOOL', 'MODEL_INVALID_DECISION'}:
        return 'parameters'
    if error in {'MODEL_CALL_LIMIT', 'TOOL_CALL_LIMIT', 'TIME_LIMIT', 'REPEATED_TOOL_CALL', 'MODEL_BUDGET_EXCEEDED'}:
        return 'budget'
    if error == 'COMPLETION_EVIDENCE' or 'export_count' in failed:
        return 'branch'
    if error:
        return 'tool_or_provider_execution'
    return 'understanding_or_query'


def reference_decisions(task, oracle):
    """Only for the scripted regression mode; never called in real mode."""
    if task['task_id'] == 'branch_21':
        return [{'kind': 'clarify', 'question': '请指定等待小时数和导出数量阈值。'}]
    decisions = []
    for name in oracle['agent_proposed_tools']:
        arguments = dict(oracle.get('query', {})) if name == 'query_orders' else {'result_set_id': '$result_set', 'columns': oracle['columns']}
        if task.get('fault') == 'invalid_arguments':
            arguments = {**arguments, 'sort': [{'field': 'arbitrary_sql', 'direction': 'desc'}]}
        decisions.append({'kind': 'call_tool', 'tool': name, 'arguments': arguments})
    decisions.append({'kind': 'finish', 'result_set_id': '$result_set', 'total': '$total', 'export_id': '$export'})
    return decisions


def evaluate_case(task, oracle, root, settings, real=False):
    ident, root = task['task_id'], Path(root)
    root.mkdir(parents=True, exist_ok=True)
    database = root/f'{ident}.db'
    if database.exists():
        raise ValueError('Refusing to reuse an evaluation database')
    overrides = {'agent_max_model_calls': 1} if ident == 'branch_22' else {}
    settings = replace(settings, database_url=f'sqlite:///{database.as_posix()}', reference_time='2026-10-26T12:00:00+08:00', auth_mode='demo', **overrides)
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    with factory.begin() as db:
        load_fixture(db)
    before = snapshot_business(factory)
    started = time.perf_counter()
    try:
        with TestClient(create_app(settings, engine), headers={'X-LLM4Web-Client': 'web'}) as client:
            client.post('/api/auth/demo', json={'actor': task['actor']}).raise_for_status()
            token = client.cookies.get('llm4web_session')
            def hook(index, state):
                if index != 1:
                    return
                if task.get('fault') == 'result_expired':
                    with factory.begin() as db:
                        run = db.scalar(select(AgentRun))
                        if run.result_set_id:
                            db.get(ResultSet, run.result_set_id).expires_at = now()-timedelta(seconds=1)
                if task.get('fault') == 'page_changed':
                    with factory.begin() as db:
                        session = db.get(Session, token)
                        service.query_orders(db, session, service.QueryRequest(filters={'shipping_city': '北京'}, expected_context_version=session.context_version), settings)
            base = create_adapter(settings) if real else ScriptedDecisions(reference_decisions(task, oracle), hook)
            # Real faults operate before the next provider call, not by altering its prompt.
            if real and task.get('fault') in {'result_expired', 'page_changed'}:
                original_decide = base.decide
                call_index = 0
                def decide(*args, **kwargs):
                    nonlocal call_index
                    hook(call_index, args[2])
                    call_index += 1
                    return original_decide(*args, **kwargs)
                base.decide = decide
            run = start_run(factory, token, task['instruction'], 0, settings, base)
            if ident == 'branch_24':
                with factory.begin() as db:
                    db.get(AgentRun, run['id']).deadline_at = now()-timedelta(seconds=1)
            fault_name = {'query_failure': 'query_orders', 'export_failure': 'export_orders'}.get(task.get('fault'))
            original = getattr(service, fault_name) if fault_name else None
            def fail_after_effect(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError('private-simulated-backend-failure')
            with patch.object(service, fault_name, fail_after_effect) if fault_name else nullcontext():
                drive_run(factory, token, run['id'], settings, base, 'eval-'+ident)
            final = get_run(factory, token, run['id'])
            page = client.get('/api/session').json()
            successful_queries = [step['observation']['output'] for step in final['steps']
                                  if step['observation'] and step['observation']['tool'] == 'query_orders' and step['observation']['status'] == 'SUCCEEDED']
            query = successful_queries[-1] if successful_queries else None
            observed = {'before': before, 'after': snapshot_business(factory), 'status': final['state'], 'error': final['error'],
                        'tools': [step['decision']['tool'] for step in final['steps'] if step['decision'] and step['decision'].get('kind') == 'call_tool'],
                        'query_ids': [], 'total_amount': query['total_amount'] if query else '0.00',
                        'query_matches': not query, 'visible_ids': [row['id'] for row in page.get('current_result', {}).get('rows', [])],
                        'visible_total': page.get('current_result', {}).get('total'), 'visible_amount': page.get('current_result', {}).get('total_amount'),
                        'context_version': page['context_version'], 'snapshot_error': page.get('snapshot_error', {}).get('code'),
                        'export_ids': [], 'csv_columns': [], 'csv_bom': False, 'csv_checksum': False, 'csv_values_correct': False, 'export_bound': False}
            with factory() as db:
                exports = list(db.scalars(select(Export)))
                observed.update(export_count=len(exports), result_count=db.scalar(select(func.count()).select_from(ResultSet)),
                                operation_count=db.scalar(select(func.count()).select_from(Operation)), audit_count=db.scalar(select(func.count()).select_from(AuditLog)))
                charged = sum((call.actual_cost if call.actual_cost is not None else call.reserved_cost
                               for call in db.scalars(select(ModelCall).where(ModelCall.billable.is_(True)))), Decimal(0))
                if query:
                    observed['query_ids'] = db.get(ResultSet, query['result_set_id']).ids
                    actual = dict(query['query'])
                    actual['filters'] = {key: value for key, value in actual['filters'].items() if value is not None}
                    observed['query_matches'] = equivalent_query(actual, oracle['query']) and query['total'] == len(oracle['query_ids'])
            if exports:
                item = exports[0]
                content = client.get(f'/api/exports/{item.id}/download').content
                (root/f'{ident}.csv').write_bytes(content)
                reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
                records = list(reader)
                by_id = {row['id']: row for row in before['orders']}
                expected = [{col: str(by_id[order_id][col]) for col in oracle['columns']} for order_id in oracle['export_ids']]
                observed.update(export_ids=[int(row['id']) for row in records], csv_columns=reader.fieldnames,
                                csv_bom=content.startswith(b'\xef\xbb\xbf'), csv_checksum=hashlib.sha256(content).hexdigest() == item.checksum,
                                csv_values_correct=records == expected,
                                export_bound=bool(query) and item.result_set_id == query['result_set_id'] and item.row_count == len(oracle['export_ids']))
            verdict = check_final({**oracle, 'tools': oracle['agent_proposed_tools']}, observed)
            verdict['checks']['tool_call_count'] = final['tool_calls'] == oracle['agent_tool_calls']
            # Real providers may take valid extra steps. Enforce bounds; exact counts are for scripted regression.
            verdict['checks']['model_call_count'] = final['model_calls'] <= final['max_model_calls'] if real else final['model_calls'] == oracle['agent_model_calls']
            verdict['checks']['successful_finish_evidence'] = final['state'] != 'SUCCEEDED' or bool(query) and (bool(final['export']) == bool(oracle['export_count']))
            verdict['passed'] = all(verdict['checks'].values())
            failed = [name for name, ok in verdict['checks'].items() if not ok]
            category = failure_category(final['error'], failed)
            result = {'task_id': ident, 'mode': 'real' if real else 'scripted', **verdict,
                      'failure_category': category, 'elapsed_seconds': time.perf_counter()-started,
                      'model_calls': final['model_calls'], 'tool_calls': final['tool_calls'], 'input_tokens': final['input_tokens'],
                      'output_tokens': final['output_tokens'], 'unknown_usage_calls': final['unknown_usage_calls'],
                      'estimated_cost_usd': final['estimated_cost_usd'], 'run': final, 'observed': observed}
            result['committed_cost_usd'] = str(charged)
            (root/f'{ident}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            return result
    finally:
        engine.dispose()


def run_agent_evaluation(run_dir=None, real=False):
    settings = Settings() if real else Settings(model_adapter='rules', model_enabled=False)
    if real and (settings.model_adapter not in {'openai', 'local'} or not settings.model_enabled):
        raise ValueError('真实评测未启用：请配置本地模型URL、模型名及可选Key，然后设置 MODEL_ADAPTER=local 和 MODEL_CALLS_ENABLED=true；未发起请求。')
    billable = real and settings.model_adapter == 'openai'
    settings = replace(settings, reference_time=json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))['reference_time'])
    root = Path(run_dir) if run_dir else ROOT/'experiments/runs'/('agent-'+('real-' if real else 'scripted-')+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:6])
    root.mkdir(parents=True, exist_ok=False)
    paths = [TASK_PATH, ORACLE_PATH, FIXTURE_PATH, *sorted((ROOT/'backend/app').glob('*.py')),
             *sorted((ROOT/'backend/migrations/versions').glob('*.py')),
             *sorted(path for path in (ROOT/'frontend/src').rglob('*') if path.is_file()),
             ROOT/'frontend/package-lock.json', ROOT/'requirements.lock',
             *sorted((ROOT/'evaluation').glob('*.py'))]
    manifest = {'mode': 'real' if real else 'scripted', 'model': settings.model_name if real else None,
                'provider': settings.model_adapter if real else 'scripted', 'reference_time': settings.reference_time,
                'local_protocol': settings.local_model_protocol if real and settings.model_adapter == 'local' else None,
                'rates': {'input': str(settings.model_input_rate), 'output': str(settings.model_output_rate)},
                'limits': {'models': settings.agent_max_model_calls, 'tools': settings.agent_max_tool_calls, 'seconds': settings.agent_max_seconds},
                'python': platform.python_version(), 'dependencies': {name: version(name) for name in ['fastapi','sqlalchemy','pydantic','httpx']},
                'split': 'dev', 'independently_reviewed': False,
                'source_sha256': {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}
    (root/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    labels, results, committed = oracles(), [], Decimal(0)
    for task in cases():
        if real and task['task_id'] in {'branch_19', 'branch_20', 'branch_23', 'branch_24'}:
            results.append({'task_id': task['task_id'], 'status': 'NOT_RUN', 'reason': 'Injected decision/deadline regression, separate from real-model benchmark'})
        elif billable and committed >= settings.model_project_budget_usd:
            results.append({'task_id': task['task_id'], 'status': 'NOT_RUN', 'reason': 'Evaluation total budget exhausted'})
        else:
            case_settings = settings
            if billable:
                remaining = settings.model_project_budget_usd-committed
                case_settings = replace(settings, model_project_budget_usd=remaining, model_budget_usd=min(settings.model_budget_usd, remaining))
            result = evaluate_case(task, labels[task['task_id']], root/'artifacts', case_settings, real)
            results.append(result)
            committed += Decimal(result['committed_cost_usd'])
            # Save progress after every case, including cost reservations with unknown usage.
            (root/'progress.json').write_text(json.dumps({'completed': len(results), 'committed_cost_usd': str(committed)}, indent=2), encoding='utf-8')
    evaluated = [item for item in results if item.get('status') != 'NOT_RUN']
    summary = {'mode': 'real' if real else 'scripted', 'scope': 'development Agent-loop evaluation; scripted results are NOT model results',
               'model': settings.model_name if real else None, 'provider': settings.model_adapter if real else 'scripted', 'prepared_tasks': len(results), 'evaluated': len(evaluated),
               'passed': sum(item['passed'] for item in evaluated), 'not_run': len(results)-len(evaluated),
               'task_success_rate': sum(item['passed'] for item in evaluated)/len(evaluated),
               'model_calls': sum(item['model_calls'] for item in evaluated), 'tool_calls': sum(item['tool_calls'] for item in evaluated),
               'real_model_attempts': sum(item['model_calls'] for item in evaluated) if real else 0,
               'input_tokens': sum(item['input_tokens'] for item in evaluated), 'output_tokens': sum(item['output_tokens'] for item in evaluated),
               'estimated_cost_usd': str(sum((Decimal(item['estimated_cost_usd']) for item in evaluated), Decimal(0))),
               'committed_cost_usd': str(committed),
               'unknown_usage_calls': sum(item['unknown_usage_calls'] for item in evaluated),
               'elapsed_seconds': sum(item['elapsed_seconds'] for item in evaluated), 'run_dir': str(root.resolve())}
    summary['failure_categories'] = {category: sum(item['failure_category'] == category for item in evaluated)
                                     for category in sorted({item['failure_category'] for item in evaluated if item['failure_category']})}
    (root/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (root/'tasks.jsonl').write_text(''.join(json.dumps({key: value for key, value in item.items() if key not in {'run', 'observed'}}, ensure_ascii=False)+'\n' for item in results), encoding='utf-8')
    (root/'events.jsonl').write_text(''.join(json.dumps({'task_id': item['task_id'], **step}, ensure_ascii=False)+'\n' for item in evaluated for step in item['run']['steps']), encoding='utf-8')
    with (root/'summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['task_id', 'passed', 'failure_category', 'elapsed_seconds', 'model_calls', 'tool_calls', 'input_tokens', 'output_tokens', 'estimated_cost_usd'])
        for item in evaluated:
            writer.writerow([item[key] for key in ['task_id', 'passed', 'failure_category', 'elapsed_seconds', 'model_calls', 'tool_calls', 'input_tokens', 'output_tokens', 'estimated_cost_usd']])
    lines = ['# 只读Agent开发集评测', '', f"模式：{summary['mode']}；通过 {summary['passed']}/{summary['evaluated']}；未运行 {summary['not_run']}。", '',
             '**scripted模式仅验证工程循环和评价器，不能报告为真实模型效果。数据与标准尚未独立复核。**', '',
             '|任务|通过|失败类别|决策次数|工具次数|耗时秒|', '|---|---|---|---:|---:|---:|']
    lines += [f"|{item['task_id']}|{item['passed']}|{item['failure_category'] or '—'}|{item['model_calls']}|{item['tool_calls']}|{item['elapsed_seconds']:.3f}|" for item in evaluated]
    (root/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return summary


if __name__ == '__main__':
    import argparse
    from scripts.manage import load_env
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    try:
        summary = run_agent_evaluation(args.run_dir, args.real)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(int(summary['passed'] != summary['evaluated']))
