"""Standalone, offline interactive daily-bar charts from the report's evidence."""
from __future__ import annotations

from html import escape
import json
import math
from typing import Any

from .models import number
from .technical import technical_series


_DEFAULT_MA = (5, 10, 20, 60, 120)
_MA_COLORS = ("#e49d24", "#5c73e8", "#d361ad", "#279c9b", "#8a64b7")
_UP, _DOWN = "#dc424a", "#17996d"


def _setting(settings: Any, name: str, default: Any) -> Any:
    return getattr(settings, name, default) if settings is not None else default


def _values(series) -> list[float | None]:
    return [number(value) for value in series]


def _safe_json(value: Any) -> str:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _same_number(left: Any, right: Any) -> bool:
    left, right = number(left), number(right)
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=0, abs_tol=0.000051)


def _chart_payload(bundle: dict, settings: Any = None) -> dict:
    """Keep only the chart's public data, never configuration or research logs."""
    technical = bundle.get("technical") or {}
    generated = str(bundle.get("generated_at") or "未记录")
    cutoff = technical.get("as_of") or bundle.get("generated_at")
    records = (bundle.get("sources") or {}).get("price", {}).get("records") or []
    warnings: list[str] = []
    verified = True
    frame = None
    metadata: dict = {"data_as_of": None, "sample_size": 0, "is_stale": False}
    if not cutoff:
        verified = False
        warnings.append("未核实：缺少报告行情基准时间，未使用打开网页时的日期推断行情。")
    else:
        try:
            frame, metadata = technical_series(records, as_of=str(cutoff))
            warnings.extend(metadata["warnings"])
        except (ValueError, TypeError, OverflowError):
            verified = False
            warnings.append("未核实：报告的行情基准时间或原始日线格式无效，未绘制 K 线。")
    if frame is not None and not frame.empty:
        expected_day = technical.get("data_as_of")
        if not technical.get("available") or not expected_day:
            verified = False
            warnings.append("未核实：报告没有确认有效技术行情，未绘制 K 线和观察点位。")
        elif expected_day != metadata["data_as_of"]:
            verified = False
            warnings.append("未核实：原始行情与报告的行情截止日期不一致，未绘制 K 线和观察点位。")
        elif "last_close" in technical and not _same_number(technical["last_close"], metadata["last_close"]):
            verified = False
            warnings.append("未核实：原始行情与报告收盘价不一致，未绘制 K 线和观察点位。")
        else:
            expected_indicators = technical.get("indicators") or {}
            for column in (*[f"ma{period}" for period in _DEFAULT_MA], "macd_dif", "macd_dea", "macd_hist"):
                if column in expected_indicators and not _same_number(expected_indicators[column], frame[column].iloc[-1]):
                    verified = False
                    warnings.append("未核实：原始行情重新计算的指标与报告不一致，未绘制 K 线和观察点位。")
                    break
    else:
        verified = False
        warnings.append("未核实：没有已完成且有效的日线，未绘制 K 线和观察点位。")
    displayed: dict[str, list] = {}
    if verified and frame is not None:
        limit = int(_setting(settings, "chart_display_bars", 250))
        shown = frame.tail(max(1, limit))
        displayed = {"date": [day.isoformat() for day in shown["date"]]}
        for column in ("open", "high", "low", "close", "volume", *[f"ma{p}" for p in _DEFAULT_MA],
                       "macd_dif", "macd_dea", "macd_hist"):
            displayed[column] = _values(shown[column])
        if metadata["sample_size"] < 34:
            warnings.append("MACD 尚未完成基础预热；缺失指标保持为空，不补零。")
    levels: list[dict] = []
    if verified and _setting(settings, "chart_show_support_resistance", True):
        for key, kind in (("supports", "支撑"), ("resistances", "压力")):
            for row in technical.get(key) or []:
                if not isinstance(row, dict):
                    continue
                price = number(row.get("price"))
                if price is not None and price > 0:
                    levels.append({"kind": kind, "price": price, "basis": str(row.get("label") or "报告观察位")})
    return {"symbol": str(bundle.get("symbol") or "未指定"),
            "name": str((bundle.get("company") or {}).get("name") or ""),
            "generated_at": generated, "as_of": str(cutoff or "未记录"),
            "data_as_of": metadata["data_as_of"], "adjustment": "qfq",
            "sample_size": metadata["sample_size"], "is_stale": metadata["is_stale"],
            "verified": verified, "warnings": list(dict.fromkeys(warnings)),
            "trend": str(technical.get("trend") or "数据不足"),
            "last_close": metadata.get("last_close"), "series": displayed, "levels": levels}


