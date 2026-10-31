"""材料管理：粘贴文本或上传文件，存为纯文本；导入时识别章节、分块并入库，检索查入库的块。"""
import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import JSON, DateTime, Engine, ForeignKey, Index, Integer, String, Text, func, or_, select, update
from sqlalchemy.orm import Mapped, defer, mapped_column, relationship, sessionmaker

from . import config
from .chunking import chunk_text, parse_sections, uses_markdown
from .db import Base
from .loaders import file_type, load_document

SNIPPET_RADIUS = 100
TRACKED = "tracked"  # 文件追踪保存的版本：材料列表和检索只用每个文件的最新版本，旧版本只供追踪模块使用
# 知识库入库解析出的正文：只属于知识库（企业受管控参考资料），材料库（个人工作区）的所有入口都不列出、
# 不检索、不读取，内容只经知识库检索读取（才有外发授权、质量限制与引用）。用户另行添加同样内容时转为工作区材料。
KNOWLEDGE = "knowledge"
WORKSPACE_FLAG = "workspace_added"  # 知识库专属材料被用户另行添加过：旧库迁移不再把它标回知识库专属
MAX_SEARCH_HITS = 10


class MaterialNotFound(ValueError):
    """材料不存在。继承 ValueError，工具里仍按普通错误回传给模型。"""


class KnowledgeOnlyMaterial(MaterialNotFound):
    """材料只属于知识库：对材料库的各入口等同不存在，但说明去知识库中提问。"""

    def __init__(self, material_id: int, title: str = "") -> None:
        name = f"《{title}》" if title else ""
        super().__init__(f"材料 {material_id}{name}属于知识库，不在材料库中，请在知识库中提问")


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20))  # paste / file / tracked
    file_type: Mapped[str | None] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # 解析报告：structure（章节来自 font_size / bookmarks / docx_styles / markdown / rules / none）、
    # 清理时删除的内容、警告等，便于核查有没有误删
    parse_report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    sections: Mapped[list["MaterialSection"]] = relationship(cascade="all, delete-orphan")
    chunks: Mapped[list["MaterialChunk"]] = relationship(cascade="all, delete-orphan")


class MaterialSection(Base):
    """章节大纲。start/end 是在材料全文中的偏移。"""

    __tablename__ = "material_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)  # 从 1 开始，按出现顺序
    level: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(Text)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)


