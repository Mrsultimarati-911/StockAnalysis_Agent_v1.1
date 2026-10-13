from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable

from .config import ROOT, Settings
from .models import CHINA_TZ, SourceResult, normalize_symbol, now_china

SOURCES = {
    "price": "前复权日线行情", "profile": "公司主营业务", "info": "公司基本资料",
    "valuation": "PE/PB 估值", "financial": "财务基本面", "announcements": "巨潮公告",
    "unlocks": "限售解禁", "sentiment": "市场关注度", "news": "近期新闻",
    "guba": "股吧帖子采样",
}

SOURCE_INTERFACES = {
    "price": ("stock_zh_a_hist", "stock_zh_a_hist_tx", "stock_zh_a_daily"),
    "profile": ("stock_zyjs_ths", "stock_profile_cninfo"),
    "info": ("stock_individual_info_em", "stock_profile_cninfo"),
    "valuation": ("stock_individual_spot_xq", "stock_zh_valuation_baidu"),
    "financial": ("stock_financial_analysis_indicator_em", "stock_financial_abstract_ths"),
    "announcements": ("stock_zh_a_disclosure_report_cninfo",),
    "unlocks": ("stock_restricted_release_queue_em",),
    "sentiment": ("stock_hot_rank_detail_em", "stock_comment_detail_scrd_focus_em"),
    "news": ("stock_news_em",),
    "guba": ("public_api", "desktop_public", "mobile_public"),
}
OPTION_LIMITS = {
    "lookback_days": (120, 1460), "announcement_days": (30, 365),
    "unlock_days": (30, 365), "guba_days": (1, 30),
    "guba_max_posts": (10, 100), "guba_max_pages": (1, 20),
}
SOURCE_OPTIONS = {key: ("preferred_source",) for key in SOURCES}
SOURCE_OPTIONS.update({
    "price": ("preferred_source", "lookback_days"),
    "announcements": ("preferred_source", "announcement_days"),
    "news": ("preferred_source", "announcement_days"),
    "unlocks": ("preferred_source", "unlock_days"),
    "guba": ("preferred_source", "guba_days", "guba_max_posts", "guba_max_pages"),
})


def validate_options(key: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the bounded tool surface before cache lookup or source execution."""
    if key not in SOURCES:
        raise ValueError("未知数据源：" + str(key))
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ValueError("options 必须是参数对象")
    invalid = set(options) - set(SOURCE_OPTIONS[key])
    if invalid:
        raise ValueError(f"{key} 不支持参数：" + "、".join(sorted(str(item) for item in invalid)))
    validated = {}
    for name, value in options.items():
        if name == "preferred_source":
            if not isinstance(value, str) or value not in ("auto", *SOURCE_INTERFACES[key]):
                raise ValueError(f"{key} 不支持所选接口；可用值：" + "、".join(("auto", *SOURCE_INTERFACES[key])))
        else:
            low, high = OPTION_LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} 必须是 {low} 至 {high} 的整数")
        validated[name] = value
    return validated


def _load_cache(path: Path, ttl: int) -> SourceResult | None:
    try:
        data = SourceResult(**json.loads(path.read_text(encoding="utf-8")))
        stamp = datetime.fromisoformat(data.fetched_at)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=CHINA_TZ)
        age = (now_china() - stamp).total_seconds()
        if data.status in {"ok", "empty"} and 0 <= age <= ttl:
            data.cached = True
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def fetch_one(key: str, symbol: str, settings: Settings, refresh: bool = False,
              options: dict[str, Any] | None = None, timeout: float | None = None) -> SourceResult:
    options = validate_options(key, options)
    symbol = normalize_symbol(symbol)
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout 必须是正数秒数")
    total_timeout = min(float(settings.source_timeout), float(timeout) if timeout is not None else float(settings.source_timeout))
    # Reserve a small amount for interpreter startup and serializing the result.
    work_seconds = total_timeout - min(1, total_timeout * .2)
    today = now_china().date().isoformat()
    job = dict(key=key, symbol=symbol, as_of=today, preferred_source="auto",
               timeout=min(float(settings.request_timeout), work_seconds), deadline_seconds=work_seconds)
    defaults = {"lookback_days": 730, "announcement_days": 90, "unlock_days": 180,
                "guba_days": settings.guba_days, "guba_max_posts": settings.guba_max_posts,
                "guba_max_pages": settings.guba_max_pages}
    for name in SOURCE_OPTIONS[key]:
        if name in defaults:
            low, high = OPTION_LIMITS[name]
            job[name] = max(low, min(high, int(defaults[name])))
    job.update(options)
    fingerprint = hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest()[:20]
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_version = "v5" if key == "guba" else "v4" if key == "unlocks" else "v3"
    cache = settings.cache_dir / f"{cache_version}_{key}_{symbol}_{fingerprint}.json"
    if not refresh:
        cached = _load_cache(cache, settings.cache_ttl)
        if cached is not None:
            return cached
    fd, name = tempfile.mkstemp(prefix="fetch_", suffix=".json", dir=settings.cache_dir)
    os.close(fd)
    output = Path(name)
    result = SourceResult(key, SOURCES[key], "AKShare", "https://akshare.akfamily.xyz/data/stock/stock.html")
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        # 独立进程使无 timeout 参数的上游接口也能在期限内停止。
        subprocess.run(
            [sys.executable, "-m", "src.fetch_worker", "--output", str(output)],
            input=json.dumps(job, ensure_ascii=False), text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env,
            timeout=total_timeout, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result = SourceResult(**json.loads(output.read_text(encoding="utf-8")))
        if result.status in {"ok", "empty"}:
            # 每个任务单独临时文件，再原子替换，避免并发读取半份缓存。
            output.replace(cache)
    except subprocess.TimeoutExpired:
        result.error = f"数据接口超过 {total_timeout:g} 秒，未完成核实。"
    except (subprocess.SubprocessError, OSError, ValueError, TypeError):
        result.error = "数据接口未成功返回有效数据，请刷新重试或检查网络。"
    finally:
        output.unlink(missing_ok=True)
    return result


def collect(symbol: str, settings: Settings, refresh: bool = False,
            progress: Callable[[str, int, int], None] | None = None) -> dict[str, SourceResult]:
    results: dict[str, SourceResult] = {}
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        futures = {pool.submit(fetch_one, key, symbol, settings, refresh): key for key in SOURCES}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                results[key] = SourceResult(key, SOURCES[key], "AKShare", "",
                                            error="数据任务异常，未完成核实。")
            if progress:
                progress(f"{SOURCES[key]}：{results[key].status}", len(results), len(SOURCES))
    return {key: results[key] for key in SOURCES}
