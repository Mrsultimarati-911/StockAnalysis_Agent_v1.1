"""Render analysis results and research logs as separate Markdown documents."""
from __future__ import annotations

from html import escape
import math
import re
from typing import Any
from urllib.parse import urlsplit

from .config import MAX_MODEL_CALLS, MAX_TOOL_CALLS

LABELS = {
    "name": "公司名称", "industry": "所属行业", "business": "主营业务",
    "pe": "市盈率 PE", "pe_ttm": "滚动市盈率 PE(TTM)", "PE(TTM)": "滚动市盈率 PE(TTM)",
    "pe_static": "静态市盈率 PE", "pe_dynamic": "动态市盈率 PE", "pb": "市净率 PB",
    "period": "财务期", "report_date": "报告期", "date": "日期", "revenue": "营业收入",
    "net_profit": "净利润", "revenue_yoy": "营收同比", "net_profit_yoy": "净利润同比",
    "roe": "净资产收益率 ROE", "debt_ratio": "资产负债率", "operating_cash_flow": "经营现金流",
    "ma5": "MA5", "ma10": "MA10", "ma20": "MA20", "ma60": "MA60", "ma120": "MA120",
    "MA5": "MA5", "MA10": "MA10", "MA20": "MA20", "MA60": "MA60",
    "dif": "MACD DIF", "dea": "MACD DEA", "macd": "MACD 柱", "rsi14": "RSI14",
    "volume_ratio": "成交量比", "cost": "持仓成本", "pnl_pct": "参考浮动盈亏(%)",
    "gain_pct": "参考浮动盈亏(%)", "stop_loss": "条件失效参考位", "take_profit": "上方观察位",
    "condition": "触发条件", "action": "观察/应对", "invalidation": "条件失效",
    "trigger": "触发条件", "risk": "风险", "rationale": "依据", "note": "说明",
    "support": "支撑参考位", "resistance": "压力参考位", "stop": "风险观察位",
    "current_profit_pct": "参考浮动盈亏(%)", "market_cap": "总市值", "eps": "每股收益",
    "macd_dif": "MACD DIF", "macd_dea": "MACD DEA", "macd_hist": "MACD 柱（2×(DIF-DEA)）",
    "atr14": "ATR14", "volume_ratio20": "成交量 / 前20日均量", "support20": "20日最低价",
    "resistance20": "20日最高价", "support60": "60日最低价", "resistance60": "60日最高价",
    "zone_low": "观察区间下沿", "zone_high": "观察区间上沿", "reference_price": "压力参考价",
    "confirmation_price": "突破观察价", "invalidation_price": "条件失效参考价",
    "support_watch": "支撑观察位", "resistance_watch": "压力观察位", "guidance": "持仓观察条件",
    "cost_note": "成本口径说明", "label": "依据", "price": "参考价格",
}
STATUS = {"ok": "已取得", "empty": "返回空数据（覆盖不足，不能视为无风险）", "unavailable": "未取得/未核实"}
AGENT_STATUS = {"completed": "研究完成", "limited": "有未决缺口", "error": "研究中断", "disabled": "未启用"}
AGENT_KIND = {"model": "模型请求", "tool": "工具调用", "assessment": "证据检查", "stop": "停止"}
_AGENT_OPTIONS = {"lookback_days", "announcement_days", "unlock_days", "guba_days", "guba_max_posts", "guba_max_pages", "preferred_source"}


