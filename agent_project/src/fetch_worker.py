"""Bounded AKShare jobs; each worker handles one public data source.

No credentials, scraping service or custom financial API is required. Source errors
remain visible in the result instead of being interpreted as a lack of risk.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import html
import json
import math
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Callable

import pandas as pd

from .models import SourceResult, exchange, normalize_symbol, now_china, number
from .provider import SOURCE_OPTIONS, validate_options


LABELS = {
    "profile": "公司主营业务", "info": "公司基本资料", "valuation": "PE/PB 估值",
    "financial": "财务基本面", "price": "前复权日线行情", "announcements": "巨潮公告",
    "unlocks": "限售解禁", "sentiment": "市场关注度", "news": "近期新闻",
}
DOC_URL = "https://akshare.akfamily.xyz/data/stock/stock.html"


def _date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (ValueError, TypeError):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y%m%d").date() if re.fullmatch(r"\d{8}", text) else date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    if value is None:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        if pd.isna(value):
            return None
    except (ValueError, TypeError):
        pass
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _safe(value.item())
    return value


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("上游返回的不是表格")
    return _safe(frame.to_dict(orient="records"))


def _require(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("上游返回的不是表格")
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError("上游字段变化，缺少：" + "、".join(missing))


def _numeric(value: Any) -> float | None:
    text = str(value).strip().replace(",", "")
    multiplier = 1
    for suffix, factor in (("亿元", 1e8), ("万元", 1e4), ("亿", 1e8), ("万", 1e4), ("元", 1)):
        if text.endswith(suffix):
            text, multiplier = text[:-len(suffix)], factor
            break
    parsed = number(text)
    return parsed * multiplier if parsed is not None else None


def normalize_price(frame: pd.DataFrame, source: str, start: date, end: date) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    if source == "stock_zh_a_hist":
        frame = frame.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                                      "最低": "low", "收盘": "close", "成交量": "volume"})
    _require(frame, ("date", "open", "high", "low", "close", "volume"))
    records: dict[str, dict[str, Any]] = {}
    for row in _frame_records(frame):
        day = _date(row["date"])
        if day is None or not start <= day <= end:
            continue
        prices = {key: number(row[key]) for key in ("open", "high", "low", "close")}
        if any(value is None for value in prices.values()):
            continue
        if prices["high"] < prices["low"]:
            continue
        volume = number(row["volume"])
        # EM documents lots; AKShare 1.19.1 Tencent and Sina already return shares.
        if volume is not None and source == "stock_zh_a_hist":
            volume *= 100
        records[day.isoformat()] = dict(date=day.isoformat(), **prices, volume=volume)
    if not records and not frame.empty:
        valid_days = [_date(value) for value in frame["date"]]
        if any(day is not None and start <= day <= end for day in valid_days):
            raise ValueError("区间内行情存在，但 OHLC 数据无效")
        if not any(day is not None for day in valid_days):
            raise ValueError("行情日期字段无法解析")
    return [records[key] for key in sorted(records)]


FINANCIAL_FIELDS = {
    "TOTALOPERATEREVE": "营业总收入", "PARENTNETPROFIT": "净利润",
    "TOTALOPERATEREVETZ": "营业总收入同比增长率", "PARENTNETPROFITTZ": "净利润同比增长率",
    "KCFJCXSYJLR": "扣非净利润", "KCFJCXSYJLRTZ": "扣非净利润同比增长率",
    "ROEJQ": "净资产收益率", "ZCFZL": "资产负债率", "MGJYXJJE": "每股经营现金流",
    "EPSJB": "基本每股收益", "BPS": "每股净资产", "XSMLL": "销售毛利率", "XSJLL": "销售净利率",
}


def normalize_financial(frame: pd.DataFrame, as_of: date) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    frame = frame.rename(columns=FINANCIAL_FIELDS)
    date_column = "REPORT_DATE" if "REPORT_DATE" in frame.columns else "报告期"
    _require(frame, (date_column, "营业总收入", "净利润"))
    records = []
    eligible_periods = 0
    for row in _frame_records(frame):
        period = _date(row[date_column])
        published = _date(row.get("NOTICE_DATE", row.get("公告日期")))
        if period is None or period > as_of or (published and published > as_of):
            continue
        eligible_periods += 1
        record = {"报告期": period.isoformat(), "公告日期": published.isoformat() if published else None,
                  "报告类型": row.get("REPORT_DATE_NAME") or ("年报" if period.month == 12 else f"{period.month}月累计报告"),
                  "指标口径": "年初至报告期累计；同比与上年同一累计期间比较，非全年预测",
                  "金额单位": "元", "比率单位": "%"}
        for key in FINANCIAL_FIELDS.values():
            record[key] = _numeric(row.get(key))
        # A report date alone is not financial evidence. Require at least one
        # usable income/profit amount; zero is valid, missing values stay None.
        # Other metrics can remain partial without being invented or zero-filled.
        if record["营业总收入"] is None and record["净利润"] is None:
            continue
        records.append(record)
    if not records and not any(_date(value) for value in frame[date_column]):
        raise ValueError("财务报告期字段无法解析")
    if not records and eligible_periods:
        raise ValueError("已公开报告期没有有效营业总收入或净利润，无法核实财务数据")
    records.sort(key=lambda row: row["报告期"], reverse=True)
    return records[:8]


def normalize_unlocks(frame: pd.DataFrame, as_of: date, days: int = 180) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    _require(frame, ("解禁时间", "解禁数量", "占总市值比例", "占流通市值比例"))
    end = as_of + timedelta(days=days)
    records = []
    for row in _frame_records(frame):
        day = _date(row["解禁时间"])
        if day is None or not as_of <= day <= end:
            continue
        total_ratio, float_ratio = number(row.get("占总市值比例")), number(row.get("占流通市值比例"))
        records.append(dict(row, date=day.isoformat(), shares=number(row.get("解禁数量")),
                            actual_shares=number(row.get("实际解禁数量")),
                            market_value_yuan=number(row.get("实际解禁数量市值")),
                            total_ratio_pct=total_ratio * 100 if total_ratio is not None else None,
                            float_ratio_pct=float_ratio * 100 if float_ratio is not None else None,
                            type=row.get("限售股类型")))
    if not records and not any(_date(value) for value in frame["解禁时间"]):
        raise ValueError("解禁日期字段无法解析")
    return sorted(records, key=lambda row: row["date"])


def _dated_records(frame: pd.DataFrame, date_column: str, start: date, end: date,
                   mapping: dict[str, str], limit: int = 100) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    _require(frame, tuple([date_column, *mapping]))
    rows = []
    for row in _frame_records(frame):
        day = _date(row[date_column])
        if day is not None and start <= day <= end:
            entry = {target: row[source] for source, target in mapping.items()}
            entry["date"] = day.isoformat()
            if "title" in entry:
                entry["title"] = html.unescape(re.sub(r"<[^>]+>", "", str(entry["title"])))
            rows.append(entry)
    if not rows and not any(_date(value) for value in frame[date_column]):
        raise ValueError("日期字段无法解析")
    return sorted(rows, key=lambda row: row["date"], reverse=True)[:limit]


def _bounded_call(function: Callable[[], Any], timeout: float) -> Any:
    """AKShare APIs without timeout cannot stall fallback processing indefinitely."""
    output: queue.Queue = queue.Queue(maxsize=1)
    def run() -> None:
        try:
            output.put((True, function()))
        except Exception as exc:
            output.put((False, exc))
    threading.Thread(target=run, daemon=True).start()
    try:
        success, value = output.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"接口超过 {timeout:g} 秒") from None
    if not success:
        raise value
    return value


@dataclass
class _Candidate:
    interface: str
    url: str
    fetch: Callable[[], list[dict[str, Any]]]
    note: str = ""


def _needs_current_price(as_of: date) -> bool:
    current = now_china()
    return as_of == current.date() and (current.hour, current.minute) >= (15, 10)


def _attempts(key: str, candidates: list[_Candidate], timeout: float, *,
              as_of: date | None = None, check_freshness: bool = False,
              deadline: float | None = None) -> SourceResult:
    errors = []
    empty = None
    best_price = None
    best_date = None
    for candidate in candidates:
        remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
        if remaining <= 0:
            errors.append("本次数据源执行时间预算已用完")
            break
        result = SourceResult(key, LABELS[key], candidate.interface, candidate.url, note=candidate.note)
        try:
            result.records = _safe(_bounded_call(candidate.fetch, remaining))
            result.status = "ok" if result.records else "empty"
            if result.records:
                if key == "price":
                    dates = [_date(row.get("date")) for row in result.records]
                    valid_dates = [day for day in dates if day is not None]
                    if not valid_dates:
                        raise ValueError("行情没有可识别的最新日期")
                    latest = max(valid_dates)
                    if as_of is not None and latest > as_of:
                        raise ValueError("行情日期晚于查询日期")
                    result.note += f"；最新行情日期：{latest.isoformat()}。"
                    if check_freshness and as_of is not None and latest < as_of:
                        if best_date is None or latest > best_date:
                            best_price, best_date = result, latest
                        errors.append(f"{candidate.interface} 最新日期 {latest.isoformat()}，继续核对备用来源")
                        continue
                if errors:
                    result.note += "；来源核对记录：" + "；".join(errors)
                return result
            empty = empty or result
            errors.append(f"{candidate.interface} 返回空结果")
        except Exception as exc:
            # Endpoint URLs can include lengthy parameters; retain useful diagnostics only.
            detail = re.sub(r"\s+", " ", str(exc))[:280]
            errors.append(f"{candidate.interface}: {type(exc).__name__}: {detail}")
    if best_price is not None:
        best_price.note += (f"【行情滞后提示】所有可用来源中最新日为 {best_date.isoformat()}，"
                            f"早于查询日 {as_of.isoformat()}；可能为非交易日或来源更新延迟。"
                            "技术结论与点位仅基于该历史日线，整段行情来自同一来源。")
        best_price.note += "；来源核对记录：" + "；".join(errors)
        return best_price
    if empty is not None:
        empty.note += "；" + "；".join(errors)
        return empty
    result = SourceResult(key, LABELS[key], " / ".join(c.interface for c in candidates), candidates[0].url)
    result.error = "；".join(errors)
    result.note = candidates[0].note
    return result


def fetch_source(job: dict[str, Any]) -> SourceResult:
    key = str(job.get("key", ""))
    if key not in SOURCE_OPTIONS:
        raise ValueError("未知数据源：" + key)
    options = validate_options(key, {name: job[name] for name in SOURCE_OPTIONS[key] if name in job})
    job = dict(job, **options)
    if key == "guba":
        from .guba import fetch_guba
        return fetch_guba(job)
    if key not in LABELS:
        raise ValueError("未知数据源：" + key)
    symbol = normalize_symbol(str(job.get("symbol", "")))
    as_of = _date(job.get("as_of")) or now_china().date()
    timeout = max(0.1, float(job.get("timeout", 10)))
    work_seconds = float(job.get("deadline_seconds", 45))
    if not math.isfinite(work_seconds) or work_seconds <= 0:
        raise ValueError("数据源执行时间预算必须为正数")
    deadline = time.monotonic() + work_seconds
    timeout = min(timeout, work_seconds)
    start = as_of - timedelta(days=int(job.get("lookback_days", 730)))
    announcement_start = as_of - timedelta(days=int(job.get("announcement_days", 90)))
    market = exchange(symbol)
    prefixed = market + symbol
    d = lambda value: value.strftime("%Y%m%d")
    import akshare as ak
    unlock_diagnostics = ""

    def unlocks() -> list[dict[str, Any]]:
        nonlocal unlock_diagnostics
        frame = ak.stock_restricted_release_queue_em(symbol=symbol)
        rows = normalize_unlocks(frame, as_of, int(job.get("unlock_days", 180)))
        dates = [day for value in frame.get("解禁时间", []) if (day := _date(value)) is not None]
        date_range = f"{min(dates).isoformat()} 至 {max(dates).isoformat()}" if dates else "无"
        unlock_diagnostics = f"上游原始 {len(frame)} 行；原始有效日期范围：{date_range}；窗口内 {len(rows)} 行。"
        if frame.empty:
            unlock_diagnostics += "上游本次未返回该股票解禁批次，并非日期过滤所致。"
        elif not rows:
            unlock_diagnostics += "上游有记录，但可识别日期均在本次查询窗口外。"
        invalid_dates = len(frame) - len(dates)
        if invalid_dates:
            unlock_diagnostics += f"另有 {invalid_dates} 行日期无法解析，未纳入窗口。"
        return rows

    def generic(name: str, required: tuple[str, ...], **kwargs: Any) -> list[dict[str, Any]]:
        frame = getattr(ak, name)(**kwargs)
        if frame.empty:
            return []
        _require(frame, required)
        return _frame_records(frame)

    def xueqiu() -> list[dict[str, Any]]:
        frame = ak.stock_individual_spot_xq(symbol=prefixed.upper(), timeout=timeout)
        _require(frame, ("item", "value"))
        values = dict(zip(frame["item"], frame["value"]))
        stamp = _date(values.get("时间"))
        if stamp is not None and stamp > as_of:
            raise ValueError("雪球快照晚于查询日期，改用历史估值")
        allowed = ("市盈率(TTM)", "市盈率(静)", "市盈率(动)", "市净率", "股息率(TTM)", "现价", "资产净值/总市值")
        rows = [{"item": item, "value": number(values.get(item)), "date": stamp.isoformat() if stamp else None}
                for item in allowed if number(values.get(item)) is not None]
        if not any(row["item"] in ("市盈率(TTM)", "市盈率(静)", "市净率") for row in rows):
            raise ValueError("雪球未返回有效 PE/PB")
        return rows

    def baidu() -> list[dict[str, Any]]:
        rows, failures = [], []
        for indicator in ("市盈率(TTM)", "市净率"):
            try:
                frame = ak.stock_zh_valuation_baidu(symbol=symbol, indicator=indicator, period="近一年")
                if frame.empty:
                    continue
                _require(frame, ("date", "value"))
                valid = [(day, number(row["value"])) for row in _frame_records(frame)
                         if (day := _date(row["date"])) is not None and day <= as_of]
                valid = [(day, value) for day, value in valid if value is not None]
                if valid:
                    day, value = max(valid, key=lambda pair: pair[0])
                    rows.append({"item": indicator, "value": value, "date": day.isoformat()})
            except Exception as exc:
                failures.append(f"{indicator}: {type(exc).__name__}")
        if not rows and failures:
            raise ValueError("百度估值失败：" + "；".join(failures))
        if failures:
            for row in rows:
                row["数据缺口"] = "；".join(failures)
        return rows

    def attention(name: str) -> list[dict[str, Any]]:
        if name == "stock_hot_rank_detail_em":
            frame = ak.stock_hot_rank_detail_em(symbol=prefixed.upper())
            rows = _dated_records(frame, "时间", as_of - timedelta(days=60), as_of,
                                  {"排名": "rank", "新晋粉丝": "new_fans_ratio", "铁杆粉丝": "loyal_fans_ratio"}, 30)
        else:
            frame = ak.stock_comment_detail_scrd_focus_em(symbol=symbol)
            rows = _dated_records(frame, "交易日", as_of - timedelta(days=60), as_of,
                                  {"用户关注指数": "attention_index"}, 30)
        return [dict(row, type="attention") for row in rows]

    def news() -> list[dict[str, Any]]:
        # pandas 3 defaults to Arrow strings; AKShare's \\u regex is unsupported
        # there. Use pandas' established object string mode within this call only.
        with pd.option_context("future.infer_string", False):
            frame = ak.stock_news_em(symbol=symbol)
        return _dated_records(frame, "发布时间", announcement_start, as_of,
                              {"新闻标题": "title", "新闻内容": "content", "新闻链接": "url", "文章来源": "publisher"}, 20)

    def cninfo_info() -> list[dict[str, Any]]:
        rows = generic("stock_profile_cninfo", ("公司名称", "A股简称", "所属行业"), symbol=symbol)
        if not rows:
            return []
        mapping = {"公司名称": "公司名称", "A股代码": "股票代码", "A股简称": "股票简称",
                   "所属行业": "行业", "所属市场": "所属市场", "上市日期": "上市时间", "官方网站": "官方网站"}
        return [{"item": target, "value": rows[0].get(source)} for source, target in mapping.items()]

    candidates: dict[str, list[_Candidate]] = {
        "profile": [
            _Candidate("stock_zyjs_ths", f"https://basic.10jqka.com.cn/new/{symbol}/operate.html",
                       lambda: generic("stock_zyjs_ths", ("主营业务",), symbol=symbol)),
            _Candidate("stock_profile_cninfo", "https://webapi.cninfo.com.cn/#/company",
                       lambda: generic("stock_profile_cninfo", ("公司名称", "主营业务"), symbol=symbol)),
        ],
        "info": [_Candidate("stock_individual_info_em", f"https://quote.eastmoney.com/{prefixed}.html",
                            lambda: generic("stock_individual_info_em", ("item", "value"), symbol=symbol, timeout=timeout),
                            "总市值与流通市值单位元、股本单位股；行情快照以来源更新时间为准。"),
                 _Candidate("stock_profile_cninfo", "https://webapi.cninfo.com.cn/#/company", cninfo_info,
                            "巨潮公司资料；仅公司名称、行业、上市信息等静态资料，不含实时市值。")],
        "valuation": [
            _Candidate("stock_individual_spot_xq", f"https://xueqiu.com/S/{prefixed.upper()}", xueqiu,
                       "PE 分别保留 TTM/静态/动态口径；PB 为市净率；未经行业与历史比较不判断便宜。"),
            _Candidate("stock_zh_valuation_baidu", f"https://gushitong.baidu.com/stock/ab-{symbol}", baidu,
                       "百度估值历史最新可用值；市盈率(TTM) 与市净率，数值为倍；日期为数据日期。"),
        ],
        "financial": [
            _Candidate("stock_financial_analysis_indicator_em", f"https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?code={prefixed.upper()}#/cwfx",
                       lambda: normalize_financial(ak.stock_financial_analysis_indicator_em(symbol=f"{symbol}.{market.upper()}", indicator="按报告期"), as_of),
                       "最近8个已公告报告期；金额元、百分比%；累计同比不等于全年同比；现金流为每股经营现金流（元/股）。"),
            _Candidate("stock_financial_abstract_ths", f"https://basic.10jqka.com.cn/new/{symbol}/finance.html",
                       lambda: normalize_financial(ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期"), as_of),
                       "最近8期；同花顺未提供公告日期，历史日期查询不能保证当时已公开；累计同比不等于全年同比；现金流为每股经营现金流。"),
        ],
        "price": [
            _Candidate(name, url,
                       lambda name=name: normalize_price(getattr(ak, name)(symbol=symbol if name == "stock_zh_a_hist" else prefixed,
                                  start_date=d(start), end_date=d(as_of), adjust="qfq",
                                  **({"timeout": timeout} if name != "stock_zh_a_daily" else {})), name, start, as_of),
                       "前复权日线；OHLC单位元/股、成交量统一为股；不拼接不同来源。前复权历史会随分红修订；当日数据可能未完成收盘更新。")
            for name, url in (("stock_zh_a_hist", f"https://quote.eastmoney.com/{prefixed}.html"),
                              ("stock_zh_a_hist_tx", f"https://gu.qq.com/{prefixed}/gp"),
                              ("stock_zh_a_daily", f"https://finance.sina.com.cn/realstock/company/{prefixed}/nc.shtml"))
        ],
        "announcements": [_Candidate("stock_zh_a_disclosure_report_cninfo", "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
                          lambda: _dated_records(ak.stock_zh_a_disclosure_report_cninfo(symbol=symbol, market="沪深京",
                                start_date=d(announcement_start), end_date=d(as_of)), "公告时间", announcement_start, as_of,
                                {"公告标题": "title", "公告链接": "url"}, 120),
                          "按公告日期筛选；公告列表返回上限120条；正文仅尝试最多2篇重点公告。")],
        "unlocks": [_Candidate("stock_restricted_release_queue_em", f"https://data.eastmoney.com/dxf/q/{symbol}.html",
                          unlocks,
                          "含查询当日至未来窗口的解禁批次；数量为股、市值为元，AKShare已换算，勿再次乘万；_pct字段为百分数（原始占比×100）。解禁计划可能调整。")],
        "sentiment": [_Candidate(name, f"https://guba.eastmoney.com/rank/stock?code={symbol}", lambda name=name: attention(name),
                          "仅市场关注度/人气代理，不是正负情绪；本接口未读取或分析股吧原帖；粉丝占比为0~1比值，排名越小关注越高。")
                          for name in ("stock_hot_rank_detail_em", "stock_comment_detail_scrd_focus_em")],
        "news": [_Candidate("stock_news_em", f"https://so.eastmoney.com/news/s?keyword={symbol}", news,
                            "近期新闻搜索结果可能混入关键词同名内容，不保证穷尽；新闻与股吧原帖不同。")],
    }
    preferred = options.get("preferred_source", "auto")
    selected = candidates[key] if preferred == "auto" else [candidate for candidate in candidates[key]
                                                           if candidate.interface == preferred]
    result = _attempts(key, selected, timeout, as_of=as_of,
                       check_freshness=key == "price" and _needs_current_price(as_of), deadline=deadline)
    if preferred != "auto":
        result.note += f"；按指定来源 {preferred} 查询。"
    if key == "price":
        result.note += f"；查询窗口：{start.isoformat()} 至 {as_of.isoformat()}。"
    elif key in {"announcements", "news"}:
        result.note += f"；查询窗口：{announcement_start.isoformat()} 至 {as_of.isoformat()}。"
    elif key == "unlocks":
        result.note += f"；解禁窗口：{as_of.isoformat()} 至 {(as_of + timedelta(days=int(job.get('unlock_days', 180)))).isoformat()}。"
        if result.status in {"ok", "empty"} and unlock_diagnostics:
            result.note += unlock_diagnostics
        result.note += "仅代表该来源本次查询，空结果不等于不存在解禁或减持风险。"
    # Keep the list request within its API deadline; PDF enrichment has a separate
    # 12-second budget and must never discard a successfully fetched list.
    if key == "announcements" and result.status == "ok":
        from .announcements import enrich_announcements
        result.records = enrich_announcements(result.records, total_timeout=max(0, min(12, deadline - time.monotonic())))
        read = sum(bool(row.get("content_excerpt")) for row in result.records)
        result.note += (f"；已提取{read}篇公告文字，其余{len(result.records) - read}篇仅标题级。"
                        "每篇最多前8页、8000字符；content_scope标明全文或摘录；未提取不代表无风险。")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    job: dict[str, Any] = {}
    try:
        job = json.load(sys.stdin)
        result = fetch_source(job)
    except Exception as exc:
        key = str(job.get("key", "unknown"))
        result = SourceResult(key, LABELS.get(key, key), "AKShare", DOC_URL,
                              error=f"{type(exc).__name__}: {str(exc)[:280]}")
    args.output.write_text(json.dumps(_safe(result.to_dict()), ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
