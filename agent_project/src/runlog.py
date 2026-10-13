"""Immediate, bounded public execution logs; model private reasoning is excluded."""
from __future__ import annotations

from datetime import datetime
import importlib.metadata
import json
import math
from pathlib import Path
import re
import sys
import threading
from typing import Any
import uuid

from .models import CHINA_TZ, normalize_symbol, now_china


class RunLogError(RuntimeError):
    """A safe failure that must stop further paid work when logging fails."""


_PRIVATE_KEYS = (
    "api_key", "apikey", "authorization", "secret", "password", "credential",
    "headers", "config", "prompt", "messages", "reasoning", "chain_of_thought",
    "raw", "exception", "traceback", "content", "body", "completion",
)
_EVENT_FIELDS = {
    "event", "kind", "step", "name", "status", "summary", "message", "reason",
    "arguments", "started_at", "duration_ms", "model_calls", "tool_calls",
    "max_model_calls", "max_tool_calls", "gaps", "gap_count", "budget", "count",
    "source", "interface", "record_count", "status_code", "model", "verified_name",
}
_GAP_FIELDS = {"id", "category", "description", "source", "status", "suggested_action", "severity", "message"}
_TOKEN_COUNTS = {"prompt_tokens", "completion_tokens", "total_tokens"}


def runtime_environment() -> dict:
    """Public runtime metadata for diagnosing dependency differences."""
    try:
        plotly_version = importlib.metadata.version("plotly")
    except importlib.metadata.PackageNotFoundError:
        plotly_version = "未安装"
    return {"python_version": sys.version.split()[0], "python_executable": sys.executable,
            "plotly_version": plotly_version}