def _agent_text(value: Any, limit: int = 500) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [已隐藏]", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _agent_count(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(1_000_000_000, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _agent_arguments(name: str, arguments: Any, kind: str) -> dict:
    """Expose only public, bounded query parameters; never assistant messages."""
    if not isinstance(arguments, dict) or kind != "tool":
        return {}
    result: dict[str, Any] = {}
    if name == "fetch_stock_data":
        for key in ("source", "refresh"):
            if key in arguments:
                result[key] = arguments[key] if isinstance(arguments[key], bool) else _agent_text(arguments[key], 80)
        options = arguments.get("options")
        if isinstance(options, dict):
            result["options"] = {key: value if isinstance(value, (bool, int)) else _agent_text(value, 80)
                                 for key, value in options.items() if key in _AGENT_OPTIONS}
    elif name == "read_announcement" and "url" in arguments:
        url = _safe_url(_agent_text(arguments["url"], 600))
        if url:
            result["url"] = url
    elif name == "inspect_evidence":
        questions = arguments.get("questions")
        if isinstance(questions, list):
            result["questions"] = [_agent_text(value, 1200) for value in questions[:20]]
    elif name == "finish_research":
        for key in ("summary", "stop_reason"):
            if key in arguments:
                result[key] = _agent_text(arguments[key], 1200)
        ids = arguments.get("unresolved_gap_ids")
        if isinstance(ids, list):
            result["unresolved_gap_ids"] = [_agent_text(value, 80) for value in ids[:30]]
        questions = arguments.get("remaining_questions")
        if isinstance(questions, list):
            result["remaining_questions"] = [_agent_text(value, 1200) for value in questions[:20]]
    return result


def agent_view(value: Any) -> dict | None:
    """A credential-free public projection shared by reports and the local UI."""
    if not isinstance(value, dict):
        return None
    try:
        elapsed = float(value.get("elapsed_seconds", 0))
        elapsed = round(max(elapsed, 0), 2) if math.isfinite(elapsed) else 0
    except (TypeError, ValueError, OverflowError):
        elapsed = 0
    trace = []
    for row in (value.get("trace") or [])[:100]:
        if not isinstance(row, dict):
            continue
        kind = _agent_text(row.get("kind"), 30)
        name = _agent_text(row.get("name"), 100)
        arguments = row.get("arguments") or {}
        reason = row.get("reason") or (arguments.get("reason") if isinstance(arguments, dict) else "")
        trace.append({"step": _agent_count(row.get("step")), "kind": kind, "name": name,
                      "arguments": _agent_arguments(name, arguments, kind),
                      "status": _agent_text(row.get("status"), 60), "summary": _agent_text(row.get("summary")),
                      "started_at": _agent_text(row.get("started_at"), 60),
                      "duration_ms": _agent_count(row.get("duration_ms")), "reason": _agent_text(reason, 240)})
    gaps = [{key: _agent_text(row.get(key), 300 if key in {"description", "suggested_action"} else 80)
             for key in ("id", "category", "description", "source", "status", "suggested_action")}
            for row in (value.get("remaining_gaps") or [])[:40] if isinstance(row, dict)]
    return {"enabled": bool(value.get("enabled")), "status": _agent_text(value.get("status"), 40),
            "model_calls": _agent_count(value.get("model_calls")),
            "max_model_calls": _agent_count(value.get("max_model_calls"), MAX_MODEL_CALLS),
            "tool_calls": _agent_count(value.get("tool_calls")),
            "max_tool_calls": _agent_count(value.get("max_tool_calls"), MAX_TOOL_CALLS),
            "stop_reason": _agent_text(value.get("stop_reason")),
            "stop_code": _agent_text(value.get("stop_code"), 80), "remaining_gaps": gaps,
            "trace": trace, "elapsed_seconds": elapsed}


def _agent_section(value: Any) -> list[str]:
    agent = agent_view(value)
    if agent is None:
        return []
    lines = ["## Agent 自主研究过程", "",
             f"状态：{AGENT_STATUS.get(agent['status'], _cell(agent['status']))}；"
             f"模型请求 {agent['model_calls']}/{agent['max_model_calls']} 次；"
             f"受控工具调用 {agent['tool_calls']}/{agent['max_tool_calls']} 次；"
             f"研究用时 {agent['elapsed_seconds']} 秒。", "",
             f"停止原因：{_cell(agent['stop_reason'])}", "",
             "### 仍待核实", ""]
    gaps = [{"编号": row["id"], "类别": row["category"], "待核实事项": row["description"],
             "来源": row["source"], "建议下一步": row["suggested_action"]} for row in agent["remaining_gaps"]]
    lines += _table(gaps, "本轮未保留新的明确缺口；这不代表已覆盖全部风险。" if agent["enabled"] else "未运行自主补查；以本地规则报告和来源状态为准。")
    lines += ["", "### 调用记录", "", "仅记录动作、结果与简短补查目的；参数限定为受控工具的公开查询字段。", ""]
    rows = [{"步骤": row["step"], "类型": AGENT_KIND.get(row["kind"], row["kind"]),
             "动作": row["name"], "状态": row["status"], "补查目的": row["reason"],
             "结果": row["summary"], "查询参数": row["arguments"] or "—",
             "开始时间": row["started_at"], "耗时(ms)": row["duration_ms"]} for row in agent["trace"]]
    lines += _table(rows, "没有调用记录。")
    return lines + [""]


