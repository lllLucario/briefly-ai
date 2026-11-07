"""SQLite 上的结构迁移：新库、旧库补齐并打版本、模型和迁移没有差异。PostgreSQL 的差异检查在 test_postgres_backend.py。"""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from briefly import schema
from briefly.db import Base, create_db_engine, database_url, is_postgres, register_models


def test_new_database_is_at_head_and_matches_models(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    with engine.connect() as connection:
        assert schema.current(connection) == schema.head()
        context = MigrationContext.configure(connection, opts=dict(compare_type=True, include_object=schema.include_object))
        assert compare_metadata(context, schema.target_metadata("sqlite")) == []
    engine.dispose()


def test_baseline_tables_match_create_all(tmp_path):
    """基线脚本建出来的表和列，与内存库 create_all 的结果一致（内存库不走迁移）。"""
    migrated = create_db_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    memory = create_db_engine("sqlite://")
    left, right = inspect(migrated), inspect(memory)
    assert set(left.get_table_names()) - {"alembic_version"} == set(right.get_table_names())
    for name in right.get_table_names():
        assert {c["name"] for c in left.get_columns(name)} == {c["name"] for c in right.get_columns(name)}, name
    migrated.dispose()
    memory.dispose()


def test_old_database_keeps_data_and_gets_stamped(tmp_path):
    """W4 之前的库没有版本表、可能缺新表：补齐缺的，已有数据和表结构不动。"""
    path = tmp_path / "old.db"
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    engine.dispose()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE alembic_version")
    db.execute("DROP TABLE outbound_claims")
    db.execute("DROP INDEX ix_material_chunks_material_index")
    db.execute("INSERT INTO chat_sessions(thread_id,title,created_at,updated_at) VALUES ('t','旧会话','2026-01-01 00:00:00','2026-01-01 00:00:00')")
    db.commit()
    db.close()

    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    with engine.connect() as connection:
        assert schema.current(connection) == schema.head()
        assert connection.execute(text("SELECT title FROM chat_sessions")).scalar() == "旧会话"
        assert "outbound_claims" in inspect(connection).get_table_names()
        assert "ix_material_chunks_material_index" in {i["name"] for i in inspect(connection).get_indexes("material_chunks")}
    engine.dispose()


_WORKER = """
import sys, time
from pathlib import Path
from briefly.db import create_db_engine
barrier, me, total = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
(barrier / me).touch()
deadline = time.monotonic() + 120
while len(list(barrier.iterdir())) < total and time.monotonic() < deadline:  # 都导入完了才一起开始
    time.sleep(0.005)
create_db_engine(sys.argv[1]).dispose()
if sys.argv[1].startswith("postgresql"):  # 正式启动的顺序：业务库，然后会话记录
    from briefly.agent import postgres_checkpointer
    postgres_checkpointer(sys.argv[1]).conn.close()
"""


def start_together(url: str, tmp_path: Path, processes: int = 4):
    """真正的多个进程同时启动。全部成功才返回。"""
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONUTF8": "1"}
    workers = [subprocess.Popen([sys.executable, "-c", _WORKER, url, str(barrier), str(i), str(processes)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for i in range(processes)]
    errors = []
    for worker in workers:
        _, err = worker.communicate(timeout=240)
        if worker.returncode:
            errors.append(err[-800:])
    assert not errors, errors[0]


def _new_file(path: Path) -> None:
    """新建的库走同一条路：这里什么都不用准备。"""


def _old_file(path: Path) -> None:
    """W4 之前的库：有数据，没有版本表，缺一些新表。"""
    create_db_engine(f"sqlite:///{path.as_posix()}").dispose()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE alembic_version")
    db.execute("DROP TABLE outbound_claims")
    db.execute("INSERT INTO chat_sessions(thread_id,title,created_at,updated_at) VALUES ('t','旧会话','2026-01-01 00:00:00','2026-01-01 00:00:00')")
    db.commit()
    db.close()


@pytest.mark.parametrize("prepare", [_new_file, _old_file], ids=["new", "old"])
def test_concurrent_first_start_all_succeed(tmp_path, prepare):
    """以前只有 PostgreSQL 排队：多个进程都读到“没有版本”，写 alembic_version 时撞主键，后到的启动失败。"""
    path = tmp_path / "app.db"
    prepare(path)
    start_together(f"sqlite:///{path.as_posix()}", tmp_path)
    db = sqlite3.connect(path)
    assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [(schema.head(),)]
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='outbound_claims'").fetchone() == (1,)
    if prepare is _old_file:
        assert db.execute("SELECT title FROM chat_sessions").fetchall() == [("旧会话",)]
    db.close()


def test_second_start_takes_the_fast_path(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'fast.db').as_posix()}"
    create_db_engine(url).dispose()

    def fail(*_args, **_kwargs):
        raise AssertionError("版本相同时不应再跑迁移")

    from alembic import command

    monkeypatch.setattr(command, "upgrade", fail)
    create_db_engine(url).dispose()


def test_migration_scripts_are_listed_as_package_data():
    root = Path(schema.__file__).parent
    assert (root / "migrations" / "env.py").exists() and (root / "migrations" / "script.py.mako").exists()
    assert list((root / "migrations" / "versions").glob("*.py"))
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    for pattern in ("migrations/*.py", "migrations/*.mako", "migrations/versions/*.py"):
        assert pattern in pyproject


def test_postgres_urls_use_psycopg():
    assert database_url("postgresql://u@h/d") == "postgresql+psycopg://u@h/d"
    assert database_url("postgres://u@h/d") == "postgresql+psycopg://u@h/d"
    assert database_url("postgresql+psycopg://u@h/d") == "postgresql+psycopg://u@h/d"
    assert database_url("sqlite:///a.db") == "sqlite:///a.db"
    assert is_postgres("postgresql+psycopg://u@h/d") and is_postgres("postgres://u@h/d") and not is_postgres("sqlite://")


def test_every_table_has_a_primary_key():
    """迁移校验和序列重置都依赖主键。"""
    register_models()
    assert all(list(table.primary_key.columns) for table in Base.metadata.sorted_tables)
