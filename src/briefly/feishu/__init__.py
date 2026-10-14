"""飞书集成（阶段 4）：粘贴链接读取文档 / 多维表格、周报写入飞书文档、追踪飞书文档、机器人提醒。

以应用身份（tenant_access_token）访问，只能读写把应用添加为协作者的文档。未配置凭证时其余功能照常可用。
"""
from .client import FeishuClient, FeishuError, FeishuNotConfigured, client_from_config, configured
from .links import find_links, looks_like_feishu, parse_link

__all__ = [
    "FeishuClient", "FeishuError", "FeishuNotConfigured", "client_from_config", "configured",
    "find_links", "looks_like_feishu", "parse_link",
]  # fmt: skip
