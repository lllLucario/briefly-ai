"""飞书妙记：读取会议录音的转写文字稿（带说话人和时间戳），作为会议纪要等能力的材料。

不做本地录音识别：会议在飞书里录制或上传到妙记后，由妙记转写，这里只导出文字。
"""
from dataclasses import dataclass, field

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


def read_minutes(client: FeishuClient, token: str) -> FeishuMinutes:
    """妙记标题、时长 + 文字稿（txt，含录制时间、说话人和时间戳）。"""
    info = client.get(f"/open-apis/minutes/v1/minutes/{token}").get("minute") or {}
    raw = client.download(
        f"/open-apis/minutes/v1/minutes/{token}/transcript",
        need_speaker="true",
        need_timestamp="true",
        file_format="txt",
    )
    transcript = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").strip()
    title = info.get("title") or "飞书妙记"
    # 标题行不写录制时间：文字稿第一行已有（北京时间，带 CST），这里再按本机时区写一遍，时区不同时会像两种说法
    # （2026-10-15 实测本机比北京时间快 3 小时）。
    header = f"妙记《{title}》" + (f"（时长 {_duration(info.get('duration'))}）" if info.get("duration") else "")
    warnings = [] if transcript else ["妙记还没有文字稿（可能仍在转写中，或录音里没有识别出语音）"]
    return FeishuMinutes(token, title, f"{header}\n\n{transcript}".strip(), info.get("url", ""), warnings)
