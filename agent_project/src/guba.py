"""小范围采集股吧公开帖子；不执行页面脚本，不绕过验证码。"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup
import requests

from .models import CHINA_TZ, SourceResult, normalize_symbol, now_china

BASE = "https://guba.eastmoney.com"
PUBLIC_API = "https://gbapi.eastmoney.com/webarticlelist/api/Article/WebArticleList"
PUBLIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": BASE + "/",
}


class _AccessRestricted(ValueError):
    """A public endpoint requires verification; do not retry it in this run."""


def embedded_json(html: str, variable: str) -> dict:
    match = re.search(r"(?:\bvar\s+|\bwindow\.)" + re.escape(variable) + r"\s*=\s*", html)
    if not match:
        raise ValueError("公开页面数据结构变化或访问受限。")
    value, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
    if not isinstance(value, dict):
        raise ValueError("页面返回的数据结构无效。")
    return value


def clean_text(value: str) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _stamp(value: str) -> datetime | None:
    # A pure date must not be invented into a midnight publication timestamp.
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", str(value)):
        return None
    try:
        moment = datetime.fromisoformat(str(value))
        return moment.replace(tzinfo=CHINA_TZ) if moment.tzinfo is None else moment.astimezone(CHINA_TZ)
    except (ValueError, TypeError):
        return None


def parse_list(html: str, symbol: str, cutoff: datetime, until: datetime,
               variable: str = "article_list") -> list[dict]:
    data = embedded_json(html, variable)
    return parse_posts(data, symbol, cutoff, until)


def parse_posts(data: dict, symbol: str, cutoff: datetime, until: datetime, *, list_excerpt: bool = False) -> list[dict]:
    """Normalize the same public post objects from the site's API and HTML."""
    if not isinstance(data.get("re"), list):
        raise ValueError("帖子列表字段缺失。")
    rows = []
    for original in data["re"]:
        if not isinstance(original, dict):
            continue
        post = dict(original)
        if isinstance(post.get("post_guba"), dict):
            post["stockbar_code"] = post.get("post_guba", {}).get("stockbar_code")
        if isinstance(post.get("post_user"), dict):
            post["user_id"] = post.get("post_user", {}).get("user_id")
        # 公告/资讯/置顶内容单独由其他数据源分析，避免混入股民情绪样本。
        if str(post.get("stockbar_code")) != symbol or post.get("post_type") != 0 or post.get("post_top_status", 0):
            continue
        published = _stamp(post.get("post_publish_time"))
        post_id = str(post.get("post_id", ""))
        content = clean_text(post.get("post_content") or "")
        abstract_only = not content and bool(post.get("post_abstract"))
        if abstract_only:
            content = clean_text(post["post_abstract"])
        original_title = clean_text(post.get("post_title", ""))
        title = original_title or content[:100]
        if not post_id.isdigit() or not title or published is None or not cutoff <= published <= until:
            continue
        author = str(post.get("user_id") or "").strip()
        rows.append({
            "id": post_id, "title": title[:400], "content": content[:1600],
            "title_is_excerpt": not bool(original_title),
            "published_at": published.isoformat(timespec="seconds"),
            "updated_at": str(post.get("post_last_time") or ""),
            "url": f"{BASE}/news,{symbol},{post_id}.html",
            "read_count": post.get("post_click_count"),
            "comment_count": post.get("post_comment_count"), "like_count": post.get("post_like_count"),
            "author_hash": hashlib.sha256((author or "unknown:" + post_id).encode()).hexdigest()[:16],
            "author_verified": bool(author),
            "text_scope": "body_excerpt" if content and (list_excerpt or abstract_only or len(content) > 1600) else "body" if content else "title_only", "kind": "discussion",
        })
    return rows


