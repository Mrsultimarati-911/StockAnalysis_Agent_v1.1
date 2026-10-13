"""Read at most two important CNInfo announcement PDFs with bounded work.

The detail endpoint and adjunctUrl field are those used by CNInfo's official
notice-detail.js. Only CNInfo hosts are requested, including during redirects.
"""
from __future__ import annotations

from io import BytesIO
import json
import math
import queue
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests

_ALLOWED_HOSTS = {"www.cninfo.com.cn", "static.cninfo.com.cn", "cninfo.com.cn"}
_MAX_BYTES = 5 * 1024 * 1024
_MAX_PAGES = 8
_MAX_CHARACTERS = 8000


def _official_url(url: str) -> str:
    parsed = urlsplit(str(url))
    if (parsed.scheme not in {"https", "http"} or parsed.hostname not in _ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise ValueError("正文链接不是允许的巨潮官方地址")
    # The public site supports HTTPS; use it for source and PDF requests.
    return urlunsplit(("https", parsed.hostname, parsed.path or "/", parsed.query, ""))


def _priority(title: str) -> int:
    if any(word in title for word in ("减持", "立案", "诉讼", "问询", "风险", "处罚", "退市", "违约", "冻结", "担保", "质押")):
        return 100
    if "重大事项" in title or "重大损失" in title:
        return 90
    if "业绩说明会" in title:
        return 20
    if any(word in title for word in ("业绩预告", "业绩快报", "业绩")):
        return 80
    if any(word in title for word in ("年度报告", "季度报告", "中期报告", "定期报告")):
        return 60 if "摘要" not in title else 50
    return 0


def _download(session: requests.Session, method: str, url: str, deadline: float,
              *, params: dict[str, str] | None = None) -> tuple[bytes, str]:
    """No unrestricted redirects, unbounded body or API-specific credential."""
    url = _official_url(url)
    for redirect in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("单篇正文读取超时")
        with session.request(method, url, params=params, timeout=min(4, remaining), stream=True,
                             allow_redirects=False, headers={"Accept": "application/json, application/pdf, */*"}) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect == 3:
                    raise ValueError("公告链接重定向次数过多")
                url = _official_url(urljoin(url, response.headers.get("Location", "")))
                params = None
                if response.status_code == 303:
                    method = "GET"
                continue
            response.raise_for_status()
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > _MAX_BYTES:
                raise ValueError("公告文件超过5MiB，未下载正文")
            chunks, count = [], 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() >= deadline:
                    raise TimeoutError("单篇正文读取超时")
                if not chunk:
                    continue
                count += len(chunk)
                if count > _MAX_BYTES:
                    raise ValueError("公告文件超过5MiB，已停止读取")
                chunks.append(chunk)
            return b"".join(chunks), url
    raise ValueError("公告地址未完成解析")


def _pdf_url(session: requests.Session, row: dict[str, Any], deadline: float) -> str:
    original = _official_url(str(row.get("url", "")))
    if urlsplit(original).path.lower().endswith(".pdf"):
        return original
    query = parse_qs(urlsplit(original).query)
    code = query.get("stockCode", [""])[0]
    announcement_id = query.get("announcementId", [""])[0]
    announcement_time = query.get("announcementTime", [str(row.get("date", ""))])[0][:10]
    if (not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"\d+", announcement_id)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", announcement_time)):
        raise ValueError("巨潮公告详情链接缺少有效代码、公告编号或日期")
    content, _ = _download(session, "POST", "https://www.cninfo.com.cn/new/announcement/bulletin_detail", deadline,
                           params={"announceId": announcement_id, "announceTime": announcement_time,
                                   "flag": "true" if code.startswith(("0", "3")) else "false"})
    detail = json.loads(content)
    adjunct = (detail.get("announcement") or {}).get("adjunctUrl")
    if not isinstance(adjunct, str) or not adjunct:
        raise ValueError("巨潮详情未提供 PDF 地址，可能暂不可访问")
    url = _official_url(urljoin("https://static.cninfo.com.cn/", adjunct))
    if not urlsplit(url).path.lower().endswith(".pdf"):
        raise ValueError("巨潮正文不是 PDF，暂只保留标题")
    return url


def _extract_pdf(payload: bytes) -> dict[str, Any]:
    if not payload.lstrip().startswith(b"%PDF-"):
        raise ValueError("巨潮未返回有效 PDF，可能是访问限制或错误页")
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(payload), strict=False)
    if reader.is_encrypted:
        raise ValueError("公告 PDF 加密，未提取正文")
    total_pages = len(reader.pages)
    texts, errors, pages_read = [], [], 0
    for index in range(min(_MAX_PAGES, total_pages)):
        try:
            text = reader.pages[index].extract_text() or ""
            pages_read += 1
            if not text.strip():
                errors.append(f"第{index + 1}页无可提取文字")
            texts.append(text)
        except Exception:
            errors.append(f"第{index + 1}页提取失败")
    text = re.sub(r"[ \t]+", " ", "\n".join(texts)).strip()
    if not text:
        raise ValueError("PDF无可提取正文，可能是扫描件；未执行OCR")
    full = total_pages <= _MAX_PAGES and len(text) <= _MAX_CHARACTERS and not errors
    return {"content_excerpt": text[:_MAX_CHARACTERS],
            "content_scope": "full_text" if full else "first_8_pages_excerpt",
            "content_pages_read": pages_read, "content_total_pages": total_pages,
            "content_error": "；".join(errors)[:240] if errors else None}


