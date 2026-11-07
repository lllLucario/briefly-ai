"""基线：W4-10 时的全部业务表。

SQLite 旧库也跑这个脚本：建表和建索引都带 IF NOT EXISTS，只补缺的，不动已有数据。
PostgreSQL 额外建 pgvector 扩展和派生索引表（knowledge/pg_tables.py）。

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SQLITE_DEPTH_GUARD = """
CREATE TRIGGER IF NOT EXISTS todos_two_levels_{op} BEFORE {OP} ON todos
WHEN NEW.parent_id IS NOT NULL AND (
    (SELECT parent_id FROM todos WHERE id = NEW.parent_id) IS NOT NULL
    OR EXISTS (SELECT 1 FROM todos WHERE parent_id = NEW.id)
)
BEGIN SELECT RAISE(ABORT, 'todos: only two levels allowed'); END
"""

_POSTGRES_DEPTH_GUARD = """
CREATE FUNCTION todos_two_levels() RETURNS trigger AS $$
BEGIN
    IF NEW.parent_id IS NOT NULL AND (
        (SELECT parent_id FROM todos WHERE id = NEW.parent_id) IS NOT NULL
        OR EXISTS (SELECT 1 FROM todos WHERE parent_id = NEW.id)
    ) THEN
        RAISE EXCEPTION 'todos: only two levels allowed' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('cache_stats',
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('hits', sa.Integer(), nullable=False),
    sa.Column('misses', sa.Integer(), nullable=False),
    sa.Column('stores', sa.Integer(), nullable=False),
    sa.Column('rejected', sa.Integer(), nullable=False),
    sa.Column('refreshed', sa.Integer(), nullable=False),
    sa.Column('saved_input_tokens', sa.Integer(), nullable=False),
    sa.Column('saved_output_tokens', sa.Integer(), nullable=False),
    sa.Column('saved_ms', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('name')
    , if_not_exists=True)
    op.create_table('chat_sessions',
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('thread_id')
    , if_not_exists=True)
    op.create_table('chunk_extractions',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('items', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    , if_not_exists=True)
    op.create_table('context_usage',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('task_type', sa.String(length=20), nullable=False),
    sa.Column('context_limit', sa.Integer(), nullable=False),
    sa.Column('estimated_tokens', sa.Integer(), nullable=False),
    sa.Column('budgeted_tokens', sa.Integer(), nullable=False),
    sa.Column('system_tokens', sa.Integer(), nullable=False),
    sa.Column('tool_schema_tokens', sa.Integer(), nullable=False),
    sa.Column('memory_tokens', sa.Integer(), nullable=False),
    sa.Column('history_tokens', sa.Integer(), nullable=False),
    sa.Column('material_tokens', sa.Integer(), nullable=False),
    sa.Column('tool_result_tokens', sa.Integer(), nullable=False),
    sa.Column('current_tokens', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('tool_call_count', sa.Integer(), nullable=False),
    sa.Column('history_turns', sa.Integer(), nullable=False),
    sa.Column('omitted_turns', sa.Integer(), nullable=False),
    sa.Column('stubbed_results', sa.Integer(), nullable=False),
    sa.Column('compaction_triggered', sa.Boolean(), nullable=False),
    sa.Column('context_overflow', sa.Boolean(), nullable=False),
    sa.Column('material_hashes', sa.JSON(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_context_usage_run_id'), 'context_usage', ['run_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_context_usage_thread_id'), 'context_usage', ['thread_id'], unique=False, if_not_exists=True)
    op.create_table('feishu_exports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.String(length=64), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('shared_with', sa.String(length=200), nullable=False),
    sa.Column('share_error', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_feishu_exports_report_id'), 'feishu_exports', ['report_id'], unique=False, if_not_exists=True)
    op.create_table('intent_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('message_id', sa.String(length=64), nullable=False),
    sa.Column('layer', sa.Integer(), nullable=False),
    sa.Column('intent', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('gap', sa.Float(), nullable=True),
    sa.Column('kb_score', sa.Float(), nullable=True),
    sa.Column('semantic', sa.JSON(), nullable=True),
    sa.Column('resolved', sa.String(length=32), nullable=True),
    sa.Column('skill', sa.String(length=32), nullable=True),
    sa.Column('state', sa.JSON(), nullable=False),
    sa.Column('skip_funnel', sa.Boolean(), nullable=False),
    sa.Column('corrects', sa.String(length=64), nullable=True),
    sa.Column('corrected', sa.Boolean(), nullable=False),
    sa.Column('text_length', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('matched', sa.String(length=200), nullable=True),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_intent_log_message_id'), 'intent_log', ['message_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_intent_log_thread_id'), 'intent_log', ['thread_id'], unique=False, if_not_exists=True)
    op.create_table('knowledge_base_removals',
    sa.Column('kb_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('removed_at', sa.DateTime(), nullable=False),
    sa.Column('generation_ids', sa.JSON(), nullable=False),
    sa.Column('job_ids', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('kb_id')
    , if_not_exists=True)
    op.create_table('knowledge_bases',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_table('llm_cache_entries',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('site', sa.String(length=40), nullable=False),
    sa.Column('principal_id', sa.String(length=100), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    , if_not_exists=True)
    op.create_index(op.f('ix_llm_cache_entries_expires_at'), 'llm_cache_entries', ['expires_at'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_llm_cache_entries_last_used_at'), 'llm_cache_entries', ['last_used_at'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_llm_cache_entries_principal_id'), 'llm_cache_entries', ['principal_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_llm_cache_entries_site'), 'llm_cache_entries', ['site'], unique=False, if_not_exists=True)
    op.create_table('mail_sends',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('to', sa.Text(), nullable=False),
    sa.Column('cc', sa.Text(), nullable=False),
    sa.Column('subject', sa.String(length=300), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('message_id', sa.String(length=300), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_mail_sends_report_id'), 'mail_sends', ['report_id'], unique=False, if_not_exists=True)
    op.create_table('materials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('file_type', sa.String(length=10), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('parse_report', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_hash')
    , if_not_exists=True)
    op.create_table('outbound_claims',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('record_id', sa.Integer(), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    , if_not_exists=True)
    op.create_index(op.f('ix_outbound_claims_report_id'), 'outbound_claims', ['report_id'], unique=False, if_not_exists=True)
    op.create_table('scan_locks',
    sa.Column('name', sa.String(length=20), nullable=False),
    sa.Column('owner', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('name')
    , if_not_exists=True)
    op.create_table('scan_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.String(length=12), nullable=False),
    sa.Column('scheduled_for', sa.Date(), nullable=True),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('stats', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_table('session_memory',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('compacted_message_id', sa.String(length=100), nullable=False),
    sa.Column('compacted_count', sa.Integer(), nullable=False),
    sa.Column('changes', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('thread_id', 'version')
    , if_not_exists=True)
    op.create_index(op.f('ix_session_memory_thread_id'), 'session_memory', ['thread_id'], unique=False, if_not_exists=True)
    op.create_table('todo_identities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sqlite_autoincrement=True
    , if_not_exists=True)
    op.create_table('todo_import_drafts',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('thread_id', sa.String(length=32), nullable=False),
    sa.Column('revision', sa.String(length=64), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('receipt', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_todo_import_drafts_thread_id'), 'todo_import_drafts', ['thread_id'], unique=False, if_not_exists=True)
    op.create_table('todos',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint('parent_id IS NULL OR parent_id <> id', name='todo_not_own_parent'),
    sa.ForeignKeyConstraint(['parent_id'], ['todos.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_todos_parent_id'), 'todos', ['parent_id'], unique=False, if_not_exists=True)
    op.create_table('tool_results',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('tool_name', sa.String(length=100), nullable=False),
    sa.Column('tool_call_id', sa.String(length=100), nullable=False),
    sa.Column('args', sa.JSON(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tokens', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_tool_results_thread_id'), 'tool_results', ['thread_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_tool_results_tool_call_id'), 'tool_results', ['tool_call_id'], unique=False, if_not_exists=True)
    op.create_table('tracked_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('path_key', sa.Text(), nullable=False),
    sa.Column('path_history', sa.JSON(), nullable=False),
    sa.Column('file_type', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('size', sa.Integer(), nullable=True),
    sa.Column('mtime', sa.Float(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=False),
    sa.Column('moved_from_id', sa.Integer(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=True),
    sa.Column('missing_since', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['moved_from_id'], ['tracked_files.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('path_key')
    , if_not_exists=True)
    op.create_table('tracking_exclusions',
    sa.Column('path_key', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('path_key')
    , if_not_exists=True)
    op.create_table('tracking_sources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('recursive', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_table('weekly_input_receipts',
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('request_id')
    , if_not_exists=True)
    op.create_table('weekly_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('schedule_enabled', sa.Boolean(), nullable=False),
    sa.Column('weekday', sa.Integer(), nullable=False),
    sa.Column('time', sa.String(length=5), nullable=False),
    sa.Column('schedule_updated_at', sa.DateTime(), nullable=False),
    sa.Column('audience', sa.String(length=100), nullable=False),
    sa.Column('author', sa.String(length=100), nullable=False),
    sa.Column('owner_names', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_table('feishu_export_progress',
    sa.Column('export_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['export_id'], ['feishu_exports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('export_id')
    , if_not_exists=True)
    op.create_table('feishu_folder_members',
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('file_key', sa.String(length=200), nullable=False),
    sa.Column('rel_path', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['tracking_sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('source_id', 'file_key')
    , if_not_exists=True)
    op.create_table('feishu_folders',
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('docs', sa.Integer(), nullable=False),
    sa.Column('skipped', sa.Integer(), nullable=False),
    sa.Column('listed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['source_id'], ['tracking_sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('source_id')
    , if_not_exists=True)
    op.create_table('file_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('file_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('mtime', sa.Float(), nullable=False),
    sa.Column('captured_at', sa.DateTime(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=True),
    sa.Column('parse_error', sa.Text(), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('scan_run_id', sa.Integer(), nullable=True),
    sa.Column('gap_before', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['tracked_files.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['scan_run_id'], ['scan_runs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('file_id', 'version')
    , if_not_exists=True)
    op.create_index(op.f('ix_file_snapshots_file_id'), 'file_snapshots', ['file_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_file_snapshots_sha256'), 'file_snapshots', ['sha256'], unique=False, if_not_exists=True)
    op.create_table('knowledge_documents',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kb_id', sa.String(length=32), nullable=False),
    sa.Column('source_key', sa.String(length=200), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('kb_id', 'source_key')
    , if_not_exists=True)
    op.create_index(op.f('ix_knowledge_documents_kb_id'), 'knowledge_documents', ['kb_id'], unique=False, if_not_exists=True)
    op.create_table('knowledge_egress_grants',
    sa.Column('kb_id', sa.String(length=32), nullable=False),
    sa.Column('allowed', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ),
    sa.PrimaryKeyConstraint('kb_id')
    , if_not_exists=True)
    op.create_table('knowledge_parse_cache',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('kb_id', sa.String(length=32), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('markdown', sa.Boolean(), nullable=True),
    sa.Column('report_snapshot', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_knowledge_parse_cache_kb_id'), 'knowledge_parse_cache', ['kb_id'], unique=False, if_not_exists=True)
    op.create_table('material_chunks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=False),
    sa.Column('index', sa.Integer(), nullable=False),
    sa.Column('section', sa.Text(), nullable=False),
    sa.Column('start', sa.Integer(), nullable=False),
    sa.Column('end', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_material_chunks_material_id'), 'material_chunks', ['material_id'], unique=False, if_not_exists=True)
    op.create_index('ix_material_chunks_material_index', 'material_chunks', ['material_id', 'index'], unique=False, if_not_exists=True)
    op.create_table('material_sections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=False),
    sa.Column('index', sa.Integer(), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('start', sa.Integer(), nullable=False),
    sa.Column('end', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_material_sections_material_id'), 'material_sections', ['material_id'], unique=False, if_not_exists=True)
    op.create_table('todo_activities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('todo_id', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=20), nullable=False),
    sa.Column('old_value', sa.JSON(), nullable=True),
    sa.Column('new_value', sa.JSON(), nullable=True),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['todo_id'], ['todos.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_todo_activities_created_at'), 'todo_activities', ['created_at'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_todo_activities_todo_id'), 'todo_activities', ['todo_id'], unique=False, if_not_exists=True)
    op.create_table('todo_materials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('todo_id', sa.Integer(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=False),
    sa.Column('relation', sa.String(length=12), nullable=False),
    sa.Column('track_key', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['todo_id'], ['todos.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('todo_id', 'material_id', 'relation')
    , if_not_exists=True)
    op.create_index(op.f('ix_todo_materials_material_id'), 'todo_materials', ['material_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_todo_materials_todo_id'), 'todo_materials', ['todo_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_todo_materials_track_key'), 'todo_materials', ['track_key'], unique=False, if_not_exists=True)
    op.create_table('weekly_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('material_ids', sa.JSON(), nullable=False),
    sa.Column('removed_materials', sa.JSON(), nullable=False),
    sa.Column('checklist', sa.String(length=10), nullable=False),
    sa.Column('prepared_at', sa.DateTime(), nullable=True),
    sa.Column('prep_notes', sa.JSON(), nullable=False),
    sa.Column('final_scan_id', sa.Integer(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('items', sa.JSON(), nullable=False),
    sa.Column('generation', sa.JSON(), nullable=False),
    sa.Column('inputs', sa.JSON(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('generated_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['final_scan_id'], ['scan_runs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('period_start')
    , if_not_exists=True)
    op.create_table('workspace_egress_holds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=True),
    sa.Column('tracked_file_id', sa.Integer(), nullable=True),
    sa.Column('kb_id', sa.String(length=32), nullable=False),
    sa.Column('kb_name', sa.String(length=200), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('document_title', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('(material_id IS NULL) <> (tracked_file_id IS NULL)'),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tracked_file_id'], ['tracked_files.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('material_id', 'document_id'),
    sa.UniqueConstraint('tracked_file_id', 'document_id')
    , if_not_exists=True)
    op.create_index(op.f('ix_workspace_egress_holds_material_id'), 'workspace_egress_holds', ['material_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_workspace_egress_holds_tracked_file_id'), 'workspace_egress_holds', ['tracked_file_id'], unique=False, if_not_exists=True)
    op.create_table('file_changes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('file_id', sa.Integer(), nullable=False),
    sa.Column('scan_run_id', sa.Integer(), nullable=True),
    sa.Column('change_type', sa.String(length=16), nullable=False),
    sa.Column('from_snapshot_id', sa.Integer(), nullable=True),
    sa.Column('to_snapshot_id', sa.Integer(), nullable=True),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('detected_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['tracked_files.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['from_snapshot_id'], ['file_snapshots.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['scan_run_id'], ['scan_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['to_snapshot_id'], ['file_snapshots.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_file_changes_detected_at'), 'file_changes', ['detected_at'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_file_changes_file_id'), 'file_changes', ['file_id'], unique=False, if_not_exists=True)
    op.create_table('knowledge_versions',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('business_version', sa.String(length=100), nullable=True),
    sa.Column('material_id', sa.Integer(), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('source_snapshot', sa.JSON(), nullable=False),
    sa.Column('chunk_config', sa.JSON(), nullable=False),
    sa.Column('chunk_manifest', sa.JSON(), nullable=False),
    sa.Column('chunk_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['knowledge_documents.id'], ),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'content_hash', 'chunk_fingerprint', 'material_id'),
    sa.UniqueConstraint('document_id', 'id'),
    sa.UniqueConstraint('document_id', 'number')
    , if_not_exists=True)
    op.create_index(op.f('ix_knowledge_versions_document_id'), 'knowledge_versions', ['document_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_knowledge_versions_material_id'), 'knowledge_versions', ['material_id'], unique=False, if_not_exists=True)
    op.create_table('notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('read_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['report_id'], ['weekly_reports.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_table('scheduled_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('occurrence', sa.DateTime(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=True),
    sa.Column('late_minutes', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['report_id'], ['weekly_reports.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('occurrence', 'attempt')
    , if_not_exists=True)
    op.create_table('snapshot_diffs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('from_snapshot_id', sa.Integer(), nullable=True),
    sa.Column('to_snapshot_id', sa.Integer(), nullable=False),
    sa.Column('algo', sa.String(length=20), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['from_snapshot_id'], ['file_snapshots.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_snapshot_id'], ['file_snapshots.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('from_snapshot_id', 'to_snapshot_id', 'algo')
    , if_not_exists=True)
    op.create_index(op.f('ix_snapshot_diffs_to_snapshot_id'), 'snapshot_diffs', ['to_snapshot_id'], unique=False, if_not_exists=True)
    op.create_table('weekly_generation_leases',
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(length=32), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['weekly_reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('report_id')
    , if_not_exists=True)
    op.create_table('weekly_report_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('reason', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['weekly_reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_weekly_report_versions_report_id'), 'weekly_report_versions', ['report_id'], unique=False, if_not_exists=True)
    op.create_table('work_evidence',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.Column('source_refs', sa.JSON(), nullable=False),
    sa.Column('attribution', sa.String(length=16), nullable=False),
    sa.Column('attribution_basis', sa.JSON(), nullable=False),
    sa.Column('review_status', sa.String(length=12), nullable=False),
    sa.Column('flags', sa.JSON(), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('user_edited', sa.Boolean(), nullable=False),
    sa.Column('has_update', sa.Boolean(), nullable=False),
    sa.Column('related_ids', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['weekly_reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_work_evidence_fingerprint'), 'work_evidence', ['fingerprint'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_work_evidence_report_id'), 'work_evidence', ['report_id'], unique=False, if_not_exists=True)
    op.create_table('workspace_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('material_id', sa.Integer(), nullable=True),
    sa.Column('tracked_file_id', sa.Integer(), nullable=True),
    sa.Column('kb_id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("kind IN ('explicit','suggested','confirmed','dismissed')"),
    sa.CheckConstraint('(material_id IS NULL) <> (tracked_file_id IS NULL)'),
    sa.ForeignKeyConstraint(['document_id'], ['knowledge_documents.id'], ),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tracked_file_id'], ['tracked_files.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('material_id', 'document_id'),
    sa.UniqueConstraint('tracked_file_id', 'document_id')
    , if_not_exists=True)
    op.create_index(op.f('ix_workspace_links_document_id'), 'workspace_links', ['document_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_workspace_links_kb_id'), 'workspace_links', ['kb_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_workspace_links_material_id'), 'workspace_links', ['material_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_workspace_links_tracked_file_id'), 'workspace_links', ['tracked_file_id'], unique=False, if_not_exists=True)
    op.create_table('evidence_reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('evidence_id', sa.Integer(), nullable=True),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(length=20), nullable=False),
    sa.Column('before', sa.JSON(), nullable=False),
    sa.Column('after', sa.JSON(), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['evidence_id'], ['work_evidence.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['report_id'], ['weekly_reports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    , if_not_exists=True)
    op.create_index(op.f('ix_evidence_reviews_evidence_id'), 'evidence_reviews', ['evidence_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_evidence_reviews_report_id'), 'evidence_reviews', ['report_id'], unique=False, if_not_exists=True)
    op.create_table('evidence_todo_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('evidence_id', sa.Integer(), nullable=False),
    sa.Column('todo_id', sa.Integer(), nullable=False),
    sa.Column('relation', sa.String(length=10), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('report_status', sa.String(length=10), nullable=True),
    sa.Column('synced_at', sa.DateTime(), nullable=True),
    sa.Column('reviewed_snapshot', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['evidence_id'], ['work_evidence.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['todo_id'], ['todos.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('evidence_id', 'todo_id')
    , if_not_exists=True)
    op.create_index(op.f('ix_evidence_todo_links_evidence_id'), 'evidence_todo_links', ['evidence_id'], unique=False, if_not_exists=True)
    op.create_index(op.f('ix_evidence_todo_links_todo_id'), 'evidence_todo_links', ['todo_id'], unique=False, if_not_exists=True)
    op.create_table('knowledge_chunks',
    sa.Column('version_id', sa.String(length=32), nullable=False),
    sa.Column('index', sa.Integer(), nullable=False),
    sa.Column('section', sa.Text(), nullable=False),
    sa.Column('start', sa.Integer(), nullable=False),
    sa.Column('end', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('embedding_text', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['version_id'], ['knowledge_versions.id'], ),
    sa.PrimaryKeyConstraint('version_id', 'index')
    , if_not_exists=True)
    op.create_table('knowledge_index_generations',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('version_id', sa.String(length=32), nullable=False),
    sa.Column('embedding_identity', sa.JSON(), nullable=False),
    sa.Column('dimension', sa.Integer(), nullable=False),
    sa.Column('chunk_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error_code', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('planned','building','ready','failed')"),
    sa.CheckConstraint('dimension > 0 AND dimension <= 4096'),
    sa.ForeignKeyConstraint(['version_id'], ['knowledge_versions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('version_id', 'id')
    , if_not_exists=True)
    op.create_index(op.f('ix_knowledge_index_generations_version_id'), 'knowledge_index_generations', ['version_id'], unique=False, if_not_exists=True)
    op.create_table('knowledge_imports',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kb_id', sa.String(length=32), nullable=False),
    sa.Column('request_key', sa.String(length=200), nullable=False),
    sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('filename', sa.String(length=200), nullable=False),
    sa.Column('source_key', sa.String(length=200), nullable=False),
    sa.Column('expected_revision', sa.Integer(), nullable=False),
    sa.Column('business_version', sa.String(length=100), nullable=True),
    sa.Column('tokens', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error_code', sa.String(length=80), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('material_id', sa.Integer(), nullable=True),
    sa.Column('version_id', sa.String(length=32), nullable=True),
    sa.Column('generation_id', sa.String(length=32), nullable=True),
    sa.Column('preview', sa.JSON(), nullable=True),
    sa.Column('lease_token', sa.String(length=32), nullable=True),
    sa.Column('lease_until', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('queued','parsing','preview','indexing','ready','failed')"),
    sa.ForeignKeyConstraint(['generation_id'], ['knowledge_index_generations.id'], ),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['version_id'], ['knowledge_versions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('kb_id', 'request_key')
    , if_not_exists=True)
    op.create_index(op.f('ix_knowledge_imports_kb_id'), 'knowledge_imports', ['kb_id'], unique=False, if_not_exists=True)
    op.create_table('knowledge_publications',
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('version_id', sa.String(length=32), nullable=False),
    sa.Column('generation_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['document_id', 'version_id'], ['knowledge_versions.document_id', 'knowledge_versions.id'], ),
    sa.ForeignKeyConstraint(['document_id'], ['knowledge_documents.id'], ),
    sa.ForeignKeyConstraint(['version_id', 'generation_id'], ['knowledge_index_generations.version_id', 'knowledge_index_generations.id'], ),
    sa.PrimaryKeyConstraint('document_id')
    , if_not_exists=True)
    op.create_table('knowledge_import_metrics',
    sa.Column('import_id', sa.String(length=32), nullable=False),
    sa.Column('values', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['import_id'], ['knowledge_imports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('import_id')
    , if_not_exists=True)
    if dialect == "sqlite":
        for name in ("insert", "update"):
            op.execute(_SQLITE_DEPTH_GUARD.format(op=name, OP=name.upper()))
    if dialect == "postgresql":
        from pgvector.sqlalchemy import Vector
        from sqlalchemy.dialects import postgresql

        op.execute(_POSTGRES_DEPTH_GUARD)
        op.execute("CREATE TRIGGER todos_two_levels BEFORE INSERT OR UPDATE ON todos FOR EACH ROW EXECUTE FUNCTION todos_two_levels()")
        op.create_table('kb_index_chunks',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('generation', sa.Text(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', Vector(), nullable=False),
        sa.Column('tokens', postgresql.TSVECTOR(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('generation', 'number', name='uq_kb_index_chunks_generation_number')
        , if_not_exists=True)
        op.create_index('ix_kb_index_chunks_tokens', 'kb_index_chunks', ['tokens'], unique=False, postgresql_using='gin', if_not_exists=True)
        op.create_table('kb_index_generations',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('signature', sa.Text(), nullable=False),
        sa.Column('manifest', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        , if_not_exists=True)
        op.create_table('ws_index_entries',
        sa.Column('material_id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('content_hash', sa.Text(), nullable=False),
        sa.Column('signature', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('material_id')
        , if_not_exists=True)
        op.create_table('ws_index_slices',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('material_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('section', sa.Text(), nullable=False),
        sa.Column('start_pos', sa.Integer(), nullable=False),
        sa.Column('end_pos', sa.Integer(), nullable=False),
        sa.Column('content_sha', sa.String(length=64), nullable=False),
        sa.Column('text_sha', sa.String(length=64), nullable=False),
        sa.Column('embedding_text', sa.Text(), nullable=False),
        sa.Column('tokens', postgresql.TSVECTOR(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('material_id', 'number', name='uq_ws_index_slices_material_number')
        , if_not_exists=True)
        op.create_index('ix_ws_index_slices_text_sha', 'ws_index_slices', ['text_sha'], unique=False, if_not_exists=True)
        op.create_index('ix_ws_index_slices_tokens', 'ws_index_slices', ['tokens'], unique=False, postgresql_using='gin', if_not_exists=True)
        op.create_table('ws_index_vectors',
        sa.Column('signature', sa.Text(), nullable=False),
        sa.Column('text_sha', sa.String(length=64), nullable=False),
        sa.Column('embedding', Vector(), nullable=False),
        sa.PrimaryKeyConstraint('signature', 'text_sha')
        , if_not_exists=True)


def downgrade() -> None:
    raise NotImplementedError("基线不支持降级：回到旧版本请恢复备份")