class RunLogger:
    """One exclusive UTF-8 TXT log per run, flushed before each call returns.

    Callers provide brief public action reasons, never raw model messages. The
    logger additionally removes private fields and credentials at its boundary.
    """

    def __init__(self, log_dir: Path, stock_code: str, api_key: str = "", started_at=None):
        self.log_dir = Path(log_dir)
        # Config failures need a log even before a valid stock code is known.
        self.stock_code = "unknown" if stock_code == "unknown" else normalize_symbol(stock_code)
        self._api_key = api_key if isinstance(api_key, str) else ""
        if started_at is None:
            stamp = now_china()
        elif isinstance(started_at, datetime):
            stamp = started_at
        elif isinstance(started_at, str):
            try:
                stamp = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("运行日志开始时间无效。") from None
        else:
            raise ValueError("运行日志开始时间无效。")
        self.started_at = stamp.replace(tzinfo=CHINA_TZ) if stamp.tzinfo is None else stamp.astimezone(CHINA_TZ)
        self._stem = f"{self.stock_code}_{self.started_at:%Y%m%d_%H%M}"
        self.path = self.log_dir / f"{self._stem}_log.txt"
        self._handle = None
        self._lock = threading.RLock()
        self._opened = False
        self._closed = False

    def __enter__(self):
        with self._lock:
            if self._opened or self._closed:
                raise RunLogError("运行日志上下文不能重复打开。")
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                while True:
                    try:
                        self._handle = self.path.open("x", encoding="utf-8", newline="\n")
                        break
                    except FileExistsError:
                        directory = self.log_dir / f"{self._stem}_{uuid.uuid4().hex}"
                        directory.mkdir(exist_ok=False)
                        self.path = directory / f"{self._stem}_log.txt"
                self._opened = True
                self.write("运行日志已建立。记录公开行动理由、证据缺口与调用状态，不记录模型隐藏推理。",
                           event="run_start", stock_code=self.stock_code,
                           started_at=self.started_at.isoformat(timespec="seconds"))
            except RunLogError:
                if self._handle is not None:
                    try:
                        self._handle.close()
                    except OSError:
                        pass
                self._closed = True
                raise
            except OSError:
                self._closed = True
                raise RunLogError("无法建立运行日志，已停止运行。请检查日志目录及写入权限。") from None
        return self

    def _text(self, value: str, maximum: int = 1600) -> str:
        # Redact before bounding text, so a long key cannot be partly exposed.
        if self._api_key:
            value = value.replace(self._api_key, "[密钥已隐藏]")
        value = re.sub(r"(?i)\bsk-[a-z0-9_-]+", "[密钥已隐藏]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [已隐藏]", value)
        value = value.replace("\r", "\\r").replace("\n", "\\n")
        # Keep each event on one physical line and prevent terminal control text.
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
        return value[:maximum] + ("…[截断]" if len(value) > maximum else "")

    @staticmethod
    def _private_key(key: str) -> bool:
        lowered = key.lower().replace("-", "_")
        if lowered in _TOKEN_COUNTS:
            return False
        return any(part in lowered for part in _PRIVATE_KEYS) or ("token" in lowered and not lowered.endswith("_tokens"))

    def _clean(self, value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "[层级省略]"
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, dict):
            return {self._text(key, 100): self._clean(item, depth + 1)
                    for key, item in list(value.items())[:50]
                    if isinstance(key, str) and not self._private_key(key)
                    and (key.lower() not in _TOKEN_COUNTS or (type(item) is int and item >= 0))}
        if isinstance(value, (list, tuple)):
            return [self._clean(item, depth + 1) for item in value[:30]]
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if value is None or isinstance(value, (int, bool)):
            return value
        # Do not call repr/str on exceptions, settings, responses or other objects.
        return "[对象详情省略]"

    def write(self, message, *, level="INFO", event="run", **details):
        with self._lock:
            if not self._opened or self._closed or self._handle is None:
                raise RunLogError("运行日志尚未打开或已关闭，已停止运行。")
            safe_message = self._text(message if isinstance(message, str) else "[消息详情省略]")
            safe_event = self._text(event if isinstance(event, str) else "run", 80)
            safe_level = level.upper() if isinstance(level, str) and level.upper() in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO"
            payload = self._clean(details)
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(serialized) > 18000:
                serialized = json.dumps({"bounded_details": serialized[:17500] + "…[详情截断]"}, ensure_ascii=False)
            line = f"[{now_china().isoformat(timespec='milliseconds')}] [{safe_level}] [{safe_event}] {safe_message}"
            if payload:
                line += " | " + serialized
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except (OSError, ValueError):
                raise RunLogError("运行日志写入失败，已停止后续调用。请检查日志目录及磁盘空间。") from None

    def event(self, event_dict):
        """Callback for copied public Agent events, with a strict top-level list."""
        if not isinstance(event_dict, dict):
            raise RunLogError("运行日志事件格式无效，已停止后续调用。")
        selected = {key: value for key, value in event_dict.items() if key in _EVENT_FIELDS}
        if "gaps" in selected:
            selected["gaps"] = self._public_gaps(selected["gaps"])
        message = selected.pop("summary", None) or selected.pop("message", None) or "Agent 执行状态更新。"
        event = selected.pop("event", None) or selected.get("kind", "agent")
        level = "WARNING" if selected.get("status") in {"error", "rejected", "timeout"} else "INFO"
        self.write(message, level=level, event=event, **selected)

    def _public_gaps(self, gaps):
        if not isinstance(gaps, (list, tuple)):
            return []
        return [{key: item for key, item in gap.items() if key in _GAP_FIELDS}
                for gap in gaps[:30] if isinstance(gap, dict)]

    def record_bundle(self, bundle):
        """Summarize a completed bundle without persisting original data or prose."""
        bundle = bundle if isinstance(bundle, dict) else {}
        state = bundle.get("agent") if isinstance(bundle.get("agent"), dict) else {}
        llm = bundle.get("llm") if isinstance(bundle.get("llm"), dict) else {}
        self.write("研究执行结束，以下为实际调用预算与停止原因。", event="research_summary",
                   status=state.get("status"), stop_code=state.get("stop_code"), stop_reason=state.get("stop_reason"),
                   model_calls=state.get("model_calls", 0), max_model_calls=state.get("max_model_calls", 12),
                   tool_calls=state.get("tool_calls", 0), max_tool_calls=state.get("max_tool_calls", 36),
                   elapsed_seconds=state.get("elapsed_seconds"), llm_status=llm.get("status"),
                   model=llm.get("model"), verified_name=llm.get("model_verified_name"),
                   usage={key: llm.get("usage", {}).get(key, 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
                   if isinstance(llm.get("usage"), dict) else {})
        sources = bundle.get("sources")
        for key, source in list(sources.items())[:30] if isinstance(sources, dict) else []:
            if not isinstance(source, dict):
                continue
            rows = source.get("records")
            self.write("数据来源采集概况。", event="source_summary", source=key,
                       interface=source.get("interface"), status=source.get("status"),
                       fetched_at=source.get("fetched_at"), cached=source.get("cached"),
                       # These are the adapters' bounded public diagnostics, not
                       # original SDK exception objects or raw HTTP responses.
                       error=source.get("error"), note=source.get("note"),
                       record_count=len(rows) if isinstance(rows, list) else 0)
        sentiment = llm.get("sentiment")
        if isinstance(sentiment, dict):
            counts = sentiment.get("counts") if isinstance(sentiment.get("counts"), dict) else {}
            self.write("股吧情绪分类校验概况。", event="sentiment_summary",
                       sample_count=sentiment.get("sample_count"), target_met=sentiment.get("target_met"),
                       counts={key: counts.get(key, 0) for key in ("bullish", "bearish", "neutral", "uncertain", "irrelevant")})
        gaps = state.get("remaining_gaps")
        self.write("保留尚未解决的证据缺口与研究范围限制。", event="remaining_gaps",
                   gap_count=len(gaps) if isinstance(gaps, list) else 0, gaps=self._public_gaps(gaps))
        if bundle.get("export_warnings"):
            self.write("部分导出内容尚未完整生成。", level="WARNING", event="export_warning",
                       warnings=bundle.get("export_warnings"))

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            event = "run_interrupted" if issubclass(exc_type, (KeyboardInterrupt, SystemExit)) else "run_error"
            message = "运行已中断，日志保留已完成的操作。" if event == "run_interrupted" else "运行未正常完成，日志保留已完成的操作；原始异常详情未记录。"
        else:
            event, message = "run_end", "本次运行日志已完整关闭。"
        failure = None
        try:
            self.write(message, level="ERROR" if exc_type else "INFO", event=event,
                       error_class=exc_type.__name__ if exc_type else None)
        except RunLogError as exc:
            failure = exc
        finally:
            with self._lock:
                try:
                    if self._handle is not None:
                        self._handle.close()
                except OSError:
                    failure = RunLogError("运行日志关闭失败，请检查日志文件完整性。")
                self._closed = True
        if failure is not None:
            raise failure from None
        return False