def _fetch_one(row: dict[str, Any], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    with requests.Session() as session:
        pdf_url = _pdf_url(session, row, deadline)
        payload, final_url = _download(session, "GET", pdf_url, deadline)
    result = _extract_pdf(payload)
    result["content_pdf_url"] = final_url
    return result


def _bounded_fetch(row: dict[str, Any], timeout: float) -> dict[str, Any]:
    output: queue.Queue = queue.Queue(maxsize=1)
    def run() -> None:
        try:
            output.put((True, _fetch_one(row, timeout)))
        except Exception as exc:
            output.put((False, exc))
    # PDF parsing, DNS resolution and streamed requests also have a wall deadline.
    threading.Thread(target=run, daemon=True).start()
    try:
        success, value = output.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"单篇正文读取超过{timeout:g}秒，保留标题") from None
    if not success:
        raise value
    return value


def fetch_announcement_body(row: dict[str, Any], timeout: float = 8) -> dict[str, Any]:
    """Read one already-selected official announcement, independent of priority.

    The agent tool must separately match the URL against its collected list. This
    layer only accepts CNInfo hosts and never returns raw request exceptions.
    """
    result = dict(row)
    for name, value in {"content_scope": "title_only", "content_excerpt": "", "content_pages_read": 0,
                        "content_total_pages": None, "content_error": None}.items():
        result.setdefault(name, value)
    try:
        _official_url(str(row.get("url", "")))
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("正文读取时间预算无效")
        result.update(_bounded_fetch(row, min(8, float(timeout))))
    except (TimeoutError, requests.Timeout):
        result["content_error"] = "本次公告正文读取超时，保留已取得信息。"
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        result["content_error"] = f"巨潮公告正文请求失败{'（HTTP ' + str(status) + '）' if isinstance(status, int) else ''}，保留已取得信息。"
    except requests.RequestException:
        result["content_error"] = "巨潮公告正文暂不可访问，保留已取得信息。"
    except ImportError:
        result["content_error"] = "缺少 PDF 读取依赖，保留已取得信息。"
    except Exception:
        result["content_error"] = "公告链接无效、正文格式不受支持或提取失败，保留已取得信息。"
    return result


def enrich_announcements(rows: list[dict[str, Any]], max_documents: int = 2,
                         total_timeout: float = 12, document_timeout: float = 4) -> list[dict[str, Any]]:
    """Keep original titles/URLs/dates, including all failed or unselected rows."""
    enriched = [dict(row, content_scope="title_only", content_excerpt="", content_pages_read=0,
                     content_total_pages=None, content_error="未在本次重点正文读取范围内") for row in rows]
    selected = sorted((index for index, row in enumerate(enriched) if _priority(str(row.get("title", ""))) > 0),
                      key=lambda index: (_priority(str(enriched[index].get("title", ""))), str(enriched[index].get("date", ""))),
                      reverse=True)[:max(0, min(2, max_documents))]
    deadline = time.monotonic() + min(12, max(0, total_timeout))
    for index in selected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            enriched[index]["content_error"] = "正文总时间预算已用完，保留标题"
            continue
        try:
            enriched[index].update(_bounded_fetch(enriched[index], min(4, document_timeout, remaining)))
        except Exception as exc:
            enriched[index]["content_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return enriched