def parse_detail(html: str, row: dict, symbol: str) -> dict:
    try:
        post = embedded_json(html, "post_article")
    except ValueError:
        return _parse_dom_detail(html, row, symbol)
    if str(post.get("post_id")) != row["id"] or str(post.get("post_guba", {}).get("stockbar_code")) != symbol:
        raise ValueError("帖子详情与目标股票不一致。")
    if post.get("post_type", 0) != 0 or post.get("post_top_status", 0):
        raise ValueError("详情不是普通讨论帖。")
    text = clean_text(post.get("post_content") or "")
    abstract_only = not text and bool(post.get("post_abstract"))
    if abstract_only:
        text = clean_text(post["post_abstract"])
    published = _stamp(post.get("post_publish_time")) or _stamp(row.get("published_at"))
    result = dict(row)
    result.update(content=text[:1600], text_scope="body_excerpt" if text and (abstract_only or len(text) > 1600) else "body" if text else "title_only",
                  published_at=published.isoformat(timespec="seconds") if published else None,
                  kind="discussion" if post.get("post_type") == 0 else row.get("kind", "discussion"),
                  like_count=post.get("post_like_count"),
                  read_count=post.get("post_click_count", row.get("read_count")),
                  comment_count=post.get("post_comment_count", row.get("comment_count")))
    return result


def parse_dom_list(html: str, symbol: str) -> list[dict]:
    """Reference Guba_Toolkit's static selectors; list update time is not publication time."""
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("tr.listitem")
    if not items:
        raise ValueError("电脑公开列表结构不可识别。")
    rows = []
    for item in items:
        link = item.select_one("div.title a[href]")
        if link is None:
            continue
        href = str(link.get("href") or "")
        parsed = urlsplit(href)
        if parsed.netloc and parsed.hostname != "guba.eastmoney.com":
            continue
        match = re.fullmatch(r"/?news,(\d{6}),(\d+)\.html", parsed.path)
        if match is None or match.group(1) != symbol:
            continue
        markers = " ".join(node.get_text(" ", strip=True) for node in item.select(".title span, .title em, .title i"))
        if any(word in markers for word in ("置顶", "公告", "资讯", "广告")):
            continue
        title = clean_text(link.get_text(" ", strip=True))
        if not title:
            continue
        author = item.select_one("div.author a")
        author_id = str(author.get("href") or author.get_text(" ", strip=True)) if author else match.group(2)
        def value(selector: str) -> str:
            node = item.select_one(selector)
            return node.get_text(" ", strip=True) if node else ""
        rows.append(dict(id=match.group(2), title=title[:400], content="", title_is_excerpt=False,
                         published_at=None, updated_at=value("div.update"),
                         url=f"{BASE}/news,{symbol},{match.group(2)}.html", read_count=value("div.read"),
                         comment_count=value("div.reply"), like_count=None,
                         author_hash=hashlib.sha256(author_id.encode()).hexdigest()[:16],
                         text_scope="title_only", kind="discussion" if str(item.get("data-post-type", item.get("data-post_type"))) == "0" else "type_unverified"))
    return rows


