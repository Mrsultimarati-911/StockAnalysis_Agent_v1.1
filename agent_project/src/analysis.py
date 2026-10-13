"""可核查的基本面观察与风险线索；不把未知数据当成安全。"""
from __future__ import annotations

from datetime import date
from .models import now_china, number


def records(sources: dict, key: str) -> list[dict]:
    source = sources.get(key, {})
    return source.get("records", []) if source.get("status") == "ok" else []


def analyze_company(sources: dict, symbol: str) -> dict:
    info = {row.get("item"): row.get("value") for row in records(sources, "info")}
    profiles = records(sources, "profile")
    profile = profiles[0] if profiles else {}
    business = profile.get("主营业务") or profile.get("经营范围")
    products = profile.get("产品名称") or profile.get("产品类型")
    if products and business:
        business = f"{business}；产品：{products}"
    name = info.get("股票简称") or info.get("公司名称") or profile.get("公司名称") or profile.get("A股简称") or symbol
    industry = info.get("行业") or info.get("所属行业") or profile.get("所属行业")
    valuation = {}
    observations = []
    warnings = []
    mapping = {"市盈率(TTM)": "pe_ttm", "市盈率(静)": "pe_static", "市盈率(动)": "pe_dynamic", "市净率": "pb"}
    for row in records(sources, "valuation"):
        item, value = row.get("item"), number(row.get("value"))
        if item in mapping and value is not None:
            valuation[mapping[item]] = {"倍数": value, "数据日期": row.get("date"), "口径": item}
            if item.startswith("市盈率") and value <= 0:
                warnings.append(f"{item} 为非正值，不作为低估证据。")
    financial = sorted(records(sources, "financial"), key=lambda row: row.get("报告期", ""), reverse=True)
    displayed = []
    for row in financial:
        def amount(key: str) -> float | None:
            value = number(row.get(key))
            return round(value / 1e8, 4) if value is not None else None
        displayed.append({"报告期": row.get("报告期"), "营收(亿元)": amount("营业总收入"),
                          "归母净利(亿元)": amount("净利润"), "营收同比(%)": row.get("营业总收入同比增长率"),
                          "净利同比(%)": row.get("净利润同比增长率"), "ROE(%)": row.get("净资产收益率"),
                          "负债率(%)": row.get("资产负债率"), "每股经营现金流(元)": row.get("每股经营现金流")})
    if financial:
        latest = financial[0]
        observations.append(f"最新财务期 {latest.get('报告期')}，公告日期 {latest.get('公告日期') or '该来源未提供'}。"
                            "财务金额为年初至报告期累计，同比比较上年同一累计期间。")
        revenue_yoy, profit_yoy = number(latest.get("营业总收入同比增长率")), number(latest.get("净利润同比增长率"))
        if revenue_yoy is not None and profit_yoy is not None:
            observations.append(f"最新累计营收同比 {revenue_yoy:.2f}%，归母净利润同比 {profit_yoy:.2f}%。")
            if revenue_yoy > 0 > profit_yoy:
                observations.append("出现增收不增利线索，需要结合费用、产品结构及非经常性损益进一步核查。")
            elif revenue_yoy < 0 and profit_yoy < 0:
                observations.append("收入与归母净利润同比同时下降，需要核查行业景气与经营变化。")
        ocf, eps = number(latest.get("每股经营现金流")), number(latest.get("基本每股收益"))
        if ocf is not None:
            observations.append(f"每股经营现金流 {ocf:.4f} 元；基本每股收益 {eps if eps is not None else '未取得'} 元。"
                                "两者可辅助检查现金回笼，不能替代完整现金流及营运资本分析。")
        if ocf is not None and eps is not None and ocf < 0 < eps:
            observations.append("利润为正而经营现金流为负，存在现金回笼与利润不匹配线索。")
        if not latest.get("公告日期"):
            warnings.append("财务来源未提供公告日期，无法证明历史某时点已公开；本项目不用于历史回测。")
        try:
            if (now_china().date() - date.fromisoformat(latest["报告期"])).days > 220:
                warnings.append("最近财务报告期距离当前超过220天，需核实数据更新及披露进度。")
        except (TypeError, ValueError, KeyError):
            warnings.append("财务报告期未能校验。")
    for key, label in (("profile", "主营业务"), ("valuation", "PE/PB"), ("financial", "财务基本面")):
        if not records(sources, key):
            warnings.append(f"{label}尚未取得有效数据，相关判断保留。")
    warnings.append("估值未作完整同行与历史分位比较，不仅凭 PE/PB 数值判断便宜；金融企业负债率需按行业解释。")
    return dict(name=str(name), industry=industry, business=business, valuation=valuation,
                financial=displayed, observations=observations, warnings=warnings)