def render_chart(bundle: dict, settings: Any = None) -> str:
    """Return one HTML file containing data and Plotly.js; no network or model use.

    Charts use the report's analysis cutoff and the shared, fully warmed indicator
    sequence. Support/resistance levels come only from the structured report.
    """
    try:
        import plotly.graph_objects as go
        import plotly.io as pio
        from plotly.subplots import make_subplots
    except ImportError as exc:
        raise RuntimeError("离线图表需要 Plotly；请在 src 环境安装 requirements.txt。") from exc

    payload = _chart_payload(bundle, settings)
    series = payload["series"]
    show_volume = bool(_setting(settings, "chart_show_volume", True))
    show_macd = bool(_setting(settings, "chart_show_macd", True))
    titles = ["前复权日 K 与均线 / 支撑压力观察位"]
    heights = [0.62]
    if show_volume:
        titles.append("成交量（股）")
        heights.append(0.16)
    if show_macd:
        titles.append("MACD · 12 / 26 / 9")
        heights.append(0.22)
    figure = make_subplots(rows=len(titles), cols=1, shared_xaxes=True, vertical_spacing=0.075,
                           row_heights=heights, subplot_titles=titles)
    if series:
        dates = series["date"]
        figure.add_trace(go.Candlestick(
            x=dates, open=series["open"], high=series["high"], low=series["low"], close=series["close"],
            name="日K", increasing_line_color=_UP, increasing_fillcolor=_UP,
            decreasing_line_color=_DOWN, decreasing_fillcolor=_DOWN, line_width=1,
            # Candlestick.hovertemplate is absent in earlier supported Plotly 6.x.
            # Explicit per-bar text preserves the same OHLC precision offline.
            hovertext=[f"{escape(str(day))}<br>开盘 {opened:.4f}<br>最高 {high:.4f}"
                       f"<br>最低 {low:.4f}<br>收盘 {close:.4f}<br>前复权 · 元/股"
                       for day, opened, high, low, close in zip(
                           dates, series["open"], series["high"], series["low"], series["close"])],
            hoverinfo="text"),
            row=1, col=1)
        for period in _setting(settings, "chart_ma_periods", _DEFAULT_MA):
            if period not in _DEFAULT_MA:
                continue
            figure.add_trace(go.Scatter(x=dates, y=series[f"ma{period}"], mode="lines", name=f"MA{period}",
                                       connectgaps=False, line={"color": _MA_COLORS[_DEFAULT_MA.index(period)], "width": 1.5},
                                       hovertemplate=f"%{{x}}<br>MA{period} %{{y:.4f}}<extra></extra>"), row=1, col=1)
        for level in payload["levels"]:
            color = "#269c8d" if level["kind"] == "支撑" else "#c77840"
            label = f"{level['kind']} {level['price']:.4f}"
            figure.add_trace(go.Scatter(x=[dates[0], dates[-1]], y=[level["price"], level["price"]],
                mode="lines", name=label, showlegend=False, line={"color": color, "width": 1, "dash": "dash"},
                hovertemplate=f"{escape(label)}<br>{escape(level['basis'])} · 生成时观察位<extra></extra>"), row=1, col=1)
            figure.add_annotation(x=1, xref="x domain", y=level["price"], yref="y", text=label,
                                  showarrow=False, xanchor="left", xshift=6, font={"color": color, "size": 11})
        row = 2
        if show_volume:
            figure.add_trace(go.Bar(x=dates, y=series["volume"], name="成交量", showlegend=False,
                marker_color=[_UP if close >= opened else _DOWN for close, opened in zip(series["close"], series["open"])],
                hovertemplate="%{x}<br>成交量 %{y:,.0f} 股<extra></extra>"), row=row, col=1)
            row += 1
        if show_macd:
            figure.add_trace(go.Bar(x=dates, y=series["macd_hist"], name="MACD柱", showlegend=False,
                marker_color=[_UP if value is not None and value >= 0 else _DOWN for value in series["macd_hist"]],
                hovertemplate="%{x}<br>MACD柱 %{y:.4f}<extra>2 × (DIF − DEA)</extra>"), row=row, col=1)
            for key, name, color in (("macd_dif", "DIF", "#526de0"), ("macd_dea", "DEA", "#e49d24")):
                figure.add_trace(go.Scatter(x=dates, y=series[key], name=name, mode="lines", connectgaps=False,
                    line={"color": color, "width": 1.5}, hovertemplate=f"%{{x}}<br>{name} %{{y:.4f}}<extra></extra>"),
                    row=row, col=1)
            figure.add_hline(y=0, line_width=1, line_color="#d6deeb", row=row, col=1)
    else:
        figure.add_annotation(text="未核实：暂无与报告一致的有效日线可绘图", x=0.5, y=0.55,
                              xref="paper", yref="paper", showarrow=False, font={"size": 17, "color": "#8a5a31"})
    figure.update_layout(template="plotly_white", height=850 if len(titles) == 3 else 680,
                         margin={"l": 65, "r": 155, "t": 85, "b": 50},
                         font={"family": "Arial, sans-serif", "color": "#334155", "size": 12},
                         legend={"orientation": "h", "y": 1.12, "x": 0}, hovermode="x unified",
                         dragmode="zoom", paper_bgcolor="#ffffff", plot_bgcolor="#ffffff", bargap=0.16)
    figure.update_xaxes(type="date", rangeslider_visible=False, showgrid=False,
                        rangebreaks=[{"bounds": ["sat", "mon"]}], showspikes=True,
                        spikemode="across", spikesnap="cursor", spikedash="dot")
    figure.update_yaxes(showgrid=True, gridcolor="#edf1f7", zeroline=False, fixedrange=False)
    figure.update_yaxes(title_text="价格（元/股）", row=1, col=1)
    graph = pio.to_html(figure, include_plotlyjs=True, include_mathjax=False, full_html=False,
                       div_id="stock-chart", config={"responsive": True, "displaylogo": False,
                       "scrollZoom": True, "toImageButtonOptions": {"format": "png", "filename": f"{payload['symbol']}_chart"}})
    title = escape(f"{payload['name']} {payload['symbol']}".strip())
    warnings = "".join(f"<li>{escape(item)}</li>" for item in payload["warnings"])
    level_rows = "".join(f"<tr><td>{escape(item['kind'])}</td><td>{item['price']:.4f}</td><td>{escape(item['basis'])}</td></tr>"
                         for item in payload["levels"])
    levels = ("<table><thead><tr><th>类型</th><th>观察价（元/股）</th><th>依据</th></tr></thead><tbody>" + level_rows + "</tbody></table>"
              if level_rows else "<p class='muted'>暂无已核实的支撑压力观察位。</p>")
    close = f"{payload['last_close']:.4f}" if payload["verified"] and payload["last_close"] is not None else "未核实"
    status = "行情已核验" if payload["verified"] else "行情未核实"
    if payload["is_stale"]:
        status += " · 可能陈旧或停牌"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · 离线行情图表</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fa;color:#233247;font:15px/1.6 Arial,sans-serif}}
