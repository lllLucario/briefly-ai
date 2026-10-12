"""材料工具：列出、分块读取、关键词检索。

可访问范围由运行时 Context.material_ids 决定：用户本次选了材料时只能访问这些，否则可访问全部。
"""
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from ..context import Context
from ..materials import MaterialStore, wrap_material


def _scope(runtime: ToolRuntime[Context]) -> list[int] | None:
    return runtime.context.material_ids if runtime.context else None


def _check_in_scope(material_id: int, scope: list[int] | None) -> None:
    if scope is not None and material_id not in scope:
        raise ValueError(f"材料 {material_id} 不在用户本次选择的范围内，可用: {scope}")


def material_tools(store: MaterialStore) -> list[BaseTool]:
    @tool
    def list_materials(runtime: ToolRuntime[Context]) -> str:
        """列出当前可用的材料：编号、标题、类型、字数、块数、导入日期。不知道有哪些材料或材料编号时调用。"""
        scope = _scope(runtime)
        materials = store.list_materials(scope)
        if not materials:
            return "当前没有可用的材料。"
        counts = store.chunk_counts(scope)
        return "\n".join(
            f"- 材料 {m.id}《{m.title}》：{m.file_type or '粘贴文本'}，{m.char_count} 字，"
            f"{counts.get(m.id, 0)} 块，{m.created_at:%Y-%m-%d}"
            for m in materials
        )

    @tool
    def read_material(material_id: int, runtime: ToolRuntime[Context], chunk: int = 1) -> str:
        """读取某份材料的第 chunk 块（从 1 开始，每块约 1500 字）。返回内容会注明共几块；需要完整内容时按顺序读完所有块。"""
        _check_in_scope(material_id, _scope(runtime))
        material = store.get(material_id)
        chunks = store.chunks(material_id)
        if not 1 <= chunk <= len(chunks):
            raise ValueError(f"块号超出范围，材料 {material_id} 共 {len(chunks)} 块")
        header = f"【材料 {material_id}《{material.title}》第 {chunk}/{len(chunks)} 块】"
        return wrap_material(header, chunks[chunk - 1].content)

    @tool
    def search_materials(keywords: str, runtime: ToolRuntime[Context], material_ids: list[int] | None = None) -> str:
        """在材料中按关键词检索，返回最相关的片段及所在材料编号和块号。keywords 用空格分隔多个词，命中任意一个即算。
        material_ids 可限定在某几份材料中检索，不填则检索全部可用材料。需要定位某个主题在哪份材料、哪一块时调用，再用 read_material 读取完整的块。"""
        scope = _scope(runtime)
        if material_ids is not None:
            for material_id in material_ids:
                _check_in_scope(material_id, scope)
            scope = material_ids
        hits = store.search(keywords, scope)
        if not hits:
            return f"没有找到包含“{keywords}”的内容。"
        return "\n\n".join(
            wrap_material(f"【材料 {h.material_id}《{h.title}》第 {h.chunk} 块，命中 {h.hits} 次】", h.snippet)
            for h in hits
        )

    return [list_materials, read_material, search_materials]
