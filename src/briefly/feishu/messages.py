"""机器人消息：把提醒发到用户自己的飞书（需要开启应用的机器人能力，并开通发消息权限）。"""
import json
import logging

from .. import config
from .client import FeishuClient, client_from_config, configured

log = logging.getLogger(__name__)

_ID_TYPES = {"email": "email", "openid": "open_id", "unionid": "union_id", "userid": "user_id"}


def receive_id_type(user: str) -> str:
    from .docx import member_type

    return _ID_TYPES[member_type(user)]


def send_text(client: FeishuClient, user: str, text: str) -> str:
    """发一条文本消息，返回 message_id。"""
    data = client.post(
        "/open-apis/im/v1/messages",
        {"receive_id": user.strip(), "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        receive_id_type=receive_id_type(user),
    )
    return data.get("message_id", "")


def notification_channel(client_factory=client_from_config, user: str | None = None):
    """返回登记到 weekly_schedule.CHANNELS 的发送函数；未配置凭证或接收人时返回 None。"""
    user = user if user is not None else config.FEISHU_NOTIFY_USER
    if not user or (client_factory is client_from_config and not configured()):
        return None

    def send(notification) -> None:
        text = notification.title + (f"\n{notification.body}" if notification.body else "")
        with client_factory() as client:
            send_text(client, user, text)

    send.feishu = True  # 便于去重登记
    return send


def register_channel() -> bool:
    """把飞书提醒登记为提醒渠道（重复调用只登记一次）。返回是否已登记。"""
    from ..tracking import weekly_schedule

    if any(getattr(c, "feishu", False) for c in weekly_schedule.CHANNELS):
        return True
    channel = notification_channel()
    if channel is None:
        return False
    weekly_schedule.CHANNELS.append(channel)
    return True
