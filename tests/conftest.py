from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.db import make_engine, session_factory
from backend.app.main import create_app
from backend.app.models import Base
from backend.app.seed import REFERENCE, seed


@pytest.fixture
def lab(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'llm4web_test.db').as_posix()}", reference_time=REFERENCE.isoformat(), model_adapter='rules', model_enabled=False)
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    with factory.begin() as db:
        seed(db, fixture=True)
    app = create_app(settings, engine)
    with TestClient(app, headers={"X-LLM4Web-Client": "web"}) as client:
        assert client.post('/api/auth/demo', json={"actor": "operator_a"}).status_code == 200
        yield client, factory, settings, engine
    engine.dispose()


def query(client, filters=None, **extra):
    version = client.get('/api/session').json()['context_version']
    response = client.post('/api/orders/query', json={"filters": filters or {}, "expected_context_version": version, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def make_preview(client, result, **extra):
    version = client.get('/api/session').json()['context_version']
    return client.post('/api/previews', json={"result_set_id": result['result_set_id'], "patch": {"priority": True}, "expected_context_version": version, **extra})


def confirm_apply(client, preview, key='test-operation-key'):
    assert client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"digest": preview['digest']}).status_code == 200
    return client.post(f"/api/previews/{preview['preview_id']}/apply", json={"idempotency_key": key})
