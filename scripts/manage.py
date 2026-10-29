"""Windows-compatible entry points: python -m scripts.manage <command>."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    os.chdir(ROOT)
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['db-migrate', 'db-seed', 'dev', 'test', 'test-smoke', 'eval-run', 'eval-report', 'agent-eval', 'fixture-reset'])
    parser.add_argument('--database')
    parser.add_argument('--fixture', action='store_true')
    parser.add_argument('--confirm-fixture', action='store_true')
    parser.add_argument('--run-dir')
    parser.add_argument('--real', action='store_true')
    args = parser.parse_args()
    if args.database:
        os.environ['DATABASE_URL'] = args.database
    if args.command in {'test','test-smoke','eval-run','eval-report'} or (args.command == 'agent-eval' and not args.real):
        os.environ['MODEL_ADAPTER'] = 'rules'
        os.environ['MODEL_CALLS_ENABLED'] = 'false'
    from backend.app.config import Settings
    from backend.app.db import make_engine, session_factory
    from backend.app.seed import seed
    settings = Settings()
    if args.command == 'db-migrate':
        run(sys.executable, '-m', 'alembic', 'upgrade', 'head')
    elif args.command == 'db-seed':
        engine = make_engine(settings.database_url)
        with session_factory(engine).begin() as db:
            print(json.dumps({'orders': seed(db, fixture=args.fixture), 'fixture': args.fixture}))
        engine.dispose()
    elif args.command == 'fixture-reset':
        from sqlalchemy.engine import make_url
        url = make_url(settings.database_url)
        if not args.confirm_fixture or url.drivername != 'sqlite' or not url.database:
            parser.error('仅允许显式确认并重置 runtime/ 下名称为 llm4web_fixture_*.db 的 SQLite 实验库')
        target = Path(url.database).resolve()
        allowed = (ROOT / 'runtime').resolve()
        if target.parent != allowed or not target.name.startswith('llm4web_fixture_') or target.suffix != '.db':
            parser.error('目标路径不是受保护的 fixture 数据库')
        from backend.app.models import Base
        from sqlalchemy import text
        engine = make_engine(settings.database_url)
        with engine.begin() as connection:
            Base.metadata.drop_all(connection)
            connection.execute(text('DROP TABLE IF EXISTS alembic_version'))
        engine.dispose()
        run(sys.executable,'-m','alembic','upgrade','head')
        engine = make_engine(settings.database_url)
        with session_factory(engine).begin() as db:
            print(json.dumps({'orders': seed(db, fixture=True), 'database': str(target)}))
        engine.dispose()
    elif args.command == 'dev':
        run(sys.executable, '-m', 'uvicorn', 'backend.app.main:app', '--host', '127.0.0.1', '--port', '8000')
    elif args.command == 'test':
        run(sys.executable, '-m', 'pytest', '--basetemp=runtime/pytest')
    elif args.command in {'test-smoke', 'eval-run'}:
        from evaluation.runner import run_smoke
        report = run_smoke(Path(args.run_dir) if args.run_dir else None)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report['passed'] != report['total']:
            sys.exit(1)
    elif args.command == 'eval-report':
        if not args.run_dir:
            parser.error('eval-report 需要 --run-dir')
        from evaluation.runner import write_report
        print(json.dumps(write_report(Path(args.run_dir)), ensure_ascii=False, indent=2))
    elif args.command == 'agent-eval':
        from evaluation.runner import run_agent_eval
        try:
            summary = run_agent_eval(Path(args.run_dir) if args.run_dir else None, real=args.real)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary['passed'] != summary['evaluated']:
            sys.exit(1)


if __name__ == '__main__':
    main()
