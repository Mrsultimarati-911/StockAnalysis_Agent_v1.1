"""Daily-bar technical observations and conditional plans (all prices are qfq)."""
from __future__ import annotations

from datetime import datetime, time
import math
from typing import Any

import numpy as np
import pandas as pd

from .models import CHINA_TZ, number


def _analysis_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(CHINA_TZ)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("as_of 应为 ISO 日期或日期时间，例如 2026-10-12T15:30:00+08:00。") from exc
    if len(value.strip()) == 10:
        parsed = datetime.combine(parsed.date(), time(15))
    return parsed.replace(tzinfo=CHINA_TZ) if parsed.tzinfo is None else parsed.astimezone(CHINA_TZ)


def _price(value: Any) -> float | None:
    value = number(value)
    return round(value, 4) if value is not None else None


def _wilder(values: pd.Series, period: int = 14) -> pd.Series:
    """Seed with the first simple mean, then apply Wilder smoothing."""
    result = pd.Series(np.nan, index=values.index, dtype=float)
    valid = values.dropna()
    if len(valid) < period:
        return result
    previous = float(valid.iloc[:period].mean())
    result.loc[valid.index[period - 1]] = previous
    for idx, value in valid.iloc[period:].items():
        previous = (previous * (period - 1) + float(value)) / period
        result.loc[idx] = previous
    return result


def _levels(candidates: list[tuple[str, float]], close: float, support: bool) -> list[dict]:
    valid = [(label, price) for label, price in candidates
             if math.isfinite(price) and price > 0 and (price < close if support else price > close)]
    valid.sort(key=lambda pair: pair[1], reverse=support)
    levels: list[dict] = []
    for label, price in valid:
        rounded = _price(price)
        if rounded is None or not (0 < rounded < close if support else rounded > close):
            continue
        match = next((item for item in levels if abs(item["price"] - rounded) / rounded < 0.003), None)
        if match:
            match["label"] += " / " + label
        else:
            levels.append({"label": label, "price": rounded})
    return levels


_SERIES_COLUMNS = ("date", "open", "high", "low", "close", "volume", "ma5", "ma10", "ma20", "ma60",
                   "ma120", "macd_dif", "macd_dea", "macd_hist", "rsi14", "atr14", "volume_ratio20")


def _empty_series() -> pd.DataFrame:
    return pd.DataFrame(columns=_SERIES_COLUMNS)


