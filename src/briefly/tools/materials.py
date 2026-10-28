"""材料工具：列出、看大纲、按块或按章节读取、关键词检索、完整提炼（Map-Reduce）。

可访问范围由运行时 Context.material_ids 决定：用户本次选了材料时只能访问这些，否则可访问全部。
"""
import re
from dataclasses import asdict

from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from .. import config
from ..conflicts import ATTRIBUTES, UNRESOLVED, classify, conflict_problems, detect_conflicts, describe
from ..context import Context
from ..context_store import ContextStore
from ..llm import create_chat_model
from ..materials import MaterialStore, wrap_material
from ..skills import read_prompt
from ..summarize import Source, extract_items, synthesize
from ..tool_budget import output_limit


# 实测中模型会把工具返回的块号、块数、字数当成文档内容写进总结。所以给模型看的文字里尽量不出现这些处理信息：
# 出处用“《标题》章节”表示，块号等机器可读的信息放在工具结果的 artifact 里（不发给模型，供程序和排查使用）。
# 只有翻页读取（read_material 的块号）这类调用工具必需的信息才保留，并标注为检索信息。
RETRIEVAL_INFO = "检索信息，不是材料内容，不要写进回答"
_TAG = re.compile(r"〔([^〕]*)〕")


def _scope(runtime: ToolRuntime[Context]) -> list[int] | None:
    return runtime.context.material_ids if runtime.context else None


def _check_in_scope(material_id: int, scope: list[int] | None) -> None:
    if scope is not None and material_id not in scope:
        raise ValueError(f"材料 {material_id} 不在用户本次选择的范围内，可用: {scope}")


