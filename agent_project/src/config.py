from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path

from .models import normalize_symbol

ROOT = Path(__file__).resolve().parents[1]
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
MAX_MODEL_CALLS = 12
MAX_TOOL_CALLS = 36


def read_token(path: Path) -> str:
    """读取用户约定的 token: 格式，错误中永远不包含文件正文。"""
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise ValueError("API.txt 应只包含一行 token:你的密钥。")
    prefix, sep, token = lines[0].partition(":")
    token = token.strip()
    if not sep or prefix.strip().lower() != "token" or not token.startswith("sk-") or any(c.isspace() for c in token):
        raise ValueError("API.txt 格式不正确，应为 token:sk-开头的密钥。")
    return token


@dataclass
class Settings:
    llm_api_key: str = field(default="", repr=False)
    llm_base_url: str = DEEPSEEK_BASE_URL
    llm_model: str = DEEPSEEK_MODEL
    llm_timeout: float = 120
    source_timeout: int = 55
    request_timeout: int = 10
    workers: int = 4
    cache_ttl: int = 1800
    guba_days: int = 7
    guba_max_posts: int = 100
    guba_max_pages: int = 20
    agent_timeout: int = 600
    cache_dir: Path = ROOT / ".cache"
    report_dir: Path = ROOT / "outputs"
    log_dir: Path = ROOT / "logs"
    chart_display_bars: int = 250
    chart_ma_periods: tuple[int, ...] = (5, 10, 20, 60, 120)
    chart_show_macd: bool = True
    chart_show_volume: bool = True
    chart_show_support_resistance: bool = True

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        return load_run_config(config_path).settings


class ConfigError(ValueError):
    """Safe to show to users: messages never contain configuration values."""


@dataclass(frozen=True)
class RunConfig:
    settings: Settings
    stock_code: str
    holding_cost: float | None
    use_llm: bool
    refresh_data: bool
    config_path: Path