def technical_series(records: list[dict], as_of: str | None = None) -> tuple[pd.DataFrame, dict]:
    """Clean completed qfq bars and compute indicators once for analysis and charts.

    All indicators are warmed up on the complete cleaned sequence. Missing data
    stays missing. The returned metadata describes the same cutoff and warnings
    used by ``analyze_technical``; callers may slice rows only after this function.
    """
    moment = _analysis_time(as_of)
    warnings = ["点位来自前复权日线；下单前核对实时价格、复权基准及涨跌停限制。"]
    metadata: dict[str, Any] = {
        "available": False, "as_of": moment.isoformat(timespec="seconds"),
        "data_as_of": None, "last_close": None, "adjustment": "qfq",
        "sample_size": 0, "is_stale": False, "actionable": False, "warnings": warnings,
    }
    rows: list[dict] = []
    invalid_dates = 0
    for row in records or []:
        if not isinstance(row, dict):
            invalid_dates += 1
            continue
        try:
            day = pd.Timestamp(row.get("date"))
            if pd.isna(day):
                raise ValueError("missing date")
            if day.tzinfo is not None:
                day = day.tz_convert(CHINA_TZ)
            rows.append({**row, "date": day.date()})
        except (ValueError, TypeError, OverflowError):
            invalid_dates += 1
    if invalid_dates:
        warnings.append(f"已忽略 {invalid_dates} 条无效日期记录。")
    if not rows:
        warnings.append("没有可用的历史行情，无法计算技术指标与点位。")
        return _empty_series(), metadata

    frame = pd.DataFrame(rows)
    complete = frame["date"].map(lambda day: day < moment.date() or
                                (day == moment.date() and moment.time() >= time(15)))
    excluded = int((~complete).sum())
    frame = frame.loc[complete].copy()
    if excluded:
        warnings.append(f"已排除 {excluded} 条未来或当日收盘前尚未完成的日K记录。")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame.get(column, pd.Series(index=frame.index, dtype=float)), errors="coerce")
        frame[column] = frame[column].replace([np.inf, -np.inf], np.nan)
    valid_prices = (frame[["open", "high", "low", "close"]].notna().all(axis=1)
                    & (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
                    & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
                    & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1)))
    invalid_prices = int((~valid_prices).sum())
    frame = frame.loc[valid_prices].copy()
    if invalid_prices:
        warnings.append(f"已忽略 {invalid_prices} 条缺失或不符合 OHLC 关系的价格记录；未插值。")
    duplicate_count = int(frame.duplicated("date", keep="last").sum())
    frame = frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    if duplicate_count:
        warnings.append(f"已合并 {duplicate_count} 条重复交易日，采用输入中最后一条有效记录。")
    zero_volume = frame["volume"].eq(0)
    suspended_tail = bool(len(frame) and zero_volume.iloc[-1])
    if zero_volume.any():
        warnings.append(f"已排除 {int(zero_volume.sum())} 条成交量为零的记录，可能为停牌或无效行情。")
        frame = frame.loc[~zero_volume].reset_index(drop=True)
    frame.loc[frame["volume"] < 0, "volume"] = np.nan
    if frame.empty:
        warnings.append("清洗后没有已完成且有效的日线，无法计算技术指标与点位。")
        return _empty_series(), metadata
    if frame["volume"].isna().any():
        warnings.append("部分成交量缺失或无效；相关量能指标不足时不计算，不将缺失量视为零。")

    size = len(frame)
    close = frame["close"]
    last = float(close.iloc[-1])
    last_day = frame["date"].iloc[-1]
    age_days = (moment.date() - last_day).days
    stale = age_days > 7 or suspended_tail
    metadata.update(available=True, data_as_of=last_day.isoformat(), last_close=_price(last),
                  sample_size=size, is_stale=stale, actionable=size >= 20 and not stale)
    if stale:
        warnings.append(f"行情可能陈旧或停牌：最后有效日线为 {last_day.isoformat()}，距基准日 {age_days} 天；"
                        "假期也可能造成间隔，需核对交易日历与停复牌公告，暂停生成进场情景。")
    if size < 60:
        warnings.append(f"仅有 {size} 根有效日线，不足 60 根；中期趋势与 60 日支撑阻力不可用。")
    elif size < 120:
        warnings.append(f"仅有 {size} 根有效日线，120 日均线不可用。")

    moving = {period: close.rolling(period, min_periods=period).mean() for period in (5, 10, 20, 60, 120)}
    dif = close.ewm(span=12, adjust=False, min_periods=12).mean() - close.ewm(span=26, adjust=False, min_periods=26).mean()
    dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
    histogram = 2 * (dif - dea)
    changes = close.diff()
    gains, losses = _wilder(changes.clip(lower=0)), _wilder(-changes.clip(upper=0))
    rsi = 100 - 100 / (1 + gains / losses)
    rsi.loc[(gains == 0) & (losses == 0)] = 50
    previous_close = close.shift(1)
    true_range = pd.concat([frame["high"] - frame["low"], (frame["high"] - previous_close).abs(),
                            (frame["low"] - previous_close).abs()], axis=1).max(axis=1)
    atr = _wilder(true_range)
    avg_volume = frame["volume"].shift(1).rolling(20, min_periods=20).mean()
    volume_ratio = frame["volume"] / avg_volume.replace(0, np.nan)
    for period, values in moving.items():
        frame[f"ma{period}"] = values
    for column, values in (("macd_dif", dif), ("macd_dea", dea), ("macd_hist", histogram),
                           ("rsi14", rsi), ("atr14", atr), ("volume_ratio20", volume_ratio)):
        frame[column] = values
    return frame, metadata


