"""飞书工具：用户在对话里粘贴飞书文档或多维表格链接时，读取当前内容存为材料，之后用材料工具处理。"""
from collections.abc import Callable

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from .. import config
from ..context import Context
from ..feishu import FeishuError, client_from_config
from ..feishu.service import import_link
from ..materials import MaterialStore, wrap_material
from .materials import RETRIEVAL_INFO


def feishu_tools(store: MaterialStore, client_factory: Callable = client_from_config) -> list[BaseTool]:
    @tool
    def read_feishu_link(url: str, runtime: ToolRuntime[Context]) -> str:
        """读取飞书新版文档、知识库页面或多维表格的当前内容，并存为材料。用户消息里有飞书链接（feishu.cn、larksuite.com）时调用，
        url 填用户给的完整链接。内容较短时直接返回全文；较长时返回材料编号，再用 get_outline、read_material、summarize_materials 读取。"""
        try:
            with client_factory() as client:
                material, _ = import_link(client, store, url)
        except FeishuError as e:
            raise ValueError(str(e)) from e
        context = runtime.context
        if context is not None and context.material_ids is not None and material.id not in context.material_ids:
            context.material_ids.append(material.id)  # 用户本次选了材料时，把刚读取的这份也纳入可访问范围
        notes = material.parse_report.get("warnings", [])
        name = f"材料 {material.id}《{material.title}》"
        if material.char_count <= config.SECTION_READ_CHARS:
            body = wrap_material(f"【{name}（读取自飞书）】", material.content)
            return "\n".join([body, *(f"解析提示：{n}" for n in notes)])
        sections = len(store.sections(material.id))
        outline = "有章节大纲，可先用 get_outline 查看" if sections else "没有识别出章节，可用 search_materials 检索或按块读取"
        return "\n".join(
            [f"已读取飞书内容并存为{name}，内容较长，{outline}；需要完整提炼时用 summarize_materials（{RETRIEVAL_INFO}）。",
             *(f"解析提示：{n}" for n in notes)]
        )  # fmt: skip

    return [read_feishu_link]
