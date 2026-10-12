"""材料工具：列出、看大纲、按块或按章节读取、关键词检索、完整提炼（Map-Reduce）。

可访问范围由运行时 Context.material_ids 决定：用户本次选了材料时只能访问这些，否则可访问全部。
"""
from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from .. import config
from ..context import Context
from ..context_store import ContextStore
from ..llm import create_chat_model
from ..materials import MaterialChunk, MaterialStore, wrap_material
from ..skills import read_prompt
from ..summarize import Source, extract_items, synthesize
from ..tool_budget import output_limit


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


def _chunk_range(chunks: list[MaterialChunk], start: int, end: int) -> tuple[int, int]:
    """与 [start, end) 有重叠的块号范围。"""
    hit = [c.index for c in chunks if c.start < end and c.end > start]
    return (hit[0], hit[-1]) if hit else (0, 0)


def _blocks(first: int, last: int) -> str:
    return f"第 {first} 块" if first == last else f"第 {first}–{last} 块"


def material_tools(
    store: MaterialStore, model: BaseChatModel | None = None, context_store: ContextStore | None = None
) -> list[BaseTool]:
    """context_store 用于缓存逐块提取结果、记录调用观测数据。model 供 summarize_materials 逐块提取和汇总；不传时用时再创建。"""
    @tool
    def list_materials(runtime: ToolRuntime[Context]) -> str:
        """列出当前可用的材料：编号、标题、类型、字数、块数、章节数、导入日期。不知道有哪些材料或材料编号时调用。"""
        scope = _scope(runtime)
        materials = store.list_materials(scope)
        if not materials:
            return "当前没有可用的材料。"
        chunks, sections = store.chunk_counts(scope), store.section_counts(scope)
        return "\n".join(
            f"- 材料 {m.id}《{m.title}》：{m.file_type or '粘贴文本'}，{m.char_count} 字，"
            f"{chunks.get(m.id, 0)} 块，{sections.get(m.id, 0)} 个章节，{m.created_at:%Y-%m-%d}"
            for m in materials
        )

    @tool
    def get_outline(material_id: int, runtime: ToolRuntime[Context]) -> str:
        """查看某份材料的章节大纲：每章的编号、标题、所在块号和字数。
        材料较长、需要了解结构或只读其中某几章时先调用，再用 read_material 的 section 参数按章节读取。"""
        _check_in_scope(material_id, _scope(runtime))
        material = store.get(material_id)
        chunks = store.chunks(material_id)
        sections = store.sections(material_id)
        name = f"材料 {material_id}《{material.title}》"
        notes = material.parse_report.get("warnings", [])
        if not sections:
            text = f"{name}没有识别出章节，请用 search_materials 检索，或按块号读取（共 {len(chunks)} 块）。"
            return "\n".join([text, *(f"解析提示：{n}" for n in notes)])
        top = min(s.level for s in sections)
        lines = []
        for s in sections:
            first, last = _chunk_range(chunks, s.start, s.end)
            lines.append(f"{'  ' * (s.level - top)}{s.index}. {s.title}（{_blocks(first, last)}，{s.end - s.start} 字）")
        header = f"【{name}大纲：{len(sections)} 个章节，共 {len(chunks)} 块】"
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
        material = store.get(material_id)
        chunks = store.chunks(material_id)
        name = f"材料 {material_id}《{material.title}》"

        if section is None:
            chunk = chunk or 1
            if not 1 <= chunk <= len(chunks):
                raise ValueError(f"块号超出范围，材料 {material_id} 共 {len(chunks)} 块")
            current = chunks[chunk - 1]
            where = f"（{current.section}）" if current.section else ""
            return wrap_material(f"【{name}第 {chunk}/{len(chunks)} 块{where}】", current.content)

        sections = store.sections(material_id)
        target = next((s for s in sections if s.index == section), None)
        if target is None:
            raise ValueError(f"章节编号超出范围，材料 {material_id} 共 {len(sections)} 个章节，请先调用 get_outline")
        first, last = _chunk_range(chunks, target.start, target.end)
        text = material.content[target.start : target.end].strip()
        max_chars = _section_chars()
        if len(text) <= max_chars:
            return wrap_material(f"【{name}章节 {section}：{target.path}（{_blocks(first, last)}）】", text)

        # 章节太长：按块返回开头的一部分，并说明从哪里接着读
        taken, size = [], 0
        for c in chunks[first - 1 : last]:
            if taken and size + c.char_count > max_chars:
                break
            taken.append(c)
            size += c.char_count
        end = taken[-1].index
        header = (
            f"【{name}章节 {section}：{target.path}，共 {len(text)} 字（{_blocks(first, last)}），"
            f"较长，本次返回{_blocks(first, end)}】"
        )
        body = wrap_material(header, "\n".join(c.content for c in taken))
        return f"{body}\n本章节还未读完，请继续用 read_material(material_id={material_id}, chunk={end + 1}) 读到第 {last} 块。"

    @tool
    def search_materials(
        keywords: str, runtime: ToolRuntime[Context], material_ids: list[int] | None = None, limit: int = 10
    ) -> str:
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
            return f"没有找到包含“{keywords}”的内容。"
        parts, size, budget = [], 0, int((output_limit() - 200) * 1.5)
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
            size += len(part)
        return "\n\n".join(parts)

    @tool
    def summarize_materials(
        runtime: ToolRuntime[Context], focus: str = "", material_ids: list[int] | None = None
    ) -> str:
        """完整提炼材料：由程序逐块读取全部内容、提取要点并核对原文，再汇总成要点清单（每条带出处）。
        需要总结、梳理一份或多份较长材料的全部内容时使用，比逐块 read_material 省、而且不会漏读。
        focus 写关注点（如“支付相关的进展”），不写则提炼全部要点；material_ids 不填则处理本次可用的全部材料。
        返回的是要点清单，请再按用户需要的格式（总结、纪要、待办等）整理输出。"""
        scope = _scope(runtime)
        if material_ids is not None:
            for material_id in material_ids:
                _check_in_scope(material_id, scope)
            scope = material_ids
        sources = [
            Source(m.id, c.index, m.title, c.content)
            for m in store.list_materials(scope)
            for c in store.chunks(m.id)
        ]
        if not sources:
            return "当前没有可用的材料。"
        llm = model or create_chat_model()
        on_call = _recorder(context_store, runtime.context, getattr(llm, 'model_name', '') or config.MODEL)
        extraction = extract_items(llm, sources, focus=focus, cache=context_store, on_call=on_call)
        notes = [f"已处理 {extraction.processed}/{extraction.total} 块"]
        if extraction.cached:
            notes.append(f"其中 {extraction.cached} 块沿用之前的提取结果")
        notes += [f"第 {f['source']} 块处理失败，未纳入" for f in extraction.failed]
        if extraction.dropped:
            notes.append(f"{len(extraction.dropped)} 条提取结果在原文中找不到依据，已丢弃")
        if not extraction.items:
            return f"没有提取到{'与“' + focus + '”相关的' if focus else ''}内容（{'；'.join(notes)}）。"
        request = f"关注点：{focus or '无，整理全部要点'}"
        digest = synthesize(llm, read_prompt("digest"), request, extraction.items, on_call=on_call)
        body = digest.text
        if digest.uncovered:  # 汇总时漏写的要点原样附上，保证交给你的内容是完整的
            body += "\n\n汇总时未写入的要点：\n" + "\n".join(f"- {i.point}〔{i.source}〕" for i in digest.uncovered)
        if digest.invalid_refs:
            notes.append(f"汇总引用了不存在的要点编号 {digest.invalid_refs}，相关内容请谨慎使用")
        if not digest.coverage_checked:
            notes.append("汇总没有标注要点编号，未能核对是否漏写")
        header = f"【材料提炼结果：程序逐块提取并核对原文，〔材料号-块号〕为出处；{'；'.join(notes)}】"
        return wrap_material(header, body)

    return [list_materials, get_outline, read_material, search_materials, summarize_materials]