def _text(value: Any) -> str:
    if value is None or value == "":
        return "未核实"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return "；".join(_text(v) for v in value) or "未核实"
    if isinstance(value, dict):
        return "；".join(f"{LABELS.get(k, k)}：{_text(v)}" for k, v in value.items()) or "未核实"
    return str(value)


def _cell(value: Any) -> str:
    return escape(_text(value), quote=False).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _safe_url(value: Any) -> str:
    url = str(value or "")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc or any(ord(c) < 32 for c in url):
        return ""
    for original, encoded in (("(", "%28"), (")", "%29"), (" ", "%20"), ("<", "%3C"), (">", "%3E"), ('"', "%22")):
        url = url.replace(original, encoded)
    return url


def _link(label: Any, url: Any) -> str:
    label = _cell(label).replace("[", "\\[").replace("]", "\\]")
    safe = _safe_url(url)
    return f"[{label}]({safe})" if safe else label


def _bullets(values: Any, fallback: str = "暂无可核实信息。") -> list[str]:
    if not values:
        return [f"- {fallback}"]
    if isinstance(values, str):
        values = [values]
    return [f"- {_cell(v)}" for v in values]


def _table(rows: Any, empty: str) -> list[str]:
    if not rows:
        return [empty]
    if isinstance(rows, dict):
        rows = [{"项目": LABELS.get(k, k), "内容": v} for k, v in rows.items()]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return _bullets(rows)
    keys = list(dict.fromkeys(k for row in rows for k in row))
    if not keys:
        return [empty]
    lines = ["| " + " | ".join(_cell(LABELS.get(k, k)) for k in keys) + " |",
             "| " + " | ".join("---" for _ in keys) + " |"]
    lines.extend("| " + " | ".join(_cell(row.get(k)) for k in keys) + " |" for row in rows)
    return lines


def _markdown_heading_kind(title: str) -> tuple[str, str]:
    title = re.sub(r"[*_`]+", "", title).strip()
    compact = re.sub(r"\s+", "", title).lower()
    bare = re.sub(r"^(?:[一二三四五六七八九十百\d]+[、.．:：)）-])+", "", compact)
    markers = ("日志", "研究过程", "取证过程", "调用记录", "调用明细", "工具调用", "执行过程",
               "数据来源", "来源说明", "来源与引用", "股吧采样说明", "证据缺口", "信息缺口", "数据缺口", "剩余缺口",
               "未解决问题", "未决问题", "未决事项", "未知事项", "仍待核实", "待核实事项",
               "模型用量", "模型核验", "模型验证", "停止原因", "模型预算", "调用预算", "工具预算", "费用预算", "请求预算", "token", "researchlog",
               "workflow", "toolcalls", "evidencegaps", "datasources", "sourcesandlimitations")
    is_log = any(marker in compact for marker in markers) or bare in {"未知", "未知信息", "待核实", "来源", "sources", "log"}
    if not is_log:
        return "analysis", title
    # A mixed parent such as "风险与证据缺口" must not swallow its risk children.
    parts = re.split(r"与|及|和|、|/|&|[（()）—–:：]", title)
    analysis_parts = [part.strip() for part in parts
                      if any(word in part for word in ("风险", "公司", "基本面", "财务", "技术", "股价", "股吧", "情绪", "估值", "研判", "结论", "持仓", "进场", "仓位", "止损"))
                      and not any(marker in re.sub(r"\s+", "", part).lower() for marker in markers)]
    if len(parts) > 1 and analysis_parts:
        return "mixed", "、".join(analysis_parts)
    if any(word in title for word in ("风险", "仓位", "止损")):
        # Risk caveats are analysis even when their heading also mentions a gap.
        # Keep the entire body rather than guessing which risk sentences to drop.
        return "mixed", title
    return "log", title


