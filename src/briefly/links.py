"""工作区 ↔ 知识库文档关联（二期）：系统知道工作区里哪份文件和知识库里哪份文档是“同一份”。

关联只是一条连线，不改变两边的存储与治理（知识库内容仍只经知识库检索读取）：
- explicit：系统在移动时记录（从知识库复制到工作区、把工作区材料加入知识库）；
- suggested：自动发现的疑似重复（句子内容指纹重合度＋文件名相似度），待用户确认，不自动合并；
- confirmed：用户确认；dismissed：用户否认或解除，不再提示。

explicit / confirmed 的关联：工作区副本继承所关联知识库的外发授权（该库未允许外发时副本内容不发给模型），
工作区文件有更新时在知识库文档页和相关回答里提醒（更新知识库仍走入库流程，不自动发布）。
工作区一侧的身份：追踪文件按文件（跨版本不变），其余按材料。
"""
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import logging
from pathlib import PurePath
import re

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, delete, or_, select
from sqlalchemy.orm import Mapped, Session, defer, mapped_column

from .db import Base
from .loaders import SUPPORTED_TYPES
from .materials import MaterialNotFound

log = logging.getLogger(__name__)

EXPLICIT, SUGGESTED, CONFIRMED, DISMISSED = "explicit", "suggested", "confirmed", "dismissed"
ACTIVE = (EXPLICIT, CONFIRMED)  # 继承外发限制、提醒改稿的关联

# 疑似重复：句子内容指纹的重合度（Dice，按字数加权）。阈值用原创样本标定，见 files/plans/plan-workspace-retrieval.md
SUGGEST_OVERLAP = 0.6  # 达到即提示
NAME_ASSISTED_OVERLAP = 0.4  # 文件名几乎相同（改稿、副本、带版本号）时放宽到这里
NAME_SIMILAR = 0.9
MIN_SENTENCE_CHARS = 8  # 太短的句子（“第一章 总则”）各文档都有，不计入
_SENTENCE = re.compile(r"[。！？；!?;\n]+")
_NOISE = re.compile(r"[\W_]+")
_NAME_NOISE = re.compile(r"(?i)v\d+(?:\.\d+)*|第?[0-9一二三四五六七八九十]+版|副本|修订稿?|修改稿?|最终版?|终稿|定稿|草稿|初稿|"
                         r"\(\d+\)|（\d+）|\d{4}[-.年]?\d{1,2}(?:[-.月]\d{1,2}日?)?|\bcopy\b|[\W_]+")


class DocumentLink(Base):
    __tablename__ = "workspace_links"
    __table_args__ = (
        UniqueConstraint("material_id", "document_id"),
        UniqueConstraint("tracked_file_id", "document_id"),
        CheckConstraint("kind IN ('explicit','suggested','confirmed','dismissed')"),
        CheckConstraint("(material_id IS NULL) <> (tracked_file_id IS NULL)"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    tracked_file_id: Mapped[int | None] = mapped_column(ForeignKey("tracked_files.id", ondelete="CASCADE"), index=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12))
    score: Mapped[float | None] = mapped_column(Float)  # 疑似重复时的重合度
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class LinkNotFound(MaterialNotFound):
    """关联不存在（接口按 404 返回）。"""


# —— 相似度 ——

def sentence_keys(text: str) -> dict[str, int]:
    """句子内容指纹 → 字数。去掉空白、标点和 Markdown 记号后比较，改动一个字只影响所在的那一句。"""
    keys: dict[str, int] = {}
    for sentence in _SENTENCE.split(text):
        key = _NOISE.sub("", sentence).lower()
        if len(key) >= MIN_SENTENCE_CHARS:
            keys[hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]] = len(key)
    return keys


def comparison_sentences(text: str) -> dict[str, str]:
    """逐句对照用：去掉空白、标点后的句子 → 原句。含数字的短句也保留：幻灯片里“14个月；”常单独成句，
    改动恰好落在这种短句上（12→14 个月）时，不能因为它短就当成没有可比内容。"""
    out: dict[str, str] = {}
    for sentence in _SENTENCE.split(text):
        key = _NOISE.sub("", sentence).lower()
        if len(key) >= MIN_SENTENCE_CHARS or (key and any(c.isdigit() for c in key)):
            out.setdefault(key, sentence.strip(" -*#|\t"))
    return out


