"""飞书妙记：读取会议录音的转写文字稿（带说话人和时间戳），作为会议纪要等能力的材料。

不做本地录音识别：会议在飞书里录制或上传到妙记后，由妙记转写，这里只导出文字。
"""
from dataclasses import dataclass, field
from datetime import datetime

from .client import FeishuClient


@dataclass
class FeishuMinutes:
    token: str
    title: str
    text: str
    url: str = ""
    warnings: list[str] = field(default_factory=list)


def _duration(ms) -> str:
    try:
        seconds = int(ms) // 1000
    except (TypeError, ValueError):
        return ""
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"


def _date(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def read_minutes(client: FeishuClient, token: str) -> FeishuMinutes:
    """妙记标题、录制时间、时长 + 文字稿（txt，含说话人和时间戳）。"""
    info = client.get(f"/open-apis/minutes/v1/minutes/{token}").get("minute") or {}
    raw = client.download(
        f"/open-apis/minutes/v1/minutes/{token}/transcript",
        need_speaker="true",
        need_timestamp="true",
        file_format="txt",
    )
    transcript = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").strip()
    title = info.get("title") or "飞书妙记"
    meta = "，".join(x for x in (_date(info.get("create_time")), f"时长 {_duration(info.get('duration'))}" if info.get("duration") else "") if x)
    header = f"妙记《{title}》" + (f"（{meta}）" if meta else "")
    warnings = [] if transcript else ["妙记还没有文字稿（可能仍在转写中，或录音里没有识别出语音）"]
    return FeishuMinutes(token, title, f"{header}\n\n{transcript}".strip(), info.get("url", ""), warnings)
