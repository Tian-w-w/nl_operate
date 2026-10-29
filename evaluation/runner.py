import hashlib
import json
import platform
import subprocess
import time
from datetime import datetime, timezone
from importlib.metadata import version
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine, session_factory
from backend.app.main import create_app
from backend.app.models import Base, Customer, Operation, Order, OrderItem, Product, ResultSet, Shipment
from backend.app.seed import FIXTURE_VERSION, REFERENCE, seed
from backend.app.service import row
from evaluation.grader import grade, inspect_csv


def snapshot(factory):
    with factory() as db:
        return {str(o.id): row(o) for o in db.scalars(select(Order).order_by(Order.id))}


def snapshot_related(factory):
    def encode(value):
        return value.isoformat() if hasattr(value,'isoformat') else str(value) if value.__class__.__name__ == 'Decimal' else value
    with factory() as db:
        return {model.__tablename__:[{key:encode(value) for key,value in record.items()}
                for record in db.execute(select(model.__table__).order_by(model.id)).mappings()]
                for model in [Customer,Product,OrderItem,Shipment]}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_agent_eval(run_dir=None, real=False):
    """Separate entry; existing offline smoke behavior is preserved."""
    from evaluation.agent_runner import run_agent_evaluation
    return run_agent_evaluation(run_dir, real)