def _parse_dom_detail(html: str, row: dict, symbol: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"], meta[property="og:url"]')
    if canonical:
        address = str(canonical.get("href") or canonical.get("content") or "")
        parsed = urlsplit(address)
        if (parsed.hostname not in {None, "guba.eastmoney.com", "mguba.eastmoney.com"}
                or parsed.path != f"/news,{symbol},{row['id']}.html"):
            raise ValueError("帖子详情与目标股票不一致。")
    content = soup.select_one("div.newstext")
    title = soup.select_one("div.newstitle")
    published = None
    for node in soup.select("div.time"):
        match = re.search(r"20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", node.get_text(" ", strip=True))
        if match:
            published = _stamp(match.group(0))
            break
    if content is None or title is None or published is None:
        raise ValueError("详情缺少正文、标题或完整发表时间。")
    result = dict(row)
    text = clean_text(str(content))
    result.update(title=clean_text(title.get_text(" ", strip=True))[:400], content=text[:1600],
                  published_at=published.isoformat(timespec="seconds"),
                  text_scope="body_excerpt" if len(text) > 1600 else "body" if text else "title_only")
    return result


class _LimitedReader:
    def __init__(self, seconds: float, timeout: float):
        self.deadline = time.monotonic() + seconds
        self.timeout = timeout
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.blocked_hosts: set[str] = set()

    def get(self, url: str) -> str:
        host = urlsplit(url).hostname
        if host not in {"guba.eastmoney.com", "mguba.eastmoney.com", "gbapi.eastmoney.com"}:
            raise ValueError("仅访问股吧公开入口。")
        if host == "gbapi.eastmoney.com" and urlsplit(url).path != "/webarticlelist/api/Article/WebArticleList":
            raise ValueError("仅访问已核实的公开帖子分页接口。")
        with self.lock:
            if host in self.blocked_hosts:
                raise _AccessRestricted("公开入口已受限，本轮不重复访问。")
            if time.monotonic() >= self.deadline:
                raise TimeoutError("本次采集已停止。")
            delay = max(0, 0.4 - (time.monotonic() - self.last_request))
            if delay:
                time.sleep(delay)
            remaining = self.deadline - time.monotonic()
            if remaining < 1:
                raise TimeoutError("本次采集时间已用完。")
            self.last_request = time.monotonic()
        response = requests.get(url, headers=PUBLIC_HEADERS,
                                timeout=min(self.timeout, remaining, 6))
        if response.status_code in {403, 429, 544}:
            with self.lock:
                self.blocked_hosts.add(host)
            raise _AccessRestricted(f"公开入口返回HTTP {response.status_code}，本轮停止该入口。")
        response.raise_for_status()
        html = response.content.decode("utf-8-sig", errors="replace")
        title = BeautifulSoup(html[:10000], "html.parser").title
        if title and title.get_text(strip=True) in {"身份核实", "安全验证", "访问验证", "Access Denied"}:
            with self.lock:
                self.blocked_hosts.add(host)
            raise _AccessRestricted("公开入口返回身份核实页，未继续访问该入口。")
        return html


def fetch_guba(job: dict) -> SourceResult:
    symbol = normalize_symbol(job["symbol"])
    result = SourceResult("guba", "股吧公开帖子", "公开列表分页与详情", f"{BASE}/list,{symbol},f_1.html")
    current = now_china()
    requested = datetime.fromisoformat(job.get("as_of", current.date().isoformat())).date()
    until = min(current, datetime.combine(requested, datetime.max.time(), tzinfo=CHINA_TZ))
    days = max(1, min(30, int(job.get("guba_days", 7))))
    cutoff = until - timedelta(days=days)
    maximum = max(10, min(100, int(job.get("guba_max_posts", 100))))
    pages = max(1, min(20, int(job.get("guba_max_pages", 20))))
    preferred = job.get("preferred_source", "auto")
    if preferred not in {"auto", "public_api", "desktop_public", "mobile_public"}:
        result.error = "不支持所选股吧公开入口。"
        return result
    reader = _LimitedReader(float(job.get("deadline_seconds", 45)), float(job.get("timeout", 10)))
    rows: list[dict] = []
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    authors: Counter = Counter()
    api_pages = desktop_pages = mobile_pages = raw_count = missing_dates = unverified_types = 0
    partial = False
    messages: list[str] = []

    def detail(row: dict) -> dict:
        if row["text_scope"] != "title_only":
            return row
        try:
            return parse_detail(reader.get(row["url"]), row, symbol)
        except (requests.RequestException, ValueError, TimeoutError):
            return row

    def collect(parsed: list[dict]) -> None:
        nonlocal missing_dates, unverified_types
        candidates = []
        pending_ids: set[str] = set()
        for row in parsed:
            if row["id"] not in seen_ids and row["id"] not in pending_ids:
                pending_ids.add(row["id"])
                candidates.append(row)
        if not candidates:
            return
        with ThreadPoolExecutor(max_workers=2) as pool:
            for row in pool.map(detail, candidates):
                published = _stamp(row.get("published_at"))
                if published is None:
                    missing_dates += 1
                    continue
                if not cutoff <= published <= until:
                    continue
                if row.get("kind") != "discussion":
                    unverified_types += 1
                    continue
                normalized = re.sub(r"\W", "", row["title"]).lower()
                if not normalized or normalized in seen_text or authors[row["author_hash"]] >= 3:
                    continue
                seen_text.add(normalized)
                seen_ids.add(row["id"])
                authors[row["author_hash"]] += 1
                rows.append(row)
                if len(rows) >= maximum:
                    break

    if preferred in {"auto", "public_api"}:
        page_ids: set[tuple[str, ...]] = set()
        try:
            for page in range(1, pages + 1):
                # Public parameters from the mobile site's own list.js sender;
                # sorttype=0 orders by original publication, rather than replies.
                # The service caps later pages at 50; ps=100 would overlap pages.
                url = PUBLIC_API + "?" + urlencode(dict(code=symbol, p=page, ps=50, sorttype=0,
                                                       plat="wap", version=300, product="guba", deviceid=1))
                data = json.loads(reader.get(url))
                if not isinstance(data, dict) or data.get("rc") != 1 or not isinstance(data.get("re"), list):
                    raise ValueError("公开分页接口未返回成功的帖子列表。")
                api_pages += 1
                raw_count += len(data["re"])
                identity = tuple(str(row.get("post_id")) for row in data["re"] if isinstance(row, dict))
                if identity and identity in page_ids:
                    messages.append("公开API分页返回重复帖子列表，已停止翻页。")
                    break
                page_ids.add(identity)
                collect(parse_posts(data, symbol, cutoff, until, list_excerpt=True))
                if len(rows) >= maximum or not data["re"]:
                    break
                # Only target-stock ordinary original posts participate in the
                # date boundary; recommendations/top posts can have other dates.
                times = [_stamp(post.get("post_publish_time")) for post in data["re"]
                         if isinstance(post, dict) and post.get("post_type") == 0
                         and str((post.get("post_guba") or {}).get("stockbar_code", post.get("stockbar_code"))) == symbol
                         and not post.get("post_top_status", 0)]
                times = [moment for moment in times if moment is not None]
                if times and max(times) < cutoff:
                    messages.append("按发表时间翻页已到采样窗口之前，停止扩页。")
                    break
        except _AccessRestricted as exc:
            partial = True
            messages.append("公开API入口受限：" + str(exc))
        except (requests.RequestException, ValueError, TimeoutError, KeyError):
            partial = True
            messages.append("公开API分页未完成：访问失败、返回异常或采集时间用完。")

    if preferred in {"auto", "desktop_public"} and len(rows) < maximum:
        page_ids: set[tuple[str, ...]] = set()
        try:
            for page in range(1, pages + 1):
                html = reader.get(f"{BASE}/list,{symbol},f_{page}.html")
                try:
                    data = embedded_json(html, "article_list")
                    raw_count += len(data.get("re") or [])
                    parsed = parse_list(html, symbol, cutoff, until)
                except ValueError:
                    parsed = parse_dom_list(html, symbol)
                    raw_count += len(BeautifulSoup(html, "html.parser").select("tr.listitem"))
                desktop_pages += 1
                identity = tuple(row["id"] for row in parsed)
                if identity and identity in page_ids:
                    messages.append("电脑分页返回重复帖子列表，已停止翻页。")
                    break
                page_ids.add(identity)
                collect(parsed)
                if len(rows) >= maximum:
                    break
        except _AccessRestricted as exc:
            partial = True
            messages.append("电脑公开入口受限：" + str(exc))
        except (requests.RequestException, ValueError, TimeoutError, KeyError):
            partial = True
            messages.append("电脑公开入口未完成：访问失败、结构变化或采集时间用完。")

    # In auto mode a small desktop result may also be supplemented by the public mobile snapshot.
    if preferred in {"auto", "mobile_public"} and len(rows) < maximum:
        mobile_url = f"https://mguba.eastmoney.com/mguba/list/{symbol}"
        try:
            html = reader.get(mobile_url)
            data = embedded_json(html, "list")
            raw_count += len(data.get("re") or [])
            collect(parse_list(html, symbol, cutoff, until, "list"))
            mobile_pages = 1
        except _AccessRestricted as exc:
            partial = True
            messages.append("移动公开入口受限：" + str(exc))
        except (requests.RequestException, ValueError, TimeoutError, KeyError):
            partial = True
            messages.append("移动公开入口未完成：访问失败、结构变化或采集时间用完。")
    rows.sort(key=lambda row: row["published_at"], reverse=True)
    result.records = rows
    result.status = "ok" if rows else "empty" if api_pages or desktop_pages or mobile_pages else "unavailable"
    if result.status == "unavailable":
        result.error = "未取得可解析的股吧公开页面，情绪尚未核实。"
    if api_pages:
        result.interface = "移动站公开JSON分页（WebArticleList，按发表时间）"
        result.url = PUBLIC_API
        if desktop_pages or mobile_pages:
            result.interface += "+公开页面补充"
    elif desktop_pages and mobile_pages:
        result.interface = "电脑公开分页（JSON/静态HTML）+移动公开快照"
    elif desktop_pages:
        result.interface = "电脑公开分页（JSON/静态HTML）"
    elif mobile_pages or preferred == "mobile_public":
        result.interface = "移动公开页面 HTML 内嵌 JSON"
        result.url = f"https://mguba.eastmoney.com/mguba/list/{symbol}"
    else:
        result.interface = "公开JSON分页" if preferred == "public_api" else "电脑公开分页" if preferred == "desktop_public" else "电脑/移动公开入口均未取得可解析页面"
    bodies = sum(row["text_scope"] != "title_only" for row in rows)
    earliest = min((row["published_at"] for row in rows), default="无")
    latest = max((row["published_at"] for row in rows), default="无")
    result.note = (
        f"公开API分页成功 {api_pages}/{pages if preferred in {'auto', 'public_api'} else 0} 页；"
        f"电脑公开列表成功采集 {desktop_pages}/{pages if preferred in {'auto', 'desktop_public'} else 0} 页，移动公开快照 {mobile_pages} 页；"
        f"列表共返回 {raw_count} 项（可能含重复、资讯、公告），筛选真实发表时间近 {days} 天、最多 {maximum} 条；"
        f"去除资讯、公告、置顶、重复标题和其他股票帖子，同作者最多3条。"
        f"返回 {len(rows)} 条，其中正文/正文节选 {bodies} 条，其余仅标题；不采集楼内评论或图片文字。"
        f"实际发表时间范围 {earliest} 至 {latest}。"
        + ("100条有效原帖采样目标已达到。" if len(rows) >= 100 else f"100条有效原帖采样目标未达到，还差{100 - len(rows)}条；保留采样缺口。")
        + ("公开API列表文本按正文节选标记，未将列表内容宣称为原帖全文。" if api_pages else "")
        + "公开页面及推荐项的列表顺序会受回复影响，属于有限样本，不能代表全部股民；首次采集不判断情绪历史变化。"
        "无独立标题的短帖以正文开头作为展示摘要。"
        + (f"{missing_dates}条候选因无法核实完整发表时间被排除；列表更新时间不当作发表时间。" if missing_dates else "")
        + (f"{unverified_types}条候选因无法确认普通讨论帖类型被排除。" if unverified_types else "")
        + (f"{sum(row.get('author_verified') is False for row in rows)}条帖子的作者标识缺失，无法核实同作者上限。" if any(row.get('author_verified') is False for row in rows) else "")
        + ("扩样未完成，已保留可用样本。" if partial else "")
        + " ".join(messages)
    )
    return result
