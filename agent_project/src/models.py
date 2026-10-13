from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any

CHINA_TZ = timezone(timedelta(hours=8))


def now_china() -> datetime:
    return datetime.now(CHINA_TZ)


def normalize_symbol(value: str) -> str:
    value = value.strip().upper()
    match = re.fullmatch(r"(?:(SH|SZ|BJ))?(\d{6})(?:\.(SH|SZ|BJ))?", value)
    if not match:
        raise ValueError("请输入六位 A 股代码，例如 600519、SH600519 或 600519.SH。")
    code = match.group(2)
    if not code.startswith(("00", "30", "60", "68", "43", "83", "87", "88", "92")):
        raise ValueError("第一版仅支持沪深北 A 股个股代码。")
    if (match.group(1) or match.group(3)) and (match.group(1) or match.group(3)).lower() != exchange(code):
        raise ValueError("股票代码与交易所后缀不一致。")
    return code


def report_basename(symbol: str, generated_at: str) -> str:
    """Use the analysis timestamp, in China time, for all exported documents."""
    code = normalize_symbol(symbol)
    try:
        timestamp = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValueError("报告生成时间无效，无法命名导出文件。") from None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=CHINA_TZ)
    return f"{code}_{timestamp.astimezone(CHINA_TZ):%Y%m%d_%H%M}"


def exchange(code: str) -> str:
    return "sh" if code.startswith("6") else "sz" if code.startswith(("0", "3")) else "bj"


def number(value: Any) -> float | None:
    try:
        result = float(str(value).replace(",", "").replace("%", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


@dataclass
class SourceResult:
    key: str
    label: str
    interface: str
    url: str
    status: str = "unavailable"
    fetched_at: str = field(default_factory=lambda: now_china().isoformat(timespec="seconds"))
    records: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    cached: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