def analyze_technical(records: list[dict], cost: float | None = None,
                      as_of: str | None = None) -> dict:
    """Analyze completed normalized qfq daily bars with shared indicator series.

    Naive timestamps use Asia/Shanghai; a date-only as_of means that day's close.
    Today's bar is excluded before 15:00. Missing/invalid observations are disclosed,
    never filled. No trading calendar or real-time quote is implied by this function.
    """
    frame, metadata = technical_series(records, as_of)
    output: dict[str, Any] = {**metadata, "warnings": list(metadata["warnings"]),
        "indicators": {}, "trend": "数据不足", "signals": [], "supports": [],
        "resistances": [], "entry_scenarios": [], "holding": {},
    }
    warnings = output["warnings"]
    if frame.empty:
        return output
    size = len(frame)
    close = frame["close"]
    last = float(close.iloc[-1])
    moving = {period: frame[f"ma{period}"] for period in (5, 10, 20, 60, 120)}
    dif, dea, histogram = (frame[column] for column in ("macd_dif", "macd_dea", "macd_hist"))
    rsi, atr, volume_ratio = (frame[column] for column in ("rsi14", "atr14", "volume_ratio20"))
    indicators = {f"ma{period}": _price(values.iloc[-1]) for period, values in moving.items()}
    indicators.update(macd_dif=_price(dif.iloc[-1]), macd_dea=_price(dea.iloc[-1]),
                      macd_hist=_price(histogram.iloc[-1]), rsi14=_price(rsi.iloc[-1]),
                      atr14=_price(atr.iloc[-1]), volume_ratio20=_price(volume_ratio.iloc[-1]),
                      support20=None, resistance20=None, support60=None, resistance60=None)
    output["indicators"] = indicators
    ma20, ma60 = indicators["ma20"], indicators["ma60"]
    if ma20 is not None and ma60 is not None:
        output["trend"] = "中短期上行" if last > ma20 > ma60 else "中短期下行" if last < ma20 < ma60 else "区间震荡或趋势分歧"
    elif ma20 is not None:
        output["trend"] = "短期偏强，中期数据不足" if last > ma20 else "短期偏弱，中期数据不足"

    signals = output["signals"]
    if len(histogram.dropna()) >= 2:
        current_hist, prev_hist = float(histogram.iloc[-1]), float(histogram.iloc[-2])
        if prev_hist <= 0 < current_hist:
            signals.append("MACD 出现金叉，需结合价格结构和成交量确认，不能单独作为买入依据。")
        elif prev_hist >= 0 > current_hist:
            signals.append("MACD 出现死叉，关注动能转弱与支撑是否失守。")
        else:
            signals.append("MACD 红柱扩大，短期动能增强。" if current_hist > max(0, prev_hist) else
                           "MACD 绿柱扩大，短期下行动能增强。" if current_hist < min(0, prev_hist) else
                           "MACD 动能收敛或方向未明确，等待价格确认。")
    else:
        warnings.append("MACD 需要至少 34 根有效日线完成基础预热，当前有效信号不足。")
    if indicators["rsi14"] is not None:
        if indicators["rsi14"] >= 70:
            signals.append("RSI14 处于高位，需留意追涨后的回撤；强趋势中也可能持续高位。")
        elif indicators["rsi14"] <= 30:
            signals.append("RSI14 处于低位，需等待止跌确认，低位本身不代表适合买入。")
    if indicators["volume_ratio20"] is not None:
        ratio = indicators["volume_ratio20"]
        signals.append(f"当日成交量为此前 20 个交易日均量的 {ratio:.2f} 倍。")
    if size >= 21:
        prior_high = float(frame["high"].iloc[-21:-1].max())
        prior_low = float(frame["low"].iloc[-21:-1].min())
        if last > prior_high:
            signals.append("收盘价突破此前 20 日最高价，关注后续放量与回踩确认，警惕假突破。")
        elif last < prior_low:
            signals.append("收盘价跌破此前 20 日最低价，原有区间支撑已失效，避免直接抄底。")
    candle = frame.iloc[-1]
    body = abs(float(candle["close"] - candle["open"]))
    candle_range = float(candle["high"] - candle["low"])
    lower_wick = float(min(candle["open"], candle["close"]) - candle["low"])
    if candle_range > 0 and lower_wick > max(body * 2, candle_range * 0.5):
        signals.append("最近一根日K有较长下影线，存在日内承接迹象，仍需后续收盘确认。")

    support_candidates, resistance_candidates = [], []
    for period in (20, 60):
        if size >= period:
            low, high = float(frame["low"].iloc[-period:].min()), float(frame["high"].iloc[-period:].max())
            indicators[f"support{period}"] = _price(low)
            indicators[f"resistance{period}"] = _price(high)
            support_candidates.append((f"近 {period} 个交易日低点", low))
            resistance_candidates.append((f"近 {period} 个交易日高点", high))
        if indicators[f"ma{period}"] is not None:
            support_candidates.append((f"{period} 日均线", indicators[f"ma{period}"]))
            resistance_candidates.append((f"{period} 日均线", indicators[f"ma{period}"]))
    output["supports"] = _levels(support_candidates, last, True)
    output["resistances"] = _levels(resistance_candidates, last, False)
    nearest_support = output["supports"][0]["price"] if output["supports"] else None
    nearest_resistance = output["resistances"][0]["price"] if output["resistances"] else None
    volatility = indicators["atr14"]
    if output["actionable"] and volatility is not None:
        buffer = max(volatility * 0.8, last * 0.01)
        if nearest_support and nearest_support > buffer:
            zone_high = nearest_support + min(volatility * 0.35, (last - nearest_support) * 0.75)
            invalidation = nearest_support - buffer
            if _price(invalidation) < _price(nearest_support) <= _price(zone_high) < _price(last):
                output["entry_scenarios"].append({
                    "name": "回踩支撑后确认", "zone_low": _price(nearest_support), "zone_high": _price(zone_high),
                    "invalidation_price": _price(invalidation),
                    "condition": "仅观察区间；等待缩量回踩不破、随后收盘重新转强并有量能配合，再评估仓位与风险。",
                    "invalidation": "收盘跌破参考失效价或出现重大利空时，取消该情景；跳空可能使实际损失超过预期。",
                })
        if nearest_resistance:
            confirmation = nearest_resistance + max(volatility * 0.1, last * 0.002)
            invalidation = nearest_resistance - buffer
            if 0 < _price(invalidation) < _price(nearest_resistance) < _price(confirmation):
                output["entry_scenarios"].append({
                    "name": "突破后回踩确认", "reference_price": nearest_resistance,
                    "confirmation_price": _price(confirmation), "invalidation_price": _price(invalidation),
                    "condition": "观察收盘有效突破并放量，随后回踩原阻力能够守住；单日刺穿不构成确认，避免跳空追价。",
                    "invalidation": "突破后收盘重新跌破参考失效价则视为假突破，取消该情景。",
                })
    if not output["entry_scenarios"]:
        signals.append("当前数据或点位关系不足以形成有效进场观察情景，等待补充数据或结构确认。")
    valid_cost = number(cost)
    if cost is not None and (valid_cost is None or valid_cost <= 0):
        warnings.append("持仓成本无效，已忽略；请输入大于零的实际成本。")
        valid_cost = None
    defense = nearest_support - max(volatility or 0, last * 0.01) if nearest_support else None
    output["holding"] = {
        "cost": valid_cost, "support_watch": output["supports"], "resistance_watch": output["resistances"],
        "invalidation_price": _price(defense) if defense and defense > 0 else None,
        "guidance": "关注收盘是否失守最近支撑、放量下跌与 MACD 转弱是否同时出现；接近阻力时观察量价承接。"
                    "参考失效价用于风险复核，不是保证可成交的止损价。",
        "cost_note": "实际持仓成本可能与前复权价格口径不同，未直接计算盈亏；请核对分红送转和成本口径。" if valid_cost else None,
    }
    warnings.append("技术形态为历史行情的条件观察，无法保证收益；请同时评估公告、财务与流动性风险。")
    return output