def write_report(run_dir):
    entries = [json.loads(line) for line in (run_dir / 'tasks.jsonl').read_text(encoding='utf-8').splitlines()]
    categories = {}
    for item in entries:
        bucket = categories.setdefault(item['category'], {'passed': 0, 'total': 0})
        bucket['passed'] += int(item['passed'])
        bucket['total'] += 1
    times = sorted(e['elapsed_seconds'] for e in entries)
    summary = {'method': 'rules-v1-offline', 'scope': 'development smoke only; not model or research results',
               'passed': sum(int(e['passed']) for e in entries), 'total': len(entries), 'categories': categories,
               'latency_p50_seconds': times[len(times)//2] if times else 0,
               'model_calls': 0, 'model_tokens': 0, 'model_cost': 0, 'run_dir': str(run_dir.resolve())}
    write_json(run_dir / 'summary.json', summary)
    lines = ['# 离线规则冒烟验收', '', f"通过 {summary['passed']} / {summary['total']} 条开发任务。", '',
             '**这不是大模型成绩、冻结测试集成绩或论文实验结果。** 参考时钟与数据固定；延迟仅为本地 TestClient 执行耗时。', '',
             '|类别|通过|总数|', '|---|---:|---:|']
    lines += [f"|{k}|{v['passed']}|{v['total']}|" for k,v in categories.items()]
    lines += ['', '## 逐任务检查', ''] + [f"- {'PASS' if e['passed'] else 'FAIL'} {e['task_id']}：" + ', '.join(f"{k}={v}" for k,v in e['checks'].items()) for e in entries]
    (run_dir / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    import csv
    with (run_dir / 'summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['task_id', 'category', 'passed', 'elapsed_seconds', 'model_calls', 'model_tokens', 'model_cost'])
        for item in entries:
            writer.writerow([item['task_id'], item['category'], item['passed'], item['elapsed_seconds'], 0, 0, 0])
    return summary


def run_smoke(run_dir=None):
    task_file = ROOT / 'data/tasks/smoke.dev.jsonl'
    oracle_file = ROOT / 'data/oracles/smoke_v1.json'
    tasks = [json.loads(line) for line in task_file.read_text(encoding='utf-8').splitlines()]
    oracles = json.loads(oracle_file.read_text(encoding='utf-8'))
    run_dir = (run_dir or ROOT / 'experiments/runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:6])).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    artifacts = run_dir / 'artifacts'
    artifacts.mkdir()
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError:
        commit = None
    source_paths = sorted({p for pattern in ['backend/**/*.py', 'evaluation/**/*.py', 'frontend/src/**/*', 'frontend/package-lock.json', 'requirements.lock', 'docs/business_rules.md'] for p in ROOT.glob(pattern) if p.is_file()})
    source_hashes = {p.relative_to(ROOT).as_posix(): file_hash(p) for p in source_paths}
    manifest = {'method': 'rules-v1-offline', 'fixture': FIXTURE_VERSION, 'reference_time': '2026-10-26T12:00:00+08:00',
                'code_commit': commit, 'model': None, 'python': platform.python_version(),
                'task_sha256': file_hash(task_file), 'oracle_sha256': file_hash(oracle_file),
                'adapter_sha256': file_hash(ROOT/'backend/app/agent.py'), 'grader_sha256': file_hash(ROOT/'evaluation/grader.py'),
                'source_files_sha256': source_hashes, 'source_sha256': hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest(),
                'dependencies': {p: version(p) for p in ['fastapi','sqlalchemy','pydantic','httpx']},
                'budget': {'model_calls': 0, 'max_http_calls_per_task': 20}, 'split': 'dev', 'frozen_test': False}
    write_json(run_dir / 'manifest.json', manifest)
    with (run_dir/'tasks.jsonl').open('w',encoding='utf-8') as task_output, (run_dir/'events.jsonl').open('w',encoding='utf-8') as events:
        for task in tasks:
            started = time.perf_counter()
            database = artifacts / f"{task['task_id']}.db"
            settings = Settings(database_url=f"sqlite:///{database.as_posix()}", reference_time=REFERENCE.isoformat(), model_adapter='rules', model_enabled=False)
            engine = make_engine(settings.database_url)
            Base.metadata.create_all(engine)
            factory = session_factory(engine)
            with factory.begin() as db:
                seed(db, fixture=True)
            before = snapshot(factory)
            observed = {'before': before, 'before_related':snapshot_related(factory), 'no_write_before_confirmation': True}
            oracle = oracles[task['task_id']]
            try:
                with TestClient(create_app(settings, engine), headers={'X-LLM4Web-Client': 'web'}) as client:
                    http_calls = 0
                    def call(method, path, body=None):
                        nonlocal http_calls
                        http_calls += 1
                        if http_calls > 20:
                            raise RuntimeError('HTTP call budget exceeded')
                        response = client.request(method, path, json=body) if body is not None else client.request(method, path)
                        events.write(json.dumps({'task_id': task['task_id'], 'method': method, 'path': path, 'status': response.status_code, 'body': body},ensure_ascii=False)+'\n')
                        return response
                    session = call('POST', '/api/auth/demo', {'actor': task['actor_id']}).json()
                    version_number = session['context_version']
                    if task.get('initial_query'):
                        initial = call('POST','/api/orders/query', {**task['initial_query'], 'expected_context_version': version_number}).json()
                        version_number = initial['context_version']
                        if task.get('page'):
                            paged = call('POST',f"/api/results/{initial['result_set_id']}/page", {'page': task['page'], 'expected_context_version': version_number}).json()
                            version_number = paged['context_version']
                    last = None
                    for turn in task['user_turns']:
                        last = call('POST','/api/assistant', {'message':turn, 'expected_context_version':version_number}).json()
                        version_number = last['context_version']
                        if 'preview' in last:
                            observed['preview_ids'] = last['preview']['ids']
                            observed['no_write_before_confirmation'] &= snapshot(factory) == before
                    observed['state'] = last['state']
                    if 'error' in last:
                        observed['error'] = last['error']
                    if 'aggregate' in last:
                        observed['aggregate'] = last['aggregate']['groups']
                    if 'detail' in last:
                        observed['detail_id'] = last['detail']['id']
                    if 'export' in last:
                        content = call('GET', last['export']['download_url']).content
                        observed.update(inspect_csv(content))
                        (artifacts / f"{task['task_id']}.csv").write_bytes(content)
                    if oracle.get('modified_ids'):
                        candidate = last.get('preview')
                        if not candidate or candidate['ids'] != oracle['modified_ids'] or candidate['patch'] != oracle['patch']:
                            observed['confirmation_withheld'] = True
                        else:
                            call('POST', f"/api/previews/{candidate['preview_id']}/confirm", {'digest':candidate['digest']}).raise_for_status()
                            operation = call('POST', f"/api/previews/{candidate['preview_id']}/apply", {'idempotency_key':task['task_id']}).json()
                            observed['state'] = operation.get('state')
                            if oracle.get('retry_once'):
                                repeat = call('POST', f"/api/previews/{candidate['preview_id']}/apply", {'idempotency_key':task['task_id']}).json()
                                observed['retry_same_operation'] = repeat == operation
                    current = call('GET', '/api/session').json()
                    if current.get('current_result'):
                        rs = current['current_result']
                        # Evaluate complete result membership AND visible page/order.
                        with factory() as db:
                            observed['ids'] = db.get(ResultSet, rs['result_set_id']).ids
                        observed['total_amount'] = rs['total_amount']
                        observed['ui_rows_correct'] = [r['id'] for r in rs['rows']] == observed['ids'][(rs['query']['page']-1)*rs['query']['page_size']:rs['query']['page']*rs['query']['page_size']]
            except Exception as error:
                observed['runner_error'] = f'{type(error).__name__}: {error}'
            finally:
                observed['after'] = snapshot(factory)
                observed['after_related'] = snapshot_related(factory)
                with factory() as db:
                    observed['operation_count'] = db.scalar(select(func.count()).select_from(Operation))
                engine.dispose()
            graded = grade(oracle, observed)
            graded['checks']['runner_no_exception'] = 'runner_error' not in observed
            graded['checks']['ui_rows_correct'] = observed.get('ui_rows_correct', 'ids' not in oracle)
            graded['passed'] = all(graded['checks'].values())
            entry = {'task_id':task['task_id'], 'family_id':task['family_id'], 'category':task['category'], **graded,
                     'elapsed_seconds':round(time.perf_counter()-started,4), 'model_calls':0, 'model_tokens':0, 'model_cost':0,
                     'artifact_ref':f"artifacts/{task['task_id']}.json"}
            write_json(artifacts / f"{task['task_id']}.json", observed)
            task_output.write(json.dumps(entry,ensure_ascii=False)+'\n')
    return write_report(run_dir)
