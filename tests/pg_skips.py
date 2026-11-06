"""PostgreSQL 模式下跳过的用例和原因。键是用例 id 的前缀。

只放确实只针对 SQLite 的用例（查 SQLite 文件、PRAGMA、FTS5 表等），PostgreSQL 上的同类语义由 test_postgres_*.py 覆盖。
"""
SKIPS: dict[str, str] = {
    "tests/test_db_connections.py::test_create_app_on_memory_database_is_refused": "检查 SQLite 内存库的共用连接，PostgreSQL 没有内存库",
    "tests/test_schema_migrations.py::test_new_database": "SQLite 文件的结构迁移，PostgreSQL 的在 test_postgres_backend.py",
    "tests/test_schema_migrations.py::test_baseline_tables": "比较 SQLite 迁移和内存库 create_all",
    "tests/test_schema_migrations.py::test_old_database": "SQLite 旧库打版本",
    "tests/test_schema_migrations.py::test_second_start": "SQLite 文件的快路径，PostgreSQL 的在 test_postgres_backend.py",
    "tests/test_knowledge.py::test_old_database_upgrade_is_additive_and_idempotent": "手工删表模拟 SQLite 旧库；PostgreSQL 没有旧库，数据由迁移命令导入",
    "tests/test_llm_cache.py::test_parse_cache_stat_reads_import_metrics_and_skips_forced": "靠 PRAGMA 关闭外键造数据；统计逻辑本身在其他后端无关用例里",
    "tests/test_logic_fixes.py::test_percent_encoded_url_does_not_create_stray_directories": "SQLite 文件路径的编码问题",
    "tests/test_material_delete.py::test_database_rejects_dangling_references": "查 PRAGMA foreign_keys；PostgreSQL 的外键一直生效",
    "tests/test_material_knowledge_isolation.py::test_old_database_migration_marks_only_import_created_materials": "SQLite 旧库启动时的补丁；PostgreSQL 在迁移命令导入后跑一次",
    "tests/test_material_reads.py::test_existing_database_gets_index_without_rechunking": "SQLite 旧库启动时补索引",
}