def overlap(a: dict[str, int], b: dict[str, int]) -> float:
    total = sum(a.values()) + sum(b.values())
    if not total:
        return 0.0
    return 2 * sum(a[k] for k in a.keys() & b.keys()) / total


def name_similarity(a: str, b: str) -> float:
    """文件名相似度：去掉扩展名、版本/日期/副本标记后比较。"""
    def norm(name):
        stem = PurePath(name).stem if PurePath(name).suffix.lower().lstrip(".") in SUPPORTED_TYPES else name
        return _NAME_NOISE.sub("", stem).lower()
    x, y = norm(a), norm(b)
    return SequenceMatcher(None, x, y).ratio() if x and y else 0.0


def is_suggested(score: float, names: float) -> bool:
    return score >= SUGGEST_OVERLAP or (score >= NAME_ASSISTED_OVERLAP and names >= NAME_SIMILAR)


# —— 工作区一侧的身份 ——

def tracked_file_of(s: Session, material_id: int) -> int | None:
    from .tracking.models import FileSnapshot

    return s.scalar(select(FileSnapshot.file_id).where(FileSnapshot.material_id == material_id)
                    .order_by(FileSnapshot.captured_at.desc(), FileSnapshot.id.desc()).limit(1))


def _owner(s: Session, material_id: int) -> dict:
    file_id = tracked_file_of(s, material_id)
    return dict(tracked_file_id=file_id) if file_id is not None else dict(material_id=material_id)


def _links_of(s: Session, material_id: int, kinds=None):
    from .tracking.models import FileSnapshot

    files = select(FileSnapshot.file_id).where(FileSnapshot.material_id == material_id)
    query = select(DocumentLink).where(or_(DocumentLink.material_id == material_id, DocumentLink.tracked_file_id.in_(files)))
    if kinds:
        query = query.where(DocumentLink.kind.in_(kinds))
    return list(s.scalars(query.order_by(DocumentLink.id)))


def current_material(s: Session, link: DocumentLink) -> int | None:
    """关联在工作区一侧当前的材料：追踪文件取最新版本，其余就是那份材料。"""
    if link.material_id is not None:
        return link.material_id
    from .tracking.models import FileSnapshot

    return s.scalar(select(FileSnapshot.material_id).where(FileSnapshot.file_id == link.tracked_file_id,
                    FileSnapshot.material_id.is_not(None)).order_by(FileSnapshot.version.desc()).limit(1))


def _published(s: Session, document_id: str):
    """文档当前发布的版本与其正文材料；没有发布或文档已移除时返回 (None, None)。"""
    from .knowledge.models import KnowledgeDocument, KnowledgePublication, KnowledgeVersion
    from .materials import Material

    doc = s.get(KnowledgeDocument, document_id)
    publication = s.get(KnowledgePublication, document_id)
    if doc is None or doc.deleted_at is not None or publication is None:
        return None, None
    version = s.get(KnowledgeVersion, publication.version_id)
    material = s.get(Material, version.material_id, options=[defer(Material.content, raiseload=True)]) if version.material_id else None
    return version, material


def _set(s: Session, owner: dict, kb_id: str, document_id: str, kind: str, score: float | None = None) -> DocumentLink:
    key = DocumentLink.material_id if "material_id" in owner else DocumentLink.tracked_file_id
    link = s.scalar(select(DocumentLink).where(key == next(iter(owner.values())), DocumentLink.document_id == document_id))
    if link is None:
        link = DocumentLink(**owner, kb_id=kb_id, document_id=document_id, kind=kind, score=score)
        s.add(link)
    else:
        link.kind, link.score = kind, score if score is not None else link.score
    s.flush()
    return link


# —— 外发继承与改稿提醒（供各入口按会话查询） ——

