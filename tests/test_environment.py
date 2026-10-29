import os
import subprocess
import sys
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from backend.app.config import ROOT
from backend.app.db import make_engine, session_factory
from backend.app.models import Base, Order
from backend.app.seed import seed
from backend.app.service import row


def test_actual_migration_matches_models_and_fixture_reproduces(tmp_path):
    snapshots = []
    for index in range(2):
        url = f"sqlite:///{(tmp_path / f'llm4web_test_{index}.db').as_posix()}"
        result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT,
                                env={**os.environ, 'DATABASE_URL':url}, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        engine = make_engine(url)
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        factory = session_factory(engine)
        with factory.begin() as db:
            seed(db,fixture=True)
        with factory() as db:
            snapshots.append([row(o) for o in db.scalars(select(Order).order_by(Order.id))])
        engine.dispose()
    assert snapshots[0] == snapshots[1]


def test_fixture_reset_refuses_demo_and_outside_workspace(tmp_path):
    for target in ['sqlite:///./runtime/llm4web_demo.db', f"sqlite:///{(tmp_path/'llm4web_fixture_outside.db').as_posix()}",
                   'postgresql+psycopg://local:synthetic@localhost/llm4web_demo']:
        result = subprocess.run([sys.executable, '-m', 'scripts.manage', 'fixture-reset', '--database',target,'--confirm-fixture'],
                                cwd=ROOT, capture_output=True, text=True)
        assert result.returncode != 0
        assert 'error:' in result.stderr
    assert not (tmp_path/'llm4web_fixture_outside.db').exists()


def test_incremental_migration_preserves_existing_business_rows(tmp_path):
    url = f"sqlite:///{(tmp_path/'llm4web_upgrade.db').as_posix()}"
    environment = {**os.environ,'DATABASE_URL':url}
    subprocess.run([sys.executable,'-m','alembic','upgrade','0001'],cwd=ROOT,env=environment,check=True,capture_output=True)
    engine = make_engine(url)
    factory = session_factory(engine)
    with factory.begin() as db:
        seed(db,fixture=True)
    with factory() as db:
        before = [row(o) for o in db.scalars(select(Order).order_by(Order.id))]
    subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=ROOT,env=environment,check=True,capture_output=True)
    with factory() as db:
        assert [row(o) for o in db.scalars(select(Order).order_by(Order.id))] == before
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection),Base.metadata) == []
    engine.dispose()


def test_fixture_reset_builds_through_real_migrations():
    import sqlite3
    from contextlib import closing
    target = ROOT/'runtime'/f'llm4web_fixture_reset_{uuid4().hex}.db'
    try:
        subprocess.run([sys.executable,'-m','scripts.manage','fixture-reset','--database',f'sqlite:///{target.as_posix()}',
                        '--confirm-fixture'],cwd=ROOT,check=True,capture_output=True)
        with closing(sqlite3.connect(target)) as db:
            assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == ScriptDirectory.from_config(Config(str(ROOT/'alembic.ini'))).get_current_head()
            assert db.execute('SELECT count(*) FROM orders').fetchone()[0] == 16
    finally:
        assert target.parent == ROOT/'runtime' and target.name.startswith('llm4web_fixture_reset_')
        target.unlink(missing_ok=True)