def split_analysis_markdown(markdown: str) -> tuple[str, str]:
    """Route explicit log/gap sections by heading, preserving adjacent risk analysis."""
    lines = str(markdown or "").splitlines()
    output: dict[str, list[str]] = {"analysis": [], "log": []}
    stack: list[tuple[int, str]] = []
    atx_level = 0
    fence: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        route = stack[-1][1] if stack else "analysis"
        fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            output[route].append(line)
            if fence_match and fence_match[1][0] == fence[0] and len(fence_match[1]) >= fence[1]:
                fence = None
            index += 1
            continue
        if fence_match:
            fence = fence_match[1][0], len(fence_match[1])
            output[route].append(line)
            index += 1
            continue
        inline_marker = re.match(
            r"^(\s{0,3})(?:(?:\d+[.)、．]|[-+*])\s+)?(?:\*\*|__)(.{1,80}?)(?:\*\*|__)\s*[:：]\s*\S.*$",
            line)
        if inline_marker and _markdown_heading_kind(inline_marker[2])[0] == "log":
            # Inline audit labels are one paragraph/list item, not headings:
            # do not let a gap item change routing of adjacent risk analysis.
            indentation = len(inline_marker[1])
            block = [line]
            index += 1
            while index < len(lines) and lines[index].strip():
                following = lines[index]
                sibling = re.match(r"^(\s*)(?:\d+[.)、．]|[-+*])\s+", following)
                boundary = re.match(r"^\s{0,3}(?:#{1,6}\s|`{3,}|~{3,}|(?:\*\*|__))", following)
                thematic = re.fullmatch(r"\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*", following)
                next_setext = (index + 1 < len(lines)
                               and re.fullmatch(r"\s{0,3}(?:={3,}|-{3,})\s*", lines[index + 1]))
                if (boundary or thematic or next_setext
                        or sibling and len(sibling[1]) <= indentation):
                    break
                block.append(following)
                index += 1
            output["log"].extend(block + [""])
            continue
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        bold = re.match(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*[:：]?\s*$", line)
        setext = (index + 1 < len(lines) and bool(line.strip())
                  and re.fullmatch(r"\s{0,3}(?:={3,}|-{3,})\s*", lines[index + 1]))
        consumed = 1
        original = [line]
        if heading:
            level, title = len(heading[1]), heading[2]
            atx_level = level
        elif setext:
            level, title = (1 if lines[index + 1].strip().startswith("=") else 2), line.strip()
            atx_level = level
            original.append(lines[index + 1])
            consumed = 2
        elif bold and len(bold[1]) <= 80:
            level, title = atx_level + 1, bold[1]
        else:
            output[route].append(line)
            index += 1
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_route = stack[-1][1] if stack else "analysis"
        kind, analysis_title = _markdown_heading_kind(title)
        route = "log" if parent_route == "log" or kind == "log" else "analysis"
        stack.append((level, route))
        if kind == "mixed" and route == "analysis":
            output["log"].extend(original + [""])
            rendered = [f"{'#' * min(level, 6)} {analysis_title}"]
        else:
            rendered = original
        output[route].extend(rendered)
        index += consumed
    return tuple("\n".join(output[key]).strip() for key in ("analysis", "log"))


def _source_section(sources: dict) -> list[str]:
    lines = ["## 数据来源与未知事项", "", "| 来源 | 状态 | 记录数 | 采集时间 | 接口与说明 |", "| --- | --- | --- | --- | --- |"]
    for key, source in sources.items():
        note = "；".join(str(source.get(k) or "") for k in ("interface", "note", "error") if source.get(k))
        if source.get("cached"):
            note += "；缓存数据（采集时间未刷新）"
        lines.append(f"| {_link(source.get('label') or key, source.get('url'))} [source:{_cell(key)}] | {STATUS.get(source.get('status'), '未核实')} | {len(source.get('records') or [])} | {_cell(source.get('fetched_at'))} | {_cell(note)} |")
    if not sources:
        lines += ["| 全部来源 | 未取得/未核实 | 0 | 未核实 | 未提供来源证据 |"]
    missing = [source.get("label") or key for key, source in sources.items() if source.get("status") != "ok"]
    lines += ["", "**未知风险**：空结果、失败、采样缺口和数据时滞都不能解释为无风险。"]
    if missing:
        lines += [f"尚待核实：{_cell(missing)}。"]
    return lines


def _raw_evidence_sections(sources: dict) -> list[str]:
    announcement_rows = sources.get("announcements", {}).get("records") or []
    read_rows = [row for row in announcement_rows if row.get("content_excerpt")]
    lines = ["## 公告正文取证记录", "", f"公告正文：本次 {len(announcement_rows)} 条公告记录中，{len(read_rows)} 条取得正文或节选；其余仅取得标题或读取未成功。", ""]
    for item in read_rows:
        scope = "本次成功提取全文文本" if item.get("content_scope") == "full_text" else "前8页或受字符数限制的节选"
        pdf = _link("公告 PDF 原文", item.get("content_pdf_url") or item.get("url"))
        lines += [f"- {_link(item.get('title'), item.get('url'))}：{scope}，读取 {_cell(item.get('content_pages_read'))}/{_cell(item.get('content_total_pages'))} 页；{pdf}。",
                  f"  正文预览（最多前300字）：{_cell(str(item.get('content_excerpt') or '')[:300])}"]
    if not read_rows:
        lines += ["公告正文尚未取得，标题检索不能替代完整正文审阅。"]
    guba = sources.get("guba") or {}
    lines += ["", "## 股吧采样记录", "", f"采样范围：{_cell(guba.get('note'))}；来源状态：{STATUS.get(guba.get('status'), '未核实')}。", ""]
    posts = guba.get("records") or []
    if posts:
        lines += ["采样原帖（最多展示前 10 条；具体依据以实际标题/正文范围为准）：", ""]
        for post in posts[:10]:
            lines.append(f"- {_cell(post.get('published_at') or post.get('updated_at'))}：{_link(post.get('title'), post.get('url'))}（文本范围：{_cell(post.get('text_scope'))}）")
    else:
        lines += ["尚未取得可核查的股吧原帖。"]
    return lines


def _stored_only_documents(bundle: dict) -> tuple[str, str] | None:
    """Preserve legacy Markdown only when no structured analysis can reconstruct it."""
    company, technical, risk = (bundle.get(key) or {} for key in ("company", "technical", "risk"))
    has_company = any(company.get(key) for key in ("business", "valuation", "financial", "observations"))
    has_technical = bool(technical.get("available")) or any(technical.get(key) for key in (
        "last_close", "indicators", "signals", "supports", "resistances", "entry_scenarios", "holding"))
    has_risk = any(risk.get(key) for key in ("flags", "announcement_hits", "unlocks"))
    if has_company or has_technical or has_risk:
        return None
    llm = bundle.get("llm") or {}
    if isinstance(llm.get("markdown"), str) and llm["markdown"].strip():
        return None
    stored = bundle.get("report_markdown")
    if not isinstance(stored, str) or not stored.strip():
        return None
    if bundle.get("report_layout") == "split-v1":
        stored_log = bundle.get("log_markdown")
        return stored.strip(), stored_log.strip() if isinstance(stored_log, str) else ""
    return split_analysis_markdown(stored)


def render_report(bundle: dict) -> str:
    """Render the analysis result; operational audit and source inventory belong to render_log."""
    stored = _stored_only_documents(bundle)
    if stored is not None and stored[0]:
        return stored[0] + "\n"
    company = bundle.get("company") or {}
    technical = bundle.get("technical") or {}
    risk = bundle.get("risk") or {}
    sources = bundle.get("sources") or {}
    llm = bundle.get("llm") or {}
    lines = [f"# {_cell(bundle.get('symbol'))} {_cell(company.get('name', '个股'))} 分析", "",
             f"生成时间：{_cell(bundle.get('generated_at'))}（以来源采集时间和行情交易日为准）", ""]
    if bundle.get("demo"):
        lines += ["> **模拟数据演示：以下数字、点位、公告与情绪仅用于功能展示，不代表真实行情，不得据此交易。**", ""]
    lines += ["> 本报告用于研究参考。技术点位是有条件的历史价位参考，存在跳空、滑点和失效风险；采样和接口空结果不能证明没有风险。", ""]
    lines += ["## 一、公司与基本面", "",
              f"**主营业务**：{_cell(company.get('business'))}", "",
              f"**行业**：{_cell(company.get('industry'))}", "", "### 估值", ""]
    lines += _table(company.get("valuation"), "PE/PB 未核实，不能据此判断估值便宜或昂贵。")
    lines += ["", "PE 须结合静态/动态/TTM 口径、数据日期及利润正负解读，不能混用口径。", "", "### 财务基本面", ""]
    lines += _table(company.get("financial"), "财务数据及财务期尚未核实。")
    lines += [""] + _bullets(company.get("observations"), "基本面观察尚未形成。")
    if company.get("warnings"):
        lines += [""] + _bullets(company["warnings"])

    lines += ["", "## 二、股价走势与条件点位", ""]
    if technical.get("available"):
        lines += [f"行情交易日：{_cell(technical.get('data_as_of') or technical.get('as_of'))}；收盘价：{_cell(technical.get('last_close'))}；复权口径：{_cell(technical.get('adjustment'))}。", "",
                  "### 技术指标", ""]
        lines += _table(technical.get("indicators"), "均线、MACD 等指标不足。")
        lines += ["", f"趋势判断：{_cell(technical.get('trend'))}", ""]
        lines += _bullets(technical.get("signals"), "尚无足够信号。")
        lines += ["", "### 支撑与压力", "", "| 类型 | 依据 | 参考价格 |", "| --- | --- | --- |"]
        for key, kind in (("supports", "支撑"), ("resistances", "压力")):
            for row in technical.get(key) or []:
                lines.append(f"| {kind} | {_cell(row.get('label'))} | {_cell(row.get('price'))} |")
        lines += ["", "### 准备买入：触发条件与失效条件", ""]
        for index, scenario in enumerate(technical.get("entry_scenarios") or [], 1):
            if isinstance(scenario, dict):
                title = scenario.get("title") or scenario.get("name") or f"场景 {index}"
                lines += [f"**{_cell(title)}**", ""]
                lines += _bullets([f"{LABELS.get(k, k)}：{_text(v)}" for k, v in scenario.items() if k not in ("title", "name")])
            else:
                lines += _bullets([scenario])
            lines.append("")
        if not technical.get("entry_scenarios"):
            lines += ["进场条件尚未形成，不据此作进场结论。", ""]
        lines += ["### 已持仓：敏感点位", ""]
        if bundle.get("cost") is not None:
            lines += [f"输入持仓成本：{_cell(bundle.get('cost'))} 元。", ""]
        lines += _table(technical.get("holding"), "尚未形成持仓条件；未提供成本时，无法评估个人浮动盈亏。")
    else:
        lines += ["行情数据不足或未取得，均线、MACD、进场与持仓点位尚未核实。"]
    lines += [""] + _bullets(technical.get("warnings"), "技术分析不能保证未来收益；以实际交易规则、流动性和风险承受能力为约束。")

    lines += ["", "## 三、风险检查", "", f"规则风险级别：{_cell(risk.get('level'))}。此为已采证据的检查结果，未知项仍需核实。", ""]
    lines += _bullets(risk.get("flags"), "尚无充分证据形成风险标记，不能解释为无风险。")
    lines += ["", "### 公告与事件线索", ""]
    hits = risk.get("announcement_hits") or []
    for item in hits:
        scope = f"；匹配范围：{_cell(item.get('match_scope'))}" if item.get("match_scope") else ""
        lines.append(f"- {_cell(item.get('date'))}：{_link(item.get('title'), item.get('url'))}；匹配词：{_cell(item.get('keywords'))}{scope}。请打开原文核验。")
    if not hits:
        lines.append("本次未形成已核实公告线索；关键词未命中或接口返回空，不代表没有重大风险。")
    lines += ["", "### 限售解禁", ""]
    lines += _table(risk.get("unlocks"), "本次未取得可核实解禁记录；请核对覆盖窗口及后续公告。")
    lines += ["", "### 股吧观点与市场情绪", ""]
    sentiment = llm.get("sentiment") if llm.get("status") == "ok" else None
    if sentiment:
        counts = sentiment.get("counts") or {}
        lines += [f"模型采样 **{sentiment.get('sample_count', 0)}** 条去重帖子；可明确分类的相关讨论 **{sentiment.get('relevant_count', 0)}** 条；未确定 **{counts.get('uncertain', 0)}** 条；无关 **{counts.get('irrelevant', 0)}** 条。", "",
                  "| 类别 | 条数 | 占可明确分类相关讨论 |", "| --- | --- | --- |"]
        for key, label in (("bullish", "看多"), ("bearish", "看空"), ("neutral", "中性")):
            percent = sentiment.get("percentages", {}).get(key)
            lines.append(f"| {label} | {counts.get(key, 0)} | {percent}% |" if percent is not None else f"| {label} | {counts.get(key, 0)} | 无有效分母 |")
        lines += ["", _cell(sentiment.get("denominator_definition")), "", _cell(sentiment.get("summary")), "",
                  "看多理由："] + _bullets(sentiment.get("bullish_reasons"), "样本中未提取到可靠看多依据。")
        lines += ["", "看空理由："] + _bullets(sentiment.get("bearish_reasons"), "样本中未提取到可靠看空依据。")
        lines += [""] + _bullets(sentiment.get("warnings"))
    else:
        lines += ["股吧情绪尚未核实：未完成有效原帖采样及模型分类；关注度和热度不能代替股吧观点。", ""]
        if risk.get("sentiment_summary"):
            lines += [_cell(risk["sentiment_summary"]), ""]
    lines += [""] + _bullets(risk.get("warnings"), "来源覆盖和原始文本均需复核，未覆盖风险仍可能存在。")

    lines += ["", "## 四、综合研判（独立解释）", ""]
    if llm.get("status") == "ok" and llm.get("markdown"):
        analysis, _ = split_analysis_markdown(llm["markdown"])
        lines += ["以下综合解释应结合上述财务、指标和风险条件理解。", "", analysis or "综合研判暂缺，请结合已列数据及其局限评估。"]
    else:
        lines += ["综合研判暂缺，请结合已列数据及其局限评估；缺失情绪不能视为中性。"]
    if bundle.get("demo"):
        lines += ["", "**再次提醒：本报告全部为模拟演示，不能用于真实投资判断。**"]
    return "\n".join(lines).rstrip() + "\n"


def render_log(bundle: dict) -> str:
    """Render the research process, provenance, unresolved gaps, and model audit."""
    stored = _stored_only_documents(bundle)
    if stored is not None and stored[1]:
        if bundle.get("report_layout") == "split-v1":
            return stored[1] + "\n"
        return (f"# {_cell(bundle.get('symbol'))} 工作日志\n\n"
                f"报告生成时间：{_cell(bundle.get('generated_at'))}\n\n{stored[1]}\n")
    company = bundle.get("company") or {}
    sources = bundle.get("sources") or {}
    llm = bundle.get("llm") or {}
    lines = [f"# {_cell(bundle.get('symbol'))} {_cell(company.get('name', '个股'))} 工作日志", "",
             f"报告生成时间：{_cell(bundle.get('generated_at'))}", ""]
    if bundle.get("demo"):
        lines += ["> **模拟数据演示：本日志的来源与研究记录仅用于功能展示。**", ""]
    agent_lines = _agent_section(bundle.get("agent"))
    lines += agent_lines or ["## Agent 自主研究过程", "", "本报告未记录 Agent 自主研究过程；历史回看不会补发模型请求。", ""]
    lines += _source_section(sources) + [""] + _raw_evidence_sections(sources)
    lines += ["", "## 模型核验与用量", "",
              f"综合研判状态：{_cell(llm.get('status'))}；模型 ID：{_cell(llm.get('model'))}；已核验模型名称：{_cell(llm.get('model_verified_name'))}。"]
    if llm.get("error"):
        lines += ["", f"模型结果说明：{_cell(_agent_text(llm['error'], 1200))}"]
    usage = llm.get("usage") or {}
    if usage.get("total_tokens") is not None:
        lines += ["", f"本次模型用量：输入 {usage.get('prompt_tokens')}，输出 {usage.get('completion_tokens')}，合计 {usage.get('total_tokens')} tokens。"]
    if llm.get("status") == "ok" and llm.get("markdown"):
        _, model_log = split_analysis_markdown(llm["markdown"])
        if model_log:
            lines += ["", "## 模型补充的来源与未决事项", "", model_log]
    return "\n".join(lines).rstrip() + "\n"