def egress_restrictions(s: Session, material_ids) -> dict[int, str]:
    """explicit/confirmed 关联到未允许外发的知识库的材料 → 给用户看的说明。suggested 不继承。"""
    from .knowledge.models import KnowledgeBase, KnowledgeDocument, KnowledgeEgressGrant
    from .materials import Material

    result = {}
    for material_id in dict.fromkeys(material_ids):
        for link in _links_of(s, material_id, ACTIVE):
            grant = s.get(KnowledgeEgressGrant, link.kb_id)
            if grant is not None and grant.allowed:
                continue
            kb, doc = s.get(KnowledgeBase, link.kb_id), s.get(KnowledgeDocument, link.document_id)
            if kb is None or kb.deleted_at is not None or doc is None or doc.deleted_at is not None:
                continue
            title = s.scalar(select(Material.title).where(Material.id == material_id)) or ""
            result[material_id] = (f"材料 {material_id}《{title}》关联了知识库“{kb.name}”中的《{doc.title}》，该库尚未允许外发，"
                                   "内容不发给模型。可在知识库页面开启“允许外发”，或在材料页解除关联。")
            break
    return result


def document_updates(s: Session, document_ids) -> dict[str, dict]:
    """explicit/confirmed 关联的工作区文件与知识库发布版内容不同：{文档ID: 更新时间、知识库版本号、工作区材料}。"""
    from .materials import Material

    result = {}
    ids = list(dict.fromkeys(document_ids))
    if not ids:
        return result
    for link in s.scalars(select(DocumentLink).where(DocumentLink.document_id.in_(ids), DocumentLink.kind.in_(ACTIVE))):
        version, published = _published(s, link.document_id)
        material_id = current_material(s, link)
        if version is None or material_id is None:
            continue
        material = s.get(Material, material_id, options=[defer(Material.content, raiseload=True)])
        if material is None or material.content_hash == version.content_hash:
            continue
        updated = material.created_at
        previous = result.get(link.document_id)
        if previous is None or updated > previous["updated_at"]:
            result[link.document_id] = dict(updated_at=updated, version=version.number, material_id=material.id,
                                            title=material.title, link_id=link.id)
    return result


def update_notice(title: str, update: dict) -> str:
    return f"《{title}》：工作区有更新（{update['updated_at']:%Y-%m-%d %H:%M}），知识库仍为第 {update['version']} 版。"


