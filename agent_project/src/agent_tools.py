"""Restricted research tools over the project's existing free data adapters."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import threading
from typing import Any

from .analysis import analyze_company, analyze_risk
from .announcements import fetch_announcement_body
from .llm import GUBA_TARGET_POSTS, MAX_GUBA_POSTS, guba_sample_count, prepare_evidence
from .models import CHINA_TZ, now_china, number
from .provider import SOURCES, SOURCE_INTERFACES, SOURCE_OPTIONS, OPTION_LIMITS, fetch_one, validate_options
from .technical import analyze_technical

REQUIRED_SOURCES = ("profile", "valuation", "financial", "price", "announcements", "unlocks", "guba")


def refresh_analysis(bundle: dict) -> None:
    sources = bundle["sources"]
    bundle["technical"] = analyze_technical(sources.get("price", {}).get("records", []),
                                            bundle.get("cost"), bundle["generated_at"])
    bundle["company"] = analyze_company(sources, bundle["symbol"])
    bundle["risk"] = analyze_risk(sources, bundle["company"], bundle["technical"])


def evidence_gaps(bundle: dict) -> list[dict]:
    """Observable gaps stay visible even if the model claims research is complete."""
    sources = bundle.get("sources", {})
    gaps = []
    def add(identity: str, category: str, description: str, source: str, action: str) -> None:
        gaps.append(dict(id=identity, category=category, description=description, source=source,
                         status="unresolved", suggested_action=action))
    for key in REQUIRED_SOURCES:
        source = sources.get(key)
        if source is None:
            add(f"source:{key}:not_queried", "未查询", f"尚未查询{SOURCES[key]}。", key, "选择对应数据工具查询。")
        elif source.get("status") != "ok" or not source.get("records"):
            empty = source.get("status") == "empty"
            add(f"source:{key}:empty" if empty else f"source:{key}:unavailable", "空结果" if empty else "获取失败",
                f"{SOURCES[key]}{'返回空结果' if empty else '未取得有效数据'}；不能据此断定不存在相关风险。",
                key, "考虑调整查询窗口、显式选择已有备用来源；仍不可得则保留未知。")
    company = bundle.get("company", {})
    if str(company.get("name") or "") in {"", bundle.get("symbol")} or not company.get("industry"):
        add("company:identity", "基本资料缺失", "公司名称或所属行业尚未取得可靠的基本资料确认。", "info",
            "选择info来源或其已有备用接口；不能补齐时保留未知。")
    valuation = company.get("valuation", {})
    if sources.get("valuation", {}).get("status") == "ok" and not any(k in valuation for k in ("pe_ttm", "pe_static", "pe_dynamic")):
        add("valuation:pe_missing", "字段缺失", "估值来源未返回有效 PE。", "valuation", "尝试估值备用来源，并保留估值口径。")
    financial = sources.get("financial", {}).get("records", [])
    if financial:
        latest = max(financial, key=lambda row: str(row.get("报告期") or ""))
        fields = [key for key in ("净利润", "营业总收入", "净资产收益率", "资产负债率", "每股经营现金流")
                  if number(latest.get(key)) is None]
        if fields:
            add("financial:missing_fields", "字段缺失", "最新财务期缺少：" + "、".join(fields), "financial",
                "查询财务备用来源或对应定期报告；不填零。")
        if not latest.get("公告日期"):
            add("financial:disclosure_date", "日期缺失", "最新财务数据缺少公告日期，无法核对披露时点。", "financial", "尝试另一财务来源或巨潮定期报告。")
    technical = bundle.get("technical", {})
    if sources.get("price", {}).get("status") == "ok" and (not technical.get("available") or technical.get("is_stale")):
        add("price:stale_or_insufficient", "行情质量", "行情陈旧或有效样本不足，不能据此给出可靠进场观察。", "price",
            "刷新行情、调整历史窗口或选择已有备用行情来源。")
    guba = sources.get("guba", {})
    if guba.get("status") == "ok":
        rows = guba.get("records", [])
        valid_count = guba_sample_count(guba)
        if valid_count < GUBA_TARGET_POSTS:
            add("guba:target_not_met", "采样未达标", f"股吧有效采样{valid_count}/{GUBA_TARGET_POSTS}条，尚未达到最低采样目标。", "guba",
                f"可将guba_max_posts设为{GUBA_TARGET_POSTS}，调整公开分页或采样窗口至最多30天；公开入口受限则保留未达标，勿重复无效抓取。")
        if valid_count < 20:
            add("guba:small_sample", "样本限制", f"股吧只有{valid_count}条有效原帖，不能判断整体情绪。", "guba",
                "可调整采样窗口；公开页仍只有少量帖子时保留小样本限制，勿重复无效抓取。")
        if any(row.get("text_scope") == "title_only" for row in rows):
            add("guba:title_only", "文本限制", "部分股吧帖子只有标题，观点证据有限。", "guba", "尝试刷新可访问公开页；不能取得正文则保留限制。")
    announcements = sources.get("announcements", {}).get("records", [])
    # Highlight latest original periodic report, rather than requesting every old PDF.
    reports = [row for row in announcements if "摘要" not in str(row.get("title", "")) and
               any(word in str(row.get("title", "")) for word in ("年度报告", "季度报告", "半年度报告", "中期报告"))]
    important = sorted(reports, key=lambda row: str(row.get("date", "")), reverse=True)[:1]
    important += [row for row in announcements if any(word in str(row.get("title", "")) for word in
                  ("立案", "处罚", "问询", "重大损失", "违约", "会计政策变更"))][:3]
    seen = set()
    for row in important:
        url = str(row.get("url") or "")
        if url in seen:
            continue
        seen.add(url)
        identity = hashlib.sha256(url.encode()).hexdigest()[:10]
        title = str(row.get("title", ""))[:100]
        if not row.get("content_excerpt"):
            add(f"announcement:{identity}:unread", "正文未读", f"尚未读取《{title}》正文。", "announcements",
                "调用 read_announcement 读取已采集列表中的该公告链接。")
        elif row.get("content_scope") != "full_text":
            add(f"announcement:{identity}:partial", "正文节选", f"《{title}》只读取部分页面，未覆盖全文。", "announcements",
                "在当前正文工具限制下保留未覆盖范围，不能声称已核实全文。")
    return gaps


def tool_definitions() -> list[dict]:
    options = {name: {"type": "integer", "minimum": limits[0], "maximum": limits[1]}
               for name, limits in OPTION_LIMITS.items()}
    options["preferred_source"] = {"type": "string", "description": "auto 或该来源已有接口的确切名称。"}
    description = "查询当前这一只股票的公开数据。可并行选择不同source；首次按需取证，补查须改变有意义的参数或选源。支持接口：" + json.dumps(SOURCE_INTERFACES, ensure_ascii=False)
    specs = [
        ("fetch_stock_data", description,
         {"source": {"type": "string", "enum": list(SOURCES)},
          "options": {"type": "object", "properties": options, "additionalProperties": False},
          "refresh": {"type": "boolean", "description": "是否绕过缓存；相同查询最多一次，不可无限刷新。"},
          "reason": {"type": "string", "description": "简短说明这次取证要回答的问题。"}}, ["source", "reason"]),
        ("read_announcement", "针对缺口读取已采集公告列表中的指定巨潮正文；只允许列表原始URL，最多前8页。",
         {"url": {"type": "string"}, "reason": {"type": "string"}}, ["url", "reason"]),
        ("inspect_evidence", "检查公司、技术、风险的证据覆盖及可观测缺口。至少检查一次，再决定补查或结束。",
         {"reason": {"type": "string"}, "questions": {"type": "array", "items": {"type": "string"}, "maxItems": 8}}, ["reason"]),
        ("finish_research", "证据已足够或剩余缺口在现有工具范围内无法解决时结束取证，保留未知并进入最终报告。核心来源尚未尝试或未检查证据时会拒绝。",
         {"stop_reason": {"type": "string", "maxLength": 240, "description": "最多240字，明确为什么现在停止取证；不复述长篇研究结论。"},
          "summary": {"type": "string", "description": "可选的简短研究概要。"},
          "unresolved_gap_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
          "remaining_questions": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                                  "description": "更新后的最终未解决问题；不应继续列入已通过补查回答的问题。"}}, ["stop_reason"]),
    ]
    return [{"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}}
            for name, desc, props, required in specs]


def safe_arguments(name: str, arguments: Any) -> dict:
    """Audit only tool parameters, never arbitrary model text or secrets."""
    if not isinstance(arguments, dict):
        return {}
    allowed = {"fetch_stock_data": {"source", "options", "refresh"}, "read_announcement": {"url"},
               "inspect_evidence": {"questions"}, "finish_research": {"stop_reason", "summary", "unresolved_gap_ids", "remaining_questions"}}
    return {key: value for key, value in arguments.items() if key in allowed.get(name, set())}


def _guba_scope(source: dict) -> tuple[str, int] | None:
    """Only compare supplements with a known matching Chinese date and window."""
    options = source.get("query_options")
    days = options.get("guba_days") if isinstance(options, dict) else None
    if type(days) is not int or not 1 <= days <= 30:
        return None
    try:
        stamp = datetime.fromisoformat(source.get("fetched_at") or "")
    except (ValueError, TypeError):
        return None
    stamp = stamp.replace(tzinfo=CHINA_TZ) if stamp.tzinfo is None else stamp.astimezone(CHINA_TZ)
    return stamp.date().isoformat(), days


class ToolRunner:
    def __init__(self, bundle: dict, settings: Any, default_refresh: bool = False):
        self.bundle, self.settings, self.default_refresh = bundle, settings, default_refresh
        self.queried_sources: set[str] = set()
        self.inspections = 0
        self.finished = False
        self.finish_summary = ""
        self.finish_reason = ""
        self.remaining_questions: list[str] = []
        self._queries: Counter = Counter()
        self._lock = threading.RLock()

    def snapshot(self) -> dict:
        with self._lock:
            return prepare_evidence(self.bundle)

    def gaps(self) -> list[dict]:
        with self._lock:
            result = evidence_gaps(self.bundle)
            for index, question in enumerate(self.remaining_questions):
                result.append(dict(id=f"model:question:{index + 1}", category="模型保留问题", description=question,
                                   source="model", status="unresolved", suggested_action="现有取证范围未解决，报告中保留未知。"))
            return result

    def execute(self, name: str, args: dict, timeout: float) -> dict:
        spec = next((item["function"] for item in tool_definitions() if item["function"]["name"] == name), None)
        if not spec or not isinstance(args, dict):
            return dict(status="rejected", summary="未知工具或参数对象无效；未执行。")
        parameters = spec["parameters"]
        if set(args) - set(parameters["properties"]) or any(key not in args for key in parameters["required"]):
            return dict(status="rejected", summary="工具存在未知参数或缺少必填参数；未执行。")
        for key, value in args.items():
            expected = parameters["properties"][key]["type"]
            valid = {"string": isinstance(value, str), "object": isinstance(value, dict),
                     "boolean": isinstance(value, bool), "array": isinstance(value, list)}.get(expected, False)
            if not valid or (isinstance(value, str) and (not value.strip() or len(value) > parameters["properties"][key].get("maxLength", 1200))):
                return dict(status="rejected", summary="工具参数类型或长度无效；未执行。")
            if expected == "array" and (len(value) > parameters["properties"][key].get("maxItems", 8) or
                                        any(not isinstance(v, str) or not v.strip() or len(v) > 400 for v in value)):
                return dict(status="rejected", summary="工具列表参数无效；未执行。")
        if timeout <= 0:
            return dict(status="timeout", summary="本次研究的时间预算已用完。")
        if name == "fetch_stock_data":
            key = args["source"]
            try:
                options = validate_options(key, args.get("options"))
            except (ValueError, TypeError):
                return dict(status="rejected", summary="来源、选源或查询参数不在允许范围内；未执行。")
            defaults = dict(lookback_days=730, announcement_days=90, unlock_days=180,
                            guba_days=self.settings.guba_days, guba_max_posts=min(MAX_GUBA_POSTS, self.settings.guba_max_posts),
                            guba_max_pages=min(OPTION_LIMITS["guba_max_pages"][1], self.settings.guba_max_pages), preferred_source="auto")
            effective = {k: options.get(k, defaults[k]) for k in SOURCE_OPTIONS[key]}
            refresh = self.default_refresh or args.get("refresh", False)
            signature = json.dumps([key, effective, refresh], sort_keys=True)
            with self._lock:
                if self._queries[signature]:
                    return dict(status="duplicate", summary="相同参数的查询已执行，跳过重复；请针对缺口改变参数或选源。")
                self._queries[signature] += 1
                self.queried_sources.add(key)
            result = fetch_one(key, self.bundle["symbol"], self.settings, refresh=refresh, options=options, timeout=timeout).to_dict()
            result["query_options"] = effective
            with self._lock:
                old = self.bundle["sources"].get(key, {})
                scope = _guba_scope(old) if key == "guba" else None
                retained_guba = (key == "guba" and old.get("status") == "ok" and result.get("status") == "ok"
                                 and scope is not None and scope == _guba_scope(result)
                                 and guba_sample_count(old) > guba_sample_count(result))
                if not retained_guba and (result.get("status") == "ok" or old.get("status") != "ok"):
                    if key == "announcements":
                        previous = {row.get("url"): row for row in old.get("records", [])}
                        for row in result.get("records", []):
                            prior = previous.get(row.get("url"), {})
                            if prior.get("content_excerpt") and not row.get("content_excerpt"):
                                row.update({k: v for k, v in prior.items() if k.startswith("content_")})
                    self.bundle["sources"][key] = result
                refresh_analysis(self.bundle)
                active = self.bundle["sources"].get(key, {})
                old_content = json.dumps(old.get("records", []), sort_keys=True, ensure_ascii=False)
                new_content = json.dumps(active.get("records", []), sort_keys=True, ensure_ascii=False)
                changed = result.get("status") == "ok" and old_content != new_content
            if retained_guba:
                return dict(status=result["status"], source=key, record_count=len(active.get("records", [])),
                            fetched_record_count=len(result.get("records", [])), interface=active.get("interface"),
                            evidence_changed=False, retained_previous=True,
                            summary=f"股吧补查返回{guba_sample_count(result)}条有效帖子，少于同日期、同{scope[1]}天窗口已有的{guba_sample_count(old)}条；保留先前更完整样本，未跨窗口合并。")
            return dict(status=result["status"], source=key, record_count=len(result.get("records", [])),
                        interface=result.get("interface"), evidence_changed=changed,
                        summary=f"{SOURCES[key]}返回{len(result.get('records', []))}条，状态{result['status']}。"
                                + ("补查失败，保留先前已取得证据。" if result.get("status") != "ok" and old.get("status") == "ok" else ""))
        if name == "read_announcement":
            url = args["url"]
            with self._lock:
                rows = self.bundle["sources"].get("announcements", {}).get("records", [])
                row = next((row for row in rows if row.get("url") == url), None)
                if row is None:
                    return dict(status="rejected", summary="公告URL不在当前股票已采集的列表中；未请求该地址。")
                signature = "pdf:" + url
                if self._queries[signature]:
                    return dict(status="duplicate", summary="该公告已执行过定向正文读取，不重复请求。")
                self._queries[signature] += 1
                copied = dict(row)
            updated = fetch_announcement_body(copied, timeout=min(8, timeout))
            with self._lock:
                row.update(updated)
                refresh_analysis(self.bundle)
            return dict(status="ok" if updated.get("content_excerpt") else "unavailable",
                        summary=f"定向读取《{str(row.get('title', ''))[:100]}》；范围：{updated.get('content_scope', 'title_only')}。",
                        record_count=1 if updated.get("content_excerpt") else 0, evidence_changed=bool(updated.get("content_excerpt")))
        if name == "inspect_evidence":
            self.inspections += 1
            self.remaining_questions = list(dict.fromkeys(args.get("questions", [])))
            gaps = self.gaps()
            return dict(status="ok", summary=f"已检查公司、技术和风险证据；当前{len(gaps)}项缺口或限制。", gaps=gaps)
        pending = [key for key in REQUIRED_SOURCES if key not in self.queried_sources]
        if pending or not self.inspections:
            return dict(status="rejected", summary="核心来源尚未全部尝试或未检查证据，暂不能结束。",
                        unqueried_sources=pending, needs_inspection=not bool(self.inspections))
        self.finished = True
        self.finish_summary = args.get("summary", "")[:400]
        self.finish_reason = args["stop_reason"][:240]
        # Final open questions replace the earlier working list. Observable gaps
        # are computed independently and cannot be removed by a completion claim.
        if "remaining_questions" in args:
            self.remaining_questions = list(dict.fromkeys(args["remaining_questions"]))[:8]
        return dict(status="ok", summary="模型已结束取证；可观测缺口将继续保留，不因结束声明被清除。",
                    gaps=self.gaps())
