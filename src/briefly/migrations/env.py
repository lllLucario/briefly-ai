"""Alembic 环境。启动时由 briefly.schema 传入连接；命令行（见 alembic.ini）按 DATABASE_URL 连接。"""
from alembic import context
from sqlalchemy import create_engine

from briefly import config as app_config
from briefly.db import database_url
from briefly.schema import include_object, target_metadata

alembic_config = context.config


def run(connection) -> None:
    dialect = connection.dialect.name
    context.configure(connection=connection, target_metadata=target_metadata(dialect), include_object=include_object,
                      render_as_batch=dialect == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if (given := alembic_config.attributes.get("connection")) is not None:
    run(given)
else:
    with create_engine(database_url(app_config.DATABASE_URL)).begin() as opened:
        run(opened)