def load_run_config(config_path: str | Path | None = None) -> RunConfig:
    """Load and validate one config, without network access or legacy key reads."""
    try:
        path = (Path(config_path).expanduser() if config_path is not None else ROOT / "config.json").resolve()
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        raise ConfigError("无法读取配置文件；请检查 config.json 路径、UTF-8 编码和 JSON 格式。") from None
    if not isinstance(document, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象。")

    allowed = {
        "llm": {"api_key", "base_url", "model", "timeout"},
        "task": {"stock_code", "holding_cost", "use_llm", "refresh_data"},
        "limits": {"max_model_calls", "max_tool_calls", "agent_timeout_seconds"},
        "output": {"directory", "offline_html"},
        "logging": {"directory"},
        "chart": {"display_bars", "ma_periods", "show_macd", "show_volume", "show_support_resistance"},
        "data": {"source_timeout", "request_timeout", "workers", "cache_ttl", "guba_days", "guba_max_posts",
                 "guba_max_pages", "cache_directory"},
    }
    if set(document) - set(allowed):
        raise ConfigError("配置文件包含未支持的顶层字段，请参考 config.example.json。")
    sections = {}
    for name, keys in allowed.items():
        section = document.get(name, {})
        if not isinstance(section, dict) or set(section) - keys:
            raise ConfigError(f"配置段 {name} 格式不正确或含未支持字段，请参考 config.example.json。")
        sections[name] = section
    llm, task, limits, output, logging, chart, data = (sections[key] for key in allowed)

    def boolean(section: dict, key: str, default: bool) -> bool:
        value = section.get(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"配置项 {key} 必须是 true 或 false。")
        return value

    def integer(section: dict, key: str, default: int, low: int, high: int) -> int:
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ConfigError(f"配置项 {key} 必须是 {low} 至 {high} 之间的整数。")
        return value

    def directory(section: dict, key: str, default: str) -> Path:
        value = section.get(key, default)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ConfigError(f"配置项 {key} 必须是有效目录路径。")
        try:
            target = Path(value).expanduser()
            return (target if target.is_absolute() else path.parent / target).resolve()
        except (OSError, ValueError):
            raise ConfigError(f"配置项 {key} 的目录路径无效。") from None

    model = llm.get("model", DEEPSEEK_MODEL)
    base_url = llm.get("base_url", DEEPSEEK_BASE_URL)
    if model != DEEPSEEK_MODEL:
        raise ConfigError("仅允许 DeepSeek 4.1 Flash：llm.model 必须为 deepseek-flash。")
    if not isinstance(base_url, str) or base_url.rstrip("/") != DEEPSEEK_BASE_URL:
        raise ConfigError("仅允许官方 DeepSeek 接口：llm.base_url 必须为 https://api.deepseek.com。")
    integer(limits, "max_model_calls", MAX_MODEL_CALLS, MAX_MODEL_CALLS, MAX_MODEL_CALLS)
    integer(limits, "max_tool_calls", MAX_TOOL_CALLS, MAX_TOOL_CALLS, MAX_TOOL_CALLS)
    if not boolean(output, "offline_html", True):
        raise ConfigError("HTML 必须离线可用，output.offline_html 必须为 true。")

    use_llm = boolean(task, "use_llm", True)
    api_key = llm.get("api_key", "")
    if not isinstance(api_key, str):
        raise ConfigError("llm.api_key 必须是字符串。")
    if api_key and (not api_key.startswith("sk-") or any(character.isspace() for character in api_key)):
        raise ConfigError("llm.api_key 格式不正确，应为 sk- 开头且不含空白的密钥。")
    symbol = task.get("stock_code", "600875")
    if not isinstance(symbol, str):
        raise ConfigError("task.stock_code 必须是六位股票代码字符串。")
    try:
        symbol = normalize_symbol(symbol)
    except ValueError:
        raise ConfigError("task.stock_code 不是有效的沪深北 A 股代码。") from None
    cost = task.get("holding_cost")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0:
            raise ConfigError("task.holding_cost 必须是大于零的有限数值或 null。")
        cost = float(cost)
    periods = chart.get("ma_periods", [5, 10, 20, 60, 120])
    if (not isinstance(periods, list) or not periods or len(periods) > 5
            or any(isinstance(value, bool) or not isinstance(value, int) or value not in (5, 10, 20, 60, 120)
                   for value in periods)
            or len(periods) != len(set(periods))):
        raise ConfigError("chart.ma_periods 只能选择 5、10、20、60、120 中的非空、不重复整数周期。")

    settings = Settings(
        llm_api_key=api_key, llm_base_url=DEEPSEEK_BASE_URL, llm_model=DEEPSEEK_MODEL,
        llm_timeout=integer(llm, "timeout", 120, 5, 300),
        source_timeout=integer(data, "source_timeout", 55, 5, 180),
        request_timeout=integer(data, "request_timeout", 10, 2, 60),
        workers=integer(data, "workers", 4, 1, 6),
        cache_ttl=integer(data, "cache_ttl", 1800, 0, 86400),
        guba_days=integer(data, "guba_days", 7, 1, 30),
        guba_max_posts=integer(data, "guba_max_posts", 100, 10, 100),
        guba_max_pages=integer(data, "guba_max_pages", 20, 1, 20),
        agent_timeout=integer(limits, "agent_timeout_seconds", 600, 30, 600),
        cache_dir=directory(data, "cache_directory", ".cache"),
        report_dir=directory(output, "directory", "outputs"),
        log_dir=directory(logging, "directory", "logs"),
        chart_display_bars=integer(chart, "display_bars", 250, 20, 2000),
        chart_ma_periods=tuple(periods),
        chart_show_macd=boolean(chart, "show_macd", True),
        chart_show_volume=boolean(chart, "show_volume", True),
        chart_show_support_resistance=boolean(chart, "show_support_resistance", True),
    )
    return RunConfig(settings=settings, stock_code=symbol, holding_cost=cost, use_llm=use_llm,
                     refresh_data=boolean(task, "refresh_data", True), config_path=path)
