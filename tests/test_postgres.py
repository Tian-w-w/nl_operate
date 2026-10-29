"""Integration tests against an explicitly named disposable PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.app.agent import RulesAdapter
from backend.app.config import ROOT, Settings
from backend.app.db import make_engine, session_factory
from backend.app.main import create_app
from backend.app.models import Base, ModelCall, Operation, Order, Plan, Session
from backend.app.seed import REFERENCE, seed
from conftest import confirm_apply, make_preview, query

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_lab(monkeypatch):
    raw = os.getenv('TEST_DATABASE_URL')
    if not raw:
        pytest.skip('TEST_DATABASE_URL is not configured; PostgreSQL was not tested')
    url = make_url(raw)
    if url.get_backend_name() != 'postgresql' or url.database not in {'llm4web_test','llm4web_ci_test'}:
        pytest.fail('Refusing integration DB: only llm4web_test / llm4web_ci_test allowed')
    schema = 'llm4web_test_' + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = url.update_query_dict({'options':f'-csearch_path={schema}'})
    monkeypatch.setenv('DATABASE_URL',isolated.render_as_string(hide_password=False))
    settings = Settings(database_url=isolated.render_as_string(hide_password=False),reference_time=REFERENCE.isoformat(),model_adapter='rules',model_enabled=False)
    engine = make_engine(settings.database_url)
    try:
        cfg = Config(str(ROOT/'alembic.ini'))
        cfg.set_main_option('script_location',str(ROOT/'backend/migrations'))
        command.upgrade(cfg,'head')
        factory = session_factory(engine)
        with factory.begin() as db:
            seed(db,fixture=True)
        app = create_app(settings,engine)
        with TestClient(app,headers={'X-LLM4Web-Client':'web'}) as client:
            client.post('/api/auth/demo',json={'actor':'operator_a'}).raise_for_status()
            yield client,factory,settings,engine
    finally:
        engine.dispose()
        assert schema.startswith('llm4web_test_') and len(schema) == len('llm4web_test_')+32
        with admin.begin() as connection:
            connection.execute(DropSchema(schema,cascade=True))
        admin.dispose()


def test_postgres_migration_tenant_isolation_and_exact_decimal(pg_lab):
    client, _, _, engine = pg_lab
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection),Base.metadata) == []
    result = query(client,{'shipping_city':'上海','status':'paid','waiting_hours_gt':48})
    assert [r['id'] for r in result['rows']] == [1,3,10,12]
    assert result['total_amount'] == '2300.00'
    assert client.get('/api/orders/13').status_code == 404
    assert confirm_apply(client,make_preview(client,result).json()).status_code == 200


def test_postgres_two_sessions_compete_one_batch_commits(pg_lab):
    client,factory,settings,engine = pg_lab
    first = make_preview(client,query(client,{'status':'paid'})).json()
    client.post(f"/api/previews/{first['preview_id']}/confirm",json={'digest':first['digest']}).raise_for_status()
    with TestClient(create_app(settings,engine),headers={'X-LLM4Web-Client':'web'}) as second:
        second.post('/api/auth/demo',json={'actor':'operator_a'}).raise_for_status()
        another = make_preview(second,query(second,{'status':'paid'})).json()
        second.post(f"/api/previews/{another['preview_id']}/confirm",json={'digest':another['digest']}).raise_for_status()
        def apply(pair):
            actor,candidate = pair
            return actor.post(f"/api/previews/{candidate['preview_id']}/apply",json={'idempotency_key':'postgres-concurrent-key'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(apply,[(client,first),(second,another)]))
    assert sorted(r.status_code for r in responses) == [200,409]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Operation)) == 1
        assert all(o.version == 2 for o in db.scalars(select(Order).where(Order.status == 'paid',Order.tenant_id == 'a')))


def test_postgres_parser_holds_no_session_row_lock(pg_lab):
    client,factory,_,_ = pg_lab
    token = client.cookies.get('llm4web_session')
    class Independent(RulesAdapter):
        def parse(self,instruction,reference,context=None):
            with factory.begin() as db:
                session = db.scalar(select(Session).where(Session.id == token).with_for_update(nowait=True))
                assert db.scalar(select(Plan)).state == 'RECEIVED'
                session.context_version += 1
            return super().parse(instruction,reference,context)
    client.app.state.adapter = Independent()
    reply = client.post('/api/assistant',json={'message':'查询订单','expected_context_version':0}).json()
    assert reply['error'] == 'STALE_CONTEXT'
    with factory() as db:
        assert db.scalar(select(ModelCall)).state == 'FAILED'


def test_postgres_global_budget_is_atomic_across_sessions(pg_lab):
    client,factory,settings,engine = pg_lab
    class Budget(RulesAdapter):
        billable = True
        def estimate_reservation(self,*args):
            return Decimal('.6')
    settings.model_budget_usd = settings.model_project_budget_usd = Decimal('1')
    client.app.state.adapter = Budget()
    with TestClient(create_app(settings,engine,adapter=Budget()),headers={'X-LLM4Web-Client':'web'}) as second:
        second.post('/api/auth/demo',json={'actor':'operator_b'}).raise_for_status()
        def run(actor):
            return actor.post('/api/assistant',json={'message':'查询订单','expected_context_version':0})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run,[client,second]))
    assert sorted(r.status_code for r in results) == [200,429]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 1