main{{max-width:1480px;margin:auto;padding:28px 24px 40px}}h1{{margin:0;font-size:27px;letter-spacing:.4px}}
h2{{margin:0 0 12px;font-size:18px}}.eyebrow{{color:#54739b;font-size:12px;letter-spacing:2px;margin-bottom:6px}}
.meta,.muted{{color:#64748b;font-size:13px}}.meta{{margin:8px 0 20px}}.cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}}
.card{{flex:1;min-width:160px;background:white;border:1px solid #e3eaf3;border-radius:10px;padding:13px 18px}}
.card span{{display:block;color:#64748b;font-size:12px}}.card strong{{font-size:20px;font-weight:600}}
.panel{{background:white;border:1px solid #e3eaf3;border-radius:12px;padding:16px 18px;margin-top:18px;overflow:hidden}}
.chart-panel{{padding:4px 8px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #edf1f7}}
th{{background:#f7f9fc;color:#506176}}ul{{margin:8px 0;padding-left:22px}}li{{margin:5px 0;font-size:13px;color:#657183}}
@media(max-width:650px){{main{{padding:18px 10px}}h1{{font-size:23px}}.card{{min-width:130px}}.panel{{padding:12px 8px}}}}
</style></head><body><main><div class="eyebrow">STOCK AGENT / OFFLINE CHART</div><h1>{title} · 日线观察</h1>
<p class="meta">生成时间：{escape(payload['generated_at'])} · 行情截至：{escape(str(payload['data_as_of'] or '未核实'))} · 前复权 qfq · {escape(status)}</p>
<div class="cards"><div class="card"><span>最近有效收盘价 · 元/股</span><strong>{close}</strong></div>
<div class="card"><span>技术结构</span><strong>{escape(payload['trend']) if payload['verified'] else '未核实'}</strong></div>
<div class="card"><span>完整有效 / 本图日线</span><strong>{payload['sample_size']} / {len(series.get('date', []))}</strong></div></div>
<p class="muted">滚轮缩放，拖动选择区间，双击恢复；点击图例可隐藏或显示指标。此文件是生成时的行情快照，离线打开不会更新行情或调用模型。</p>
<section class="panel chart-panel">{graph}</section><section class="panel"><h2>支撑与压力观察位</h2>
<p class="muted">水平线直接采用本次分析报告的点位与依据，属于生成时观察位，不随鼠标区间重算，不保证成交或未来有效。</p>{levels}</section>
<section class="panel"><h2>口径与数据限制</h2><p class="muted">A 股涨红跌绿；成交量统一为股；均线先在完整有效行情上预热，再截取展示区间。
MACD 参数 12 / 26 / 9，柱值 = 2 × (DIF − DEA)。缺失数据不补零。实际持仓成本可能与前复权价格基准不同，本图不据此标注盈亏。</p><ul>{warnings}</ul></section>
<script id="stock-chart-data" type="application/json">{_safe_json(payload)}</script></main></body></html>"""
