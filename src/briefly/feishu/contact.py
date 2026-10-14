"""通讯录：open_id 换成姓名（追踪飞书文档时显示最后编辑人）。需要“获取用户基本信息”权限，没有时退回 open_id。"""
from .client import FeishuClient, FeishuError


def user_name(client: FeishuClient, open_id: str, cache: dict[str, str] | None = None) -> str:
    if cache is not None and open_id in cache:
        return cache[open_id]
    try:
        user = client.get(f"/open-apis/contact/v3/users/{open_id}", user_id_type="open_id").get("user") or {}
        name = user.get("name") or open_id
    except FeishuError:
        name = open_id
    if cache is not None:
        cache[open_id] = name
    return name