def _recorder(context_store: ContextStore | None, context: Context | None, model_name: str):
    """把 Map-Reduce 的每次调用记进观测表，归到当前对话的 run_id 下。"""
    if context_store is None:
        return None
    context = context or Context()

    def record(task_type: str, estimated: int, message, source: str) -> None:
        usage = getattr(message, "usage_metadata", None) or {}
        context_store.record_usage(
            thread_id=context.thread_id,
            run_id=context.run_id,
            model=model_name,
            task_type=task_type,
            estimated_tokens=estimated,
            note=f"出处 {source}" if source else "",
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

    return record


def _section_chars() -> int:
    """按章节读取单次最多返回的字数：配置上限与本次工具预算（中文约 1.7 字符/token，按 1.5 取保守值）取小。"""
    return min(config.SECTION_READ_CHARS, max(int((output_limit() - 300) * 1.5), 500))


def _blocks(first: int, last: int) -> str:
    return f"第 {first} 块" if first == last else f"第 {first}–{last} 块"


def material_tools(
    store: MaterialStore, model: BaseChatModel | None = None, context_store: ContextStore | None = None
) -> list[BaseTool]:
    """context_store 用于缓存逐块提取结果、记录调用观测数据。model 供 summarize_materials 逐块提取和汇总；不传时用时再创建。"""
    @tool
    def list_materials(runtime: ToolRuntime[Context]) -> str:
        """列出当前可用的材料：编号、标题、类型、有没有章节大纲、导入日期。不知道有哪些材料或材料编号时调用。"""
        scope = _scope(runtime)
        materials = store.list_materials(scope, content=False)
        if not materials:
            return "当前没有可用的材料。"
        sections = store.section_counts(scope)
        return f"{RETRIEVAL_INFO}：\n" + "\n".join(
            f"- 材料 {m.id}《{m.title}》：{m.file_type or '粘贴文本'}，"
            f"{'有章节大纲' if sections.get(m.id) else '没有识别出章节'}，{m.created_at:%Y-%m-%d}"
            for m in materials
        )

    @tool
    def get_outline(material_id: int, runtime: ToolRuntime[Context]) -> str:
        """查看某份材料的章节大纲：每章的编号和标题，较长的章节会注明需要分次读取。
        材料较长、需要了解结构或只读其中某几章时先调用，再用 read_material 的 section 参数按章节读取。"""
        _check_in_scope(material_id, _scope(runtime))
        material = store.get(material_id, content=False)
        sections = store.sections(material_id)
        name = f"材料 {material_id}《{material.title}》"
        notes = material.parse_report.get("warnings", [])
        if not sections:
            text = f"{name}没有识别出章节，请用 search_materials 检索，或用 read_material 从 chunk=1 开始按顺序读取（{RETRIEVAL_INFO}）。"
            return "\n".join([text, *(f"解析提示：{n}" for n in notes)])
        top = min(s.level for s in sections)
        limit = _section_chars()
        lines = [
            f"{'  ' * (s.level - top)}{s.index}. {s.title}" + ("（较长，需分次读取）" if s.end - s.start > limit else "")
            for s in sections
        ]
        header = f"【{name}的章节大纲（编号用于 read_material 的 section 参数）】"  # 不写块号、字数：会被当成文档内容
        return "\n".join([wrap_material(header, "\n".join(lines)), *(f"解析提示：{n}" for n in notes)])

    @tool
    def read_material(
        material_id: int, runtime: ToolRuntime[Context], chunk: int | None = None, section: int | None = None
    ) -> str:
        """读取某份材料。chunk 是块号（从 1 开始，每块约 1500 字）；section 是 get_outline 里的章节编号。两者只填一个，都不填时读第 1 块。
        返回内容会注明共几块；需要完整内容时按顺序读完所有块。章节过长时只返回前几块，并注明从哪一块接着读。"""
        if chunk is not None and section is not None:
            raise ValueError("chunk 和 section 只能填一个")
        _check_in_scope(material_id, _scope(runtime))

        if section is None:
            chunk = chunk or 1
            title, count, current = store.read_chunk(material_id, chunk)
            if not 1 <= chunk <= count or current is None:
                raise ValueError(f"块号超出范围，材料 {material_id} 共 {count} 块")
            name = f"材料 {material_id}《{title}》"
            where = f"（{current.section}）" if current.section else ""
            return wrap_material(f"【{name}第 {chunk}/{count} 块{where}】", current.content)

        material = store.get(material_id, content=False)
        name = f"材料 {material_id}《{material.title}》"
        sections = store.sections(material_id)
        target = next((s for s in sections if s.index == section), None)
        if target is None:
            raise ValueError(f"章节编号超出范围，材料 {material_id} 共 {len(sections)} 个章节，请先调用 get_outline")
        first, last = store.chunk_range(material_id, target.start, target.end)
        max_chars = _section_chars()
        text, length = store.section_text(material_id, target.start, target.end, max_chars)
        if text is not None:
            return wrap_material(f"【{name}章节 {section}：{target.path}（{_blocks(first, last)}）】", text)

        # 章节太长：按块返回开头的一部分，并说明从哪里接着读
        taken = store.chunk_prefix(material_id, first, last, max_chars)
        end = taken[-1].index
        header = (
            f"【{name}章节 {section}：{target.path}，共 {length} 字（{_blocks(first, last)}），"
            f"较长，本次返回{_blocks(first, end)}】"
        )
        body = wrap_material(header, "\n".join(c.content for c in taken))
        return f"{body}\n本章节还未读完，请继续用 read_material(material_id={material_id}, chunk={end + 1}) 读到第 {last} 块。"

    @tool(response_format="content_and_artifact")
    def search_materials(
        keywords: str, runtime: ToolRuntime[Context], material_ids: list[int] | None = None, limit: int = 10
    ) -> tuple[str, dict]:
        """在材料中按关键词检索，返回最相关的片段及所在材料编号、块号和章节。keywords 用空格分隔多个词，命中任意一个即算。
        material_ids 可限定在某几份材料中检索，不填则检索全部可用材料；limit 是最多返回几条（不超过 10）。
        需要定位某个主题在哪份材料、哪一块时调用，再用 read_material 读取完整的块。"""
        scope = _scope(runtime)
        if material_ids is not None:
            for material_id in material_ids:
                _check_in_scope(material_id, scope)
            scope = material_ids
        hits = store.search(keywords, scope)[: max(1, limit)]
        if not hits:
            return f"没有找到包含“{keywords}”的内容。", {}
        parts, selected_ids, size, budget = [], [], 0, int((output_limit() - 200) * 1.5)
        for n, h in enumerate(hits):
            part = wrap_material(
                f"【材料 {h.material_id}《{h.title}》第 {h.chunk} 块{f'（{h.section}）' if h.section else ''}，"
                f"命中 {h.hits} 次】",
                h.snippet,
            )
            if parts and size + len(part) > budget:  # 按本次工具预算放，放不下的注明条数
                parts.append(f"（另有 {len(hits) - n} 条结果因篇幅省略，请换更具体的关键词）")
                break
            parts.append(part)
            selected_ids.append(h.material_id)
            size += len(part)
        # artifact只记录真正输出的来源，不能把可访问全库当成已读依据。
        from ..knowledge.evidence import material_quality_notice
        selected_ids = list(dict.fromkeys(selected_ids))
        return "\n\n".join(parts), {"material_ids": selected_ids,
            "quality_notice": material_quality_notice(store, selected_ids)}

    @tool(response_format="content_and_artifact")
    def summarize_materials(
        runtime: ToolRuntime[Context], focus: str = "", material_ids: list[int] | None = None
    ) -> tuple[str, dict]:
        """完整提炼材料：由程序逐块读取全部内容、提取要点并核对原文，再汇总成要点清单（每条带出处）。
        需要总结、梳理一份或多份较长材料的全部内容时使用，比逐块 read_material 省、而且不会漏读。
        focus 写关注点（如“支付相关的进展”），不写则提炼全部要点；material_ids 不填则处理本次可用的全部材料。
        返回的是要点清单，请再按用户需要的格式（总结、纪要、待办等）整理输出。"""
        scope = _scope(runtime)
        if material_ids is not None:
            for material_id in material_ids:
                _check_in_scope(material_id, scope)
            scope = material_ids
        from ..knowledge.evidence import material_quality_notice
        selected = [m.id for m in store.list_materials(scope, content=False)]
        notice = material_quality_notice(store, selected)
        if notice:
            return notice, {"material_ids": selected, "quality_blocked": True, "quality_notice": notice}
        sources, labels, outlines, titles = [], {}, {}, {}
        for m in store.list_materials(scope, content=False):
            titles[m.id] = m.title
            outlines[m.id] = store.sections(m.id)
            for c in store.chunks(m.id):
                # 传入块在全文中的起点：要点的原文位置才能对应到章节（不传时只知道块内位置）
                sources.append(Source(m.id, c.index, m.title, c.content, c.start))
                section = c.section.rsplit(" > ", 1)[-1] if c.section else ""  # 块起点所在的章节：定位不到要点时的退路
                labels[f"{m.id}-{c.index}"] = f"《{m.title}》" + (section if section != m.title else "")
        if not sources:
            return "当前没有可用的材料。", {}
        llm = model or create_chat_model()
        on_call = _recorder(context_store, runtime.context, getattr(llm, 'model_name', '') or config.MODEL)
        extraction = extract_items(llm, sources, focus=focus, cache=context_store, on_call=on_call)
        # 给模型看的说明不写块号、块数；失败的部分按“《标题》章节”说明
        notes = ["材料已全部处理"] if not extraction.failed else []
        failed = list(dict.fromkeys(labels.get(f["source"], "部分内容") for f in extraction.failed))
        notes += [f"{label}有部分内容处理失败，未纳入" for label in failed]
        if extraction.dropped:
            notes.append(f"{len(extraction.dropped)} 条提取结果在原文中找不到依据，已丢弃")
        artifact = {
            "material_ids": list(titles),
            "processed": extraction.processed,
            "total": extraction.total,
            "cached": extraction.cached,
            "failed": extraction.failed,
            "dropped": extraction.dropped,
            "items": [asdict(i) for i in extraction.items],  # 机器可读的出处：source 为“材料号-块号”
        }
        # 出处按要点所在章节标：一块常跨两个章节，按块起点标会把后一章的要点标成前一章，
        # 模型读到“讲约束规则的要点标在角色设定下”会当成文档自己标错了（2026-10-15 实测）
        item_labels = [_item_label(i, labels, outlines, titles) for i in extraction.items]
        if not extraction.items:
            return f"没有提取到{'与“' + focus + '”相关的' if focus else ''}内容（{'；'.join(notes)}）。", artifact
        request = f"关注点：{focus or '无，整理全部要点'}"
        # 多份材料的不同说法：合并之前由代码检测，避免汇总时被当成重复内容“统一”掉
        conflicts = detect_conflicts(extraction.items)
        classify(llm, conflicts, extraction.items)
        if conflicts:
            request += "\n\n" + describe(conflicts, extraction.items)

        def check(text: str) -> list[str]:
            cited: dict[int, list[str]] = {}
            for line in text.splitlines():
                for n in dict.fromkeys(int(x) for x in re.findall(r"#(\d+)", line)):
                    cited.setdefault(n, []).append(line)
            return conflict_problems(conflicts, lambda n: cited.get(n, []))

        digest = synthesize(llm, read_prompt("digest"), request, extraction.items, on_call=on_call, check=check)
        # 必须保留的信息放在最前面：结果超出工具预算被截断时，截掉的是末尾
        front = []
        unresolved = [c for c in conflicts if c.resolution == UNRESOLVED]
        if unresolved:  # 看不出哪个为准的不同说法：原样列出，回答里要保留，不要替用户选定
            front.append("不同材料说法不一致（看不出哪个为准，回答中请同时写出并说明不一致）：\n" + "\n".join(
                f"- {c.subject} 的{ATTRIBUTES[c.attribute]}："
                + " / ".join(f"{v}〔{item_labels[n - 1]}〕" for n, v in zip(c.numbers, c.values))
                for c in unresolved
            ))
        artifact["conflicts"] = [c.to_dict() for c in conflicts]
        if digest.uncovered:  # 汇总时漏写的要点原样附上，保证交给你的内容是完整的
            index = {id(i): n for n, i in enumerate(extraction.items)}
            front.append("汇总时未写入的要点：\n" + "\n".join(f"- {i.point}〔{item_labels[index[id(i)]]}〕" for i in digest.uncovered))
        text = _item_sources(digest.raw, item_labels) if digest.raw else _readable_sources(digest.text, labels)
        body = "\n\n".join([*front, ("要点清单：\n" if front else "") + text])
        if digest.invalid_refs:
            notes.append(f"汇总引用了不存在的要点编号 {digest.invalid_refs}，相关内容请谨慎使用")
        if not digest.coverage_checked:
            notes.append("汇总没有标注要点编号，未能核对是否漏写")
        coverage = "程序已读完全部内容并核对原文" if not extraction.failed else f"已处理 {extraction.processed}/{extraction.total} 部分，部分内容处理失败"
        header = (
            f"【材料提炼结果：{coverage}。〔〕内是程序标注的出处（要点来自哪份材料的哪一章），"
            f"不是原文内容，不要评论或转述它（{RETRIEVAL_INFO}：{'；'.join(notes)}）】"
        )
        artifact["uncovered"] = [asdict(i) for i in digest.uncovered]
        content = wrap_material(header, body)
        artifact["content_full"] = content  # 截断前的完整输出（诊断用：区分 Map-Reduce 损失与工具预算截断损失）
        return content, artifact

    return [list_materials, get_outline, read_material, search_materials, summarize_materials]


def _item_label(item, labels: dict[str, str], outlines: dict, titles: dict[int, str]) -> str:
    """要点所在的最深一级章节；原文位置不精确（-1）或不在任何章节里时，用块起点所在的章节。"""
    fallback = labels.get(item.source, "")
    try:
        material_id = int(item.source.split("-", 1)[0])
    except ValueError:
        return fallback
    if item.span_start < 0:
        return fallback
    hits = [s for s in outlines.get(material_id, []) if s.start <= item.span_start < s.end]
    if not hits:
        return fallback
    section = max(hits, key=lambda s: s.level).title
    title = titles.get(material_id, "")
    return f"《{title}》" + (section if section != title else "")


def _item_sources(raw: str, item_labels: list[str]) -> str:
    """合成结果里的编号〔#3〕〔#3、#5〕换成各要点自己的出处〔《标题》章节〕（同一出处只写一次）。"""
    from ..summarize import resolve_refs

    keys = [f"{n}-0" for n in range(1, len(item_labels) + 1)]  # 借 resolve_refs 的编号解析，先换成临时键
    placeholder = [_Ref(k) for k in keys]
    text, _, _ = resolve_refs(raw, placeholder)
    return _readable_sources(text, dict(zip(keys, item_labels)))


class _Ref:
    """resolve_refs 只读工作项的 source 字段。"""

    def __init__(self, source: str) -> None:
        self.source = source


def _readable_sources(text: str, labels: dict[str, str]) -> str:
    """〔材料号-块号〕出处换成〔《标题》章节〕：块号是内部编号，模型会把它当成内容写进回答。"""

    def replace(m: re.Match) -> str:
        parts = [p.strip() for p in re.split(r"[、,，]", m.group(1))]
        if not all(p in labels for p in parts):
            return m.group(0)
        return f"〔{'；'.join(dict.fromkeys(labels[p] for p in parts))}〕"

    return _TAG.sub(replace, text)
