"""Synthetic browser-test server. Explicit scripted decisions, never a model demo."""
from pathlib import Path
import time

from sqlalchemy.engine import make_url

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine, session_factory
from backend.app.main import create_app
from backend.app.models import Base
from evaluation.branch_fixture import load_fixture
from evaluation.scripted_decisions import ScriptedDecisions
from evaluation.tool_contracts import cases, oracles
from evaluation.agent_runner import reference_decisions


class BrowserFixtureAdapter:
    name = 'scripted-browser-test-only'
    billable = False

    def decide(self, goal, reference, state, timeout_seconds=None):
        task = next(task for task in cases() if task['instruction'] == goal)
        script = reference_decisions(task, oracles()[task['task_id']])
        # Explicit, prewritten decision sequence; no conditional branch policy.
        index = len(state['observations'])
        time.sleep(0.15)
        return ScriptedDecisions([script[index]]).decide(goal, reference, state, timeout_seconds)


settings = Settings(model_adapter='rules', model_enabled=False)
url = make_url(settings.database_url)
target = Path(url.database or '').resolve()
if url.drivername != 'sqlite' or target.parent != (ROOT/'runtime').resolve() or target.name != 'llm4web_fixture_agent_browser.db':
    raise RuntimeError('Browser fixture refuses any database other than its dedicated runtime fixture')
engine = make_engine(settings.database_url)
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with session_factory(engine).begin() as db:
    load_fixture(db)
app = create_app(settings, engine, BrowserFixtureAdapter())
