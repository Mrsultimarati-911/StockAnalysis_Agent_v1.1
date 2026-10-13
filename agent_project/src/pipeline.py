from __future__ import annotations

import json
from pathlib import Path
import traceback
import uuid
from typing import Callable, NamedTuple
from html import escape

from .analysis import analyze_company, analyze_risk
from .config import MAX_MODEL_CALLS, MAX_TOOL_CALLS, Settings
from .llm import synthesize
from .models import normalize_symbol, now_china, number, report_basename
from .provider import collect
from .report import render_log, render_report
from .runlog import runtime_environment
from .technical import analyze_technical


class SavedReport(NamedTuple):
    result_path: Path
    log_path: Path
    chart_path: Path
    json_path: Path


def _chart_diagnostic(error: Exception) -> dict:
    """Keep actionable metadata without recording raw exceptions or inputs."""
    if isinstance(error, ImportError) or isinstance(error.__cause__, ImportError):
        code, reason = "dependency_unavailable", "离线绘图依赖不可用，请在运行环境安装 requirements.txt。"
    elif isinstance(error, ValueError):
        code, reason = "chart_value_error", "图表参数或数据格式不兼容，请结合绘图库版本检查。"
    elif isinstance(error, TypeError):
        code, reason = "chart_type_error", "图表数据类型或绘图库接口不兼容。"
    else:
        code, reason = "chart_render_error", "离线图表渲染发生异常，已有研究证据保留。"
    environment = {key: value for key, value in runtime_environment().items() if key != "python_executable"}
    diagnostic = dict(component="offline_chart", error_class=type(error).__name__,
                      reason_code=code, reason=reason, **environment)
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        if Path(frame.filename).name == "charts.py":
            diagnostic["location"] = {"file": "charts.py", "line": frame.lineno, "function": frame.name}
            break
    return diagnostic


def run_analysis(symbol: str, cost: float | None = None, use_llm: bool = True,
                 refresh: bool = False, settings: Settings | None = None,
                 progress: Callable[[str, int, int], None] | None = None,
                 event_sink: Callable[[dict], None] | None = None) -> dict:
    symbol = normalize_symbol(symbol)
    settings = settings or Settings.load()
    if cost is not None:
        parsed = number(cost)
        if parsed is None or parsed <= 0:
            raise ValueError("持仓成本必须是大于零的有效数字。")
        cost = parsed
    if use_llm:
        from .agent import run_agent
        if event_sink is None:
            return run_agent(symbol, cost, settings, refresh=refresh, progress=progress)
        return run_agent(symbol, cost, settings, refresh=refresh, progress=progress, event_sink=event_sink)
    fetched = collect(symbol, settings, refresh, progress)
    sources = {key: result.to_dict() for key, result in fetched.items()}
    generated = now_china().isoformat(timespec="seconds")
    technical = analyze_technical(sources.get("price", {}).get("records", []), cost, generated)
    company = analyze_company(sources, symbol)
    risk = analyze_risk(sources, company, technical)
    bundle = dict(schema_version="2.0", symbol=symbol, generated_at=generated, cost=cost, demo=False,
                  company=company, risk=risk, technical=technical, sources=sources,
                  llm=dict(status="disabled", markdown="", sentiment=None, error="已选择仅规则分析。"),
                  agent=dict(enabled=False, status="disabled", model_calls=0, max_model_calls=MAX_MODEL_CALLS,
                             tool_calls=0, max_tool_calls=MAX_TOOL_CALLS, trace=[], remaining_gaps=[],
                             stop_code="disabled", stop_reason="已选择固定流程规则分析，不请求模型。", elapsed_seconds=0))
    result_markdown, log_markdown = render_report(bundle), render_log(bundle)
    bundle.update(report_markdown=result_markdown, log_markdown=log_markdown)
    return bundle


def save_report(bundle: dict, settings: Settings | None = None) -> SavedReport:
    """Save result, log, offline chart and evidence without overwriting earlier runs."""
    settings = settings or Settings.load()
    stem = report_basename(bundle["symbol"], bundle["generated_at"])
    from .charts import render_chart
    # Re-render legacy bundles so an old combined Markdown never becomes result.
    result_markdown, log_markdown = render_report(bundle), render_log(bundle)
    try:
        chart_html = render_chart(bundle, settings)
        bundle.pop("export_warnings", None)
        bundle.pop("export_diagnostics", None)
    except Exception as error:
        # A renderer error must not discard a completed, possibly paid study.
        message = "离线图表未能生成有效图形；分析报告和原始行情证据已保留，可修复后重新导出，无需重新请求模型。"
        bundle["export_warnings"] = [message]
        diagnostic = _chart_diagnostic(error)
        bundle["export_diagnostics"] = [diagnostic]
        log_markdown += ("\n## 导出状态\n\n" + message + "\n\n"
                         f"绘图异常类别：{diagnostic['error_class']}；原因：{diagnostic['reason']}\n\n"
                         f"运行环境：Python {diagnostic['python_version']}，Plotly {diagnostic['plotly_version']}。\n")
        chart_html = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                      "<title>图表未完成</title></head><body><h1>" + escape(bundle["symbol"]) +
                      " 图表未完成</h1><p>" + message + "</p></body></html>")
    bundle.update(report_markdown=result_markdown, log_markdown=log_markdown, report_layout="split-v1")
    json_text = json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False)
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    directory = settings.report_dir
    while True:
        result_path = directory / f"{stem}_result.md"
        log_path = directory / f"{stem}_log.md"
        chart_path = directory / f"{stem}_chart.html"
        json_path = directory / f"{stem}.json"
        lock_path = directory / f".{stem}.saving"
        try:
            lock = lock_path.open("x", encoding="utf-8")
        except FileExistsError:
            lock = None
        if lock is not None:
            if not any(path.exists() for path in (result_path, log_path, chart_path, json_path)):
                break
            lock.close()
            lock_path.unlink()
        # Preserve the requested filenames; isolate only a colliding run.
        directory = settings.report_dir / f"{stem}_{uuid.uuid4().hex}"
        directory.mkdir(exist_ok=False)
    temporary = []
    published = []
    try:
        documents = ((result_path, bundle["report_markdown"]), (log_path, bundle["log_markdown"]),
                     (chart_path, chart_html), (json_path, json_text))
        for destination, content in documents:
            pending = directory / f".{destination.name}.{uuid.uuid4().hex}.tmp"
            temporary.append(pending)
            with pending.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        # JSON becomes visible last, after both Markdown files and HTML exist.
        for pending, (destination, _) in zip(temporary, documents):
            pending.rename(destination)
            published.append(destination)
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
        lock.close()
        lock_path.unlink(missing_ok=True)
    return SavedReport(result_path, log_path, chart_path, json_path)