class LinkService:
    def __init__(self, engine) -> None:
        self.engine = engine
        self._keys: dict[str, dict[str, int]] = {}  # content_hash → 句子指纹，材料内容不变，可长期缓存

    def _fingerprint(self, s: Session, material) -> dict[str, int]:
        from .materials import Material

        if material.content_hash not in self._keys:
            content = s.scalar(select(Material.content).where(Material.id == material.id))
            self._keys[material.content_hash] = sentence_keys(content or "")
        return self._keys[material.content_hash]

    # —— 显式关联 ——

    def link_explicit(self, material_id: int, kb_id: str, document_id: str) -> DocumentLink:
        with Session(self.engine) as s, s.begin():
            link = _set(s, _owner(s, material_id), kb_id, document_id, EXPLICIT)
            s.expunge(link)
        return link

    def copy_to_workspace(self, store, kb_id: str, document_id: str):
        """把知识库文档当前发布版的正文放进工作区，并记显式关联。同内容按材料库规则转为工作区材料（知识版本
        仍引用同一条，检索、引用与外发授权不变）。返回 (材料, 是否新加入工作区)。"""
        from .knowledge.models import KnowledgeDocument
        from .knowledge.service import KnowledgeConflict, KnowledgeNotFound
        from .materials import KNOWLEDGE

        with Session(self.engine) as s:
            doc = s.get(KnowledgeDocument, document_id)
            if doc is None or doc.kb_id != kb_id or doc.deleted_at is not None:
                raise KnowledgeNotFound("文档不在当前知识库或已移除")
            version, material = _published(s, document_id)
            if version is None or material is None or material.content_hash != version.content_hash:
                raise KnowledgeConflict("文档尚无可用的发布版本，不能复制到工作区")
            title, kind = doc.title, material.file_type
        source = store.get(material.id, workspace=False)
        copied, added = store.add_text(title, source.content, source="file" if kind else "paste", kind=kind)
        added = added or source.source == KNOWLEDGE
        self.link_explicit(copied.id, kb_id, document_id)
        return copied, added

    # —— 确认 / 否认 ——

    def decide(self, link_id: int, confirm: bool) -> DocumentLink:
        with Session(self.engine) as s, s.begin():
            link = s.get(DocumentLink, link_id)
            if link is None:
                raise LinkNotFound("关联不存在")
            link.kind = CONFIRMED if confirm else DISMISSED
            s.flush()
            s.expunge(link)
        return link

    def forget_material(self, s: Session, material_id: int) -> None:
        """材料移出工作区时去掉按材料记的关联（追踪文件的关联跟着文件走）。"""
        s.execute(delete(DocumentLink).where(DocumentLink.material_id == material_id))

    # —— 疑似重复 ——

    def detect(self) -> int:
        """工作区默认可见的材料与各知识库已发布版本两两比较，达到阈值且这对文档还没有任何关联记录（含已否认）时
        写 suggested；已有的 suggested 更新重合度，降到阈值以下的删掉。返回新写入的条数。"""
        from .knowledge.models import KnowledgeBase, KnowledgeDocument, KnowledgePublication
        from .materials import Material, visible

        created = 0
        with Session(self.engine) as s, s.begin():
            workspace = list(s.scalars(select(Material).options(defer(Material.content, raiseload=True)).where(visible())))
            docs = s.execute(select(KnowledgeDocument).join(KnowledgePublication, KnowledgePublication.document_id == KnowledgeDocument.id)
                             .join(KnowledgeBase, KnowledgeBase.id == KnowledgeDocument.kb_id)
                             .where(KnowledgeDocument.deleted_at.is_(None), KnowledgeBase.deleted_at.is_(None))).scalars().all()
            published = []
            for doc in docs:
                version, material = _published(s, doc.id)
                if version is not None and material is not None and material.content_hash == version.content_hash:
                    published.append((doc, material))
            for mine in workspace:
                owner = _owner(s, mine.id)
                existing = {link.document_id: link for link in _links_of(s, mine.id)}
                keys = self._fingerprint(s, mine)
                for doc, theirs in published:
                    score = 1.0 if theirs.content_hash == mine.content_hash else overlap(keys, self._fingerprint(s, theirs))
                    hit = is_suggested(score, name_similarity(mine.title, doc.title))
                    link = existing.get(doc.id)
                    if link is None and hit:
                        _set(s, owner, doc.kb_id, doc.id, SUGGESTED, round(score, 4))
                        created += 1
                    elif link is not None and link.kind == SUGGESTED:
                        if hit:
                            link.score = round(score, 4)
                        else:
                            s.delete(link)
        return created

    # —— 给页面看的 ——

    def material_links(self, material_id: int) -> list[dict]:
        from .knowledge.models import KnowledgeBase, KnowledgeDocument, KnowledgeEgressGrant

        with Session(self.engine) as s:
            out = []
            for link in _links_of(s, material_id):
                if link.kind == DISMISSED:
                    continue
                kb, doc = s.get(KnowledgeBase, link.kb_id), s.get(KnowledgeDocument, link.document_id)
                if kb is None or kb.deleted_at is not None or doc is None or doc.deleted_at is not None:
                    continue
                version, _ = _published(s, link.document_id)
                grant = s.get(KnowledgeEgressGrant, link.kb_id)
                out.append(dict(id=link.id, kind=link.kind, score=link.score, kb_id=kb.id, kb_name=kb.name,
                                document_id=doc.id, document_title=doc.title,
                                version=version.number if version else None,
                                egress_allowed=bool(grant and grant.allowed)))
            return out

    def document_updates(self, document_ids) -> dict[str, dict]:
        with Session(self.engine) as s:
            return document_updates(s, document_ids)