class MaterialChunk(Base):
    __tablename__ = "material_chunks"
    __table_args__ = (Index("ix_material_chunks_material_index", "material_id", "index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)  # 从 1 开始
    section: Mapped[str] = mapped_column(Text)  # 块起点所在章节的路径
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)


@dataclass(frozen=True)
class SearchHit:
    material_id: int
    title: str
    chunk: int  # 从 1 开始
    section: str
    hits: int
    snippet: str


def wrap_material(header: str, body: str) -> str:
    """材料内容统一用标记包起来，提醒模型这是待处理的材料而不是指令。"""
    return f"{header}\n【材料开始】\n{body}\n【材料结束】"


def visible():
    """默认可见的材料：用户添加的，加上追踪中文件的最新版本；不含知识库专属材料。"""
    from .tracking.models import latest_versions

    latest = latest_versions().subquery()
    return or_(Material.source.not_in([TRACKED, KNOWLEDGE]), Material.id.in_(select(latest.c.material_id)))


def knowledge_referenced(s, material_ids) -> set[int]:
    """被知识版本、导入任务或解析缓存引用的材料。"""
    from .knowledge.models import KnowledgeImport, KnowledgeParseCache, KnowledgeVersion

    ids = list(material_ids)
    found: set[int] = set()
    for model in (KnowledgeVersion, KnowledgeImport, KnowledgeParseCache):
        found.update(s.scalars(select(model.material_id).where(model.material_id.in_(ids))))
    return found


def mark_knowledge_only(engine: Engine) -> int:
    """旧库迁移：此前知识库入库按 source=file 存材料，与用户上传混在材料库里。

    由导入任务创建（材料不早于引用它的最早导入任务）、且没有作为工作区材料使用过（待办关联、周报本周材料、
    追踪版本、用户另行添加）的，标为知识库专属。早于导入任务就存在的是用户先上传、知识库复用的，保持可见。
    幂等：已标记的不再处理，被用户另行添加过的（WORKSPACE_FLAG）不会被标回。返回本次标记的条数。
    """
    from .knowledge.models import KnowledgeImport
    from .todos.models import TodoMaterial
    from .tracking.models import FileSnapshot
    from .weekly.models import WeeklyReport

    with sessionmaker(engine)() as s:
        first_import = dict(s.execute(
            select(KnowledgeImport.material_id, func.min(KnowledgeImport.created_at))
            .where(KnowledgeImport.material_id.is_not(None)).group_by(KnowledgeImport.material_id)
        ).all())
        if not first_import:
            return 0
        used = set(s.scalars(select(TodoMaterial.material_id).where(TodoMaterial.material_id.in_(first_import))))
        used.update(s.scalars(select(FileSnapshot.material_id).where(FileSnapshot.material_id.in_(first_import))))
        for ids in s.scalars(select(WeeklyReport.material_ids)):
            used.update(ids or [])
        marked = 0
        query = select(Material).options(defer(Material.content, raiseload=True)).where(
            Material.id.in_(first_import), Material.source == "file")
        for material in s.scalars(query):
            if material.id in used or (material.parse_report or {}).get(WORKSPACE_FLAG):
                continue
            if material.created_at >= first_import[material.id]:
                material.source = KNOWLEDGE
                marked += 1
        s.commit()
    return marked


class MaterialStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session = sessionmaker(engine, expire_on_commit=False)

    def add_text(
        self,
        title: str,
        content: str,
        *,
        source: str = "paste",
        kind: str | None = None,
        markdown: bool | None = None,
        report: dict | None = None,
    ) -> tuple[Material, bool]:
        """返回 (材料, 是否新建)。内容相同的材料已存在时直接返回已有的那条。

        markdown 见 chunking.uses_markdown：粘贴的文本为 None，按内容判断。
        """
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            raise ValueError("材料内容为空")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._session() as s:
            existing = s.scalar(select(Material).where(Material.content_hash == digest))
            if existing and report:
                # Identical text from a newly parsed file must not lose stricter provenance
                # merely because an earlier paste/upload won content deduplication.
                from .knowledge.evidence import constraints,NOTES
                limits=constraints(report,0,len(content))
                if limits:
                    previous=dict(existing.parse_report or {})
                    previous['inherited_quality_constraints']={**previous.get('inherited_quality_constraints',{}),**limits}
                    previous['warnings']=list(dict.fromkeys([*previous.get('warnings',[]),*(NOTES[k] for k in limits)]))
                    existing.parse_report=previous
                    s.commit()
            if existing and existing.source == KNOWLEDGE and source == TRACKED:
                # 追踪的文件与知识库专属材料内容相同：追踪版本是用户自己的工作区文件，转为追踪材料，知识版本仍引用同一条
                existing.source = TRACKED
                s.commit()
                return existing, True
            if existing and existing.source in (TRACKED, KNOWLEDGE) and source not in (TRACKED, KNOWLEDGE):
                # 用户添加的内容恰好与某个追踪版本或知识库专属材料相同：转为用户材料（追踪版本、知识版本仍引用同一条），
                # 算作新建。知识库检索、引用与外发授权不受影响；在材料库删除它按原规则处理（见 delete）
                if existing.source == KNOWLEDGE:
                    existing.parse_report = {**(existing.parse_report or {}), WORKSPACE_FLAG: True}
                existing.source, existing.title = source, (title.strip() or existing.title)[:200]
                if report and report.get("feishu"):
                    existing.parse_report = {**existing.parse_report, "feishu": report["feishu"]}
                s.commit()
                return existing, True
            if existing:
                return existing, False
            material = Material(
                title=(title.strip() or content[:20])[:200],
                source=source,
                file_type=kind,
                content=content,
                char_count=len(content),
                content_hash=digest,
            )
            sections = parse_sections(content, markdown)
            report = dict(report or {})
            if not sections:
                report["structure"] = "none"
            elif not report.get("structure"):
                report["structure"] = "markdown" if uses_markdown(content, markdown) else "rules"
            material.parse_report = report
            material.sections = [
                MaterialSection(index=i, level=x.level, title=x.title[:200], path=x.path, start=x.start, end=x.end)
                for i, x in enumerate(sections, start=1)
            ]
            material.chunks = [
                MaterialChunk(
                    index=i, section=c.section, start=c.start, end=c.end, content=c.content, char_count=len(c.content)
                )
                for i, c in enumerate(chunk_text(content, sections), start=1)
            ]
            s.add(material)
            s.commit()
            return material, True

    def add_file(self, filename: str, data: bytes) -> tuple[Material, bool]:
        kind = file_type(filename)
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"文件超过 {config.MAX_UPLOAD_MB}MB 上限")
        doc = load_document(filename, data)
        if not doc.text.strip():
            hint = "，可能是扫描版 PDF" if kind == "pdf" else ""
            raise ValueError(f"未从文件中提取到文字{hint}")
        return self.add_text(filename, doc.text, source="file", kind=kind, markdown=doc.markdown, report=doc.report)

    def list_materials(self, ids: list[int] | None = None, content: bool = True) -> list[Material]:
        """不指定 ids 时列用户添加的材料和追踪中文件的最新版本（不含旧版本和知识库专属材料）。
        content=False 不读正文（列表摘要用）。指定的 ids 里有知识库专属材料时拒绝（KnowledgeOnlyMaterial）。"""
        query = select(Material).order_by(Material.id)
        if not content:
            query = query.options(defer(Material.content, raiseload=True))
        if ids is not None:
            self.require_workspace(ids)
            query = query.where(Material.id.in_(ids))
        else:
            query = query.where(visible())
        with self._session() as s:
            return list(s.scalars(query))

    def require_workspace(self, material_ids) -> None:
        """显式指定的材料里有知识库专属材料时拒绝，不静默放行；不存在的编号由各入口按原样处理。"""
        ids = list(material_ids)
        if not ids:
            return
        query = select(Material.id, Material.title).where(Material.id.in_(ids), Material.source == KNOWLEDGE)
        with self._session() as s:
            row = s.execute(query.order_by(Material.id).limit(1)).one_or_none()
        if row is not None:
            raise KnowledgeOnlyMaterial(row.id, row.title)

    def get(self, material_id: int, *, content: bool = True, workspace: bool = True) -> Material:
        """workspace=True（默认，材料库各入口）时知识库专属材料按不存在处理并说明属于知识库；
        只有知识库自身（入库预览、质量与引用核对）传 False。"""
        query = select(Material).where(Material.id == material_id)
        if not content:
            query = query.options(defer(Material.content, raiseload=True))
        with self._session() as s:
            material = s.scalar(query)
        if material is None:
            raise MaterialNotFound(f"材料 {material_id} 不存在")
        if workspace and material.source == KNOWLEDGE:
            raise KnowledgeOnlyMaterial(material.id, material.title)
        return material

    def delete(self, material_id: int, force: bool = False) -> None:
        """删除材料及其在本应用里的全部数据（一个事务内完成，不留失效引用）。

        - 关联在待办上：force=False 时拒绝（ConfirmationRequired，说明关联了几个待办）；确认后删除关联，
          每个待办记一条“材料已删除”的活动，旧活动里的材料编号清空（标题仍在活动内容里）。
        - 周报：正文和工作项不动；本周材料里去掉它，记进 removed_materials（编号、标题、删除时间）。
        - 章节、分块随材料删除；追踪版本对它的引用由外键置空。
        - 飞书导入的材料：只删本地记录，不调用飞书，云端原文件不受影响。
        """
        from .todos.models import MATERIAL_DELETED, TodoActivity, TodoMaterial
        from .todos.service import ConfirmationRequired
        from .weekly.models import WeeklyReport

        with self._session() as s:
            material = s.get(Material, material_id)
            if material is None:
                raise MaterialNotFound(f"材料 {material_id} 不存在")
            if material.source == KNOWLEDGE:
                raise KnowledgeOnlyMaterial(material.id, material.title)
            links = list(s.scalars(select(TodoMaterial).where(TodoMaterial.material_id == material_id)))
            todo_ids = list(dict.fromkeys(x.todo_id for x in links))
            if todo_ids and not force:
                raise ConfirmationRequired(
                    f"《{material.title}》关联在 {len(todo_ids)} 个待办上。删除后这些待办里不再有这份材料"
                    "（待办会记一条“材料已删除”），已生成的周报正文不变。确定删除吗？"
                )
            s.execute(update(TodoActivity).where(TodoActivity.material_id == material_id).values(material_id=None))
            for todo_id in todo_ids:
                s.add(TodoActivity(todo_id=todo_id, event_type=MATERIAL_DELETED, old_value={"title": material.title}))
            for link in links:
                s.delete(link)
            removed = {"id": material.id, "title": material.title, "removed_at": datetime.now().isoformat(timespec="seconds")}
            for report in s.scalars(select(WeeklyReport)):
                if material_id in (report.material_ids or []):
                    report.material_ids = [i for i in report.material_ids if i != material_id]
                    report.removed_materials = [*(report.removed_materials or []), removed]
            # 与知识库共用的同一条记录（用户先上传、或另行添加过）按原规则删除：知识版本的来源置空，
            # 由知识库按“来源已删除”处理（不能再发布、引用不会重新绑定），不在这里改知识库的来源治理
            s.delete(material)
            s.commit()

    def chunks(
        self, material_id: int, *, first: int | None = None, last: int | None = None, content: bool = True
    ) -> list[MaterialChunk]:
        """按需读块；范围含首尾，content=False 时只读位置等元数据。"""
        self._require_exists(material_id)
        query = select(MaterialChunk).where(MaterialChunk.material_id == material_id).order_by(MaterialChunk.index)
        if first is not None:
            query = query.where(MaterialChunk.index >= first)
        if last is not None:
            query = query.where(MaterialChunk.index <= last)
        if not content:
            query = query.options(defer(MaterialChunk.content, raiseload=True))
        with self._session() as s:
            return list(s.scalars(query))

    def read_chunk(self, material_id: int, index: int) -> tuple[str, int, MaterialChunk | None]:
        """一次查询取得标题、块总数和指定块；缺少目标块时仍返回总数供错误提示。"""
        count = select(func.count()).select_from(MaterialChunk).where(
            MaterialChunk.material_id == material_id
        ).correlate(None).scalar_subquery()
        query = select(Material.title, count, MaterialChunk, Material.source).outerjoin(
            MaterialChunk,
            (MaterialChunk.material_id == Material.id) & (MaterialChunk.index == index),
        ).where(Material.id == material_id)
        with self._session() as s:
            row = s.execute(query).one_or_none()
        if row is None:
            raise MaterialNotFound(f"材料 {material_id} 不存在")
        if row[3] == KNOWLEDGE:
            raise KnowledgeOnlyMaterial(material_id, row[0])
        return row[0], row[1], row[2]

    def sections(self, material_id: int) -> list[MaterialSection]:
        self._require_exists(material_id)
        query = (
            select(MaterialSection).where(MaterialSection.material_id == material_id).order_by(MaterialSection.index)
        )
        with self._session() as s:
            return list(s.scalars(query))

    def _require_exists(self, material_id: int) -> None:
        # 存在性检查仅查主键，不为验证存在再加载一份材料元数据。
        with self._session() as s:
            found = s.execute(select(Material.source, Material.title).where(Material.id == material_id)).one_or_none()
        if found is None:
            raise MaterialNotFound(f"材料 {material_id} 不存在")
        if found.source == KNOWLEDGE:
            raise KnowledgeOnlyMaterial(material_id, found.title)

    def chunk_range(self, material_id: int, start: int, end: int) -> tuple[int, int]:
        """与正文 [start, end) 重叠的块号范围；只查询块位置。"""
        query = select(func.min(MaterialChunk.index), func.max(MaterialChunk.index)).where(
            MaterialChunk.material_id == material_id, MaterialChunk.start < end, MaterialChunk.end > start
        )
        with self._session() as s:
            first, last = s.execute(query).one()
        return (first, last) if first is not None else (0, 0)

    def chunk_prefix(self, material_id: int, first: int, last: int, max_chars: int) -> list[MaterialChunk]:
        """按已有预算规则选择连续前缀：至少返回首块，只加载选中块的正文。"""
        query = select(MaterialChunk.index, MaterialChunk.char_count).where(
            MaterialChunk.material_id == material_id, MaterialChunk.index.between(first, last)
        ).order_by(MaterialChunk.index)
        size, stop = 0, None
        with self._session() as s:
            # 不把长章节的全部元数据积攒成列表；多数情况读取少数行即停止。
            for index, count in s.execute(query).yield_per(32):
                if stop is not None and size + count > max_chars:
                    break
                stop, size = index, size + count
        return self.chunks(material_id, first=first, last=stop) if stop is not None else []

    def section_text(self, material_id: int, start: int, end: int, max_chars: int) -> tuple[str | None, int]:
        """按原有 Python strip 规则测章节长度；仅在预算内时读取完整章节。

        长章节只分段检查首尾空白，不将整个章节载入 Python。返回 (正文或 None, 去空白后的字数)。
        """
        with self._session() as s:
            if self.engine.dialect.name == "sqlite":
                # SQLite TEXT substr 在 NUL 处停止；非常规文本回退 Python 切片一次。
                has_nul = s.scalar(select(func.instr(Material.content, "\x00") > 0).where(Material.id == material_id))
                if has_nul:
                    content = s.scalar(select(Material.content).where(Material.id == material_id))[start:end].strip()
                    return (content if len(content) <= max_chars else None), len(content)

            def read(a: int, b: int) -> str:
                span = func.substr(Material.content, a + 1, b - a)
                return s.scalar(select(span).where(Material.id == material_id))

            if end - start <= max_chars:
                content = read(start, end).strip()
                return content, len(content)
            # strip 的 Unicode 空白规则与数据库 trim 并不相同，分段检查保持原输出。
            while start < end:
                prefix = read(start, min(start + 4096, end))
                if not prefix:
                    raise ValueError("章节位置与材料正文不一致，请重新导入材料")
                stripped = prefix.lstrip()
                start += len(prefix) - len(stripped)
                if stripped:
                    break
            while start < end:
                suffix = read(max(start, end - 4096), end)
                if not suffix:
                    raise ValueError("章节位置与材料正文不一致，请重新导入材料")
                stripped = suffix.rstrip()
                end -= len(suffix) - len(stripped)
                if stripped:
                    break
            length = end - start
            return (read(start, end) if length <= max_chars else None), length

    def chunk_counts(self, material_ids: list[int] | None = None) -> dict[int, int]:
        return self._counts(MaterialChunk, material_ids)

    def section_counts(self, material_ids: list[int] | None = None) -> dict[int, int]:
        return self._counts(MaterialSection, material_ids)

    def _counts(self, model, material_ids: list[int] | None) -> dict[int, int]:
        query = select(model.material_id, func.count()).group_by(model.material_id)
        if material_ids is not None:
            query = query.where(model.material_id.in_(material_ids))
        with self._session() as s:
            return dict(s.execute(query).all())

    def search(self, keywords: str, material_ids: list[int] | None = None) -> list[SearchHit]:
        """空格分隔多个关键词，子串匹配（不区分大小写），命中任意一个即算，按命中次数排序。"""
        words = [w.lower() for w in keywords.split() if w]
        if not words:
            raise ValueError("关键词为空")
        query = select(MaterialChunk, Material.title).join(Material).order_by(Material.id, MaterialChunk.index)
        if material_ids is not None:
            self.require_workspace(material_ids)
            query = query.where(Material.id.in_(material_ids))
        else:
            query = query.where(visible())
        with self._session() as s:
            rows = s.execute(query).all()
        hits: list[SearchHit] = []
        for chunk, title in rows:
            lower = chunk.content.lower()
            count = sum(lower.count(w) for w in words)
            if not count:
                continue
            first = min(pos for w in words if (pos := lower.find(w)) >= 0)
            start, end = max(0, first - SNIPPET_RADIUS), first + SNIPPET_RADIUS
            text = chunk.content
            snippet = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
            hits.append(SearchHit(chunk.material_id, title, chunk.index, chunk.section, count, snippet))
        hits.sort(key=lambda h: h.hits, reverse=True)
        return hits[:MAX_SEARCH_HITS]