RISK_WORDS = ("减持", "质押", "诉讼", "仲裁", "立案", "调查", "处罚", "问询", "亏损", "预亏",
              "风险提示", "退市", "非标准", "无法表示意见", "否定意见", "资金占用", "违约", "担保")


def analyze_risk(sources: dict, company: dict, technical: dict) -> dict:
    flags, warnings, hits = [], [], []
    for row in records(sources, "announcements"):
        title = str(row.get("title", ""))
        content = str(row.get("content_excerpt", ""))
        words = [word for word in RISK_WORDS if word in title or word in content]
        if words:
            hits.append(dict(row, keywords=words,
                             match_scope="标题及已提取正文" if content else "仅标题"))
    if hits:
        flags.append(f"公告标题及已提取正文检索命中 {len(hits)} 条风险相关线索；关键词命中可能是常规说明，需区分事项进展、解除和实际影响。")
    name = str(company.get("name", ""))
    if "ST" in name.upper() or "退" in name:
        flags.append("公司名称包含特别处理或退市相关标记，需要优先核查交易限制与持续上市风险。")
    financial = sorted(records(sources, "financial"), key=lambda row: row.get("报告期", ""), reverse=True)
    if financial:
        latest = financial[0]
        profit, growth = number(latest.get("净利润")), number(latest.get("净利润同比增长率"))
        if profit is not None and profit < 0:
            flags.append("最新报告期累计归母净利润为负，需核查亏损原因及持续性。")
        if growth is not None and growth < -30:
            flags.append(f"最新报告期累计归母净利润同比下降 {abs(growth):.2f}%，需关注经营恶化或基数影响。")
    unlocks = records(sources, "unlocks")
    if unlocks:
        query_options = sources["unlocks"].get("query_options")
        unlock_days = query_options.get("unlock_days", 180) if isinstance(query_options, dict) else 180
        if type(unlock_days) is not int or not 30 <= unlock_days <= 365:
            unlock_days = 180
        flags.append(f"未来{unlock_days}天内返回 {len(unlocks)} 批限售解禁计划，关注新增可流通供给；解禁不等同必然卖出。")
    if technical.get("is_stale"):
        flags.append("行情可能陈旧或停牌，进场情景已暂停。")
    required = ("announcements", "unlocks", "guba", "financial", "price")
    missing = []
    for key in required:
        source = sources.get(key, {})
        status = source.get("status", "unavailable")
        if status != "ok":
            label = source.get("label", key)
            warnings.append(f"{label}：{'返回空数据' if status == 'empty' else '未完成核实'}；"
                            "仅代表本次查询结果，不能据此断定不存在相关风险。")
            if status == "unavailable" or key in {"guba", "financial", "price"}:
                missing.append(key)
    if records(sources, "guba"):
        summary = f"已取得 {len(records(sources, 'guba'))} 条股吧样本；正负观点比例待模型分类，未分类不视为中性。"
    else:
        summary = "股吧原帖情绪未核实；人气排名及关注度不能替代看多/看空观点。"
    warnings.extend(["公告检索未覆盖全部风险，正文提取可能仅为前若干页；以每条 content_scope 为准。",
                     "股吧及新闻中的传闻尚未经过事实核实，应与巨潮公告或公司披露交叉验证。"])
    level = "存在需复核线索" if flags else "信息不足，待核实" if missing else "已取得范围内未形成高风险标记，仍有未覆盖事项"
    return dict(level=level, flags=flags, announcement_hits=hits, unlocks=unlocks,
                sentiment_summary=summary, warnings=warnings, missing_sources=missing)
