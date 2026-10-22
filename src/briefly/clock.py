"""请求级时间：一次用户请求取一次当前时间（带时区），本轮的提示词、日期工具和日期校验共用。

时区沿用项目已有配置 BRIEFLY_TRACKING_TIMEZONE（IANA 名称，如 Australia/Sydney，定时任务也用它）；
未配置时用系统本地时区。不硬编码任何地区。时间只放在每次调用的系统提示里，不写进会话历史，避免过期。
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo as _tzinfo
from zoneinfo import ZoneInfo

from . import config

WEEKDAYS = "一二三四五六日"
WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def tzinfo() -> _tzinfo:
    """配置的时区；留空用系统时区。"""
    if config.TRACKING_TIMEZONE:
        return ZoneInfo(config.TRACKING_TIMEZONE)
    return datetime.now().astimezone().tzinfo


def tz_label(now: datetime) -> str:
    """IANA 名称（已配置时）或系统时区的 UTC 偏移说明。"""
    if config.TRACKING_TIMEZONE:
        return config.TRACKING_TIMEZONE
    offset = now.utcoffset() or timedelta()
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"系统本地时区（UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}）"


def now_local() -> datetime:
    """带时区的当前时间。"""
    return datetime.now(tzinfo())


def local_today() -> date:
    """按配置时区的今天（替代 date.today()：服务所在机器的时区可能与配置不同）。"""
    return now_local().date()


@dataclass(frozen=True)
class RequestTime:
    now: datetime  # 带时区

    @property
    def today(self) -> date:
        return self.now.date()

    def prompt(self) -> str:
        """注入系统提示的当前时间与使用规则（简短，每次调用的系统提示里一份）。"""
        n = self.now.replace(microsecond=0)
        return "\n".join([
            "# 当前时间（本次请求开始时由程序获取）",
            f"Current datetime: {n.isoformat()}",
            f"Current date: {n.date().isoformat()}（星期{WEEKDAYS[n.weekday()]}）",
            f"Current weekday: {WEEKDAYS_EN[n.weekday()]}",
            f"Timezone: {tz_label(n)}",
            "- 用户说的今天、昨天、明天、本周、下周等，以上面的当前时间为基准；需要算出具体日期时调用 resolve_date，不要心算。",
            "- 材料里的相对时间以材料自身的日期为基准（如会议日期），与用户当前的时间视角分开写清楚。",
            "- 不猜测材料没有写明的日期；关键日期有歧义时列出可能并请用户确认，不擅自定一个。",
        ])


def request_time(now: datetime | None = None) -> RequestTime:
    return RequestTime(now or now_local())
