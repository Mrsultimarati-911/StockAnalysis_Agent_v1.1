"""Offline logging checks: no external data, network or real model requests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.config import Settings
from src.runlog import RunLogError, RunLogger


STAMP = "2026-10-13T11:22:00+08:00"


class RunLoggerTests(unittest.TestCase):
    def test_flushes_utf8_before_context_finishes(self):
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", started_at=STAMP) as logger:
                self.assertEqual(logger.path.name, "600875_20261013_1122_log.txt")
                logger.write("东方电气公开行动理由", event="check")
                self.assertIn("东方电气公开行动理由", logger.path.read_text(encoding="utf-8"))
            self.assertIn("run_end", logger.path.read_text(encoding="utf-8"))
            self.assertTrue(logger._handle.closed)

    def test_redacts_key_patterns_and_private_structures(self):
        fake_key = "private-local-credential-123"
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", api_key=fake_key, started_at=STAMP) as logger:
                logger.write(f"{fake_key} sk-test-123 Bearer abc-local-xyz", event="redact",
                             api_key="SHOULD_NOT_APPEAR", headers={"x": "HEADER_SECRET"},
                             config={"x": "CONFIG_SECRET"},
                             nested={"reasoning_content": "PRIVATE_REASONING", "messages": "RAW_MESSAGES",
                                     "safe": fake_key},
                             usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
                logger.event({"event": "tool_start", "reason": "核实业务", "name": "fetch_stock_data",
                              "raw": "RAW_ARGUMENTS", "reasoning_content": "PRIVATE_REASONING_2",
                              "arguments": {"source": "profile", "api_key": fake_key}})
            text = logger.path.read_text(encoding="utf-8")
            for secret in (fake_key, "sk-test-123", "abc-local-xyz", "SHOULD_NOT_APPEAR", "HEADER_SECRET",
                           "CONFIG_SECRET", "PRIVATE_REASONING", "RAW_MESSAGES", "RAW_ARGUMENTS"):
                self.assertNotIn(secret, text)
            self.assertIn("密钥已隐藏", text)
            self.assertIn('"completion_tokens":5', text)
            self.assertIn("核实业务", text)

    def test_exact_key_is_redacted_before_truncation(self):
        fake_key = "LOCAL_SECRET_" + "a" * 2000
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", api_key=fake_key, started_at=STAMP) as logger:
                logger.write("prefix " + fake_key + " suffix")
            text = logger.path.read_text(encoding="utf-8")
            self.assertNotIn("LOCAL_SECRET_", text)
            self.assertNotIn("a" * 50, text)

    def test_same_minute_collision_preserves_both_exact_filenames(self):
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", started_at=STAMP) as first:
                first.write("第一次")
                with RunLogger(Path(directory), "600875", started_at=STAMP) as second:
                    second.write("第二次")
                    self.assertNotEqual(first.path, second.path)
                    self.assertEqual(first.path.name, second.path.name)
            self.assertIn("第一次", first.path.read_text(encoding="utf-8"))
            self.assertNotIn("第二次", first.path.read_text(encoding="utf-8"))
            self.assertIn("第二次", second.path.read_text(encoding="utf-8"))

    def test_utc_start_time_uses_china_date(self):
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", started_at=datetime(2026, 10, 12, 20, 30, tzinfo=timezone.utc)) as logger:
                self.assertEqual(logger.path.name, "600875_20261013_0430_log.txt")

    def test_config_failure_can_use_only_exact_unknown_sentinel(self):
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "unknown", started_at=STAMP) as logger:
                self.assertEqual(logger.path.name, "unknown_20261013_1122_log.txt")
            with self.assertRaises(ValueError):
                RunLogger(Path(directory), "../../unknown", started_at=STAMP)

    def test_token_usage_requires_nonnegative_integer_counts(self):
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", started_at=STAMP) as logger:
                logger.write("实际用量", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
                logger.write("无效用量", usage={"prompt_tokens": "RAW_PROMPT", "completion_tokens": True, "total_tokens": -1})
            text = logger.path.read_text(encoding="utf-8")
            self.assertIn('"prompt_tokens":10', text)
            self.assertIn('"completion_tokens":5', text)
            self.assertIn('"total_tokens":15', text)
            self.assertNotIn("RAW_PROMPT", text)
            self.assertNotIn('"completion_tokens":true', text)
            self.assertNotIn('"total_tokens":-1', text)

    def test_concurrent_events_keep_complete_separate_lines(self):
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", started_at=STAMP) as logger:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda index: logger.event({"event": "tool_start", "step": index,
                                                             "summary": "并行取证"}), range(80)))
                lines = logger.path.read_text(encoding="utf-8").splitlines()
                action_lines = [line for line in lines if "[tool_start]" in line]
                self.assertEqual(len(action_lines), 80)
                steps = {json.loads(line.split(" | ", 1)[1])["step"] for line in action_lines}
                self.assertEqual(steps, set(range(80)))

    def test_exception_is_closed_and_logged_without_raw_text(self):
        with TemporaryDirectory() as directory:
            logger = RunLogger(Path(directory), "600875", started_at=STAMP)
            with self.assertRaises(ValueError):
                with logger:
                    raise ValueError("RAW_EXCEPTION_SECRET")
            text = logger.path.read_text(encoding="utf-8")
            self.assertIn("run_error", text)
            self.assertIn("ValueError", text)
            self.assertNotIn("RAW_EXCEPTION_SECRET", text)
            self.assertTrue(logger._handle.closed)

    def test_keyboard_interrupt_is_closed_and_logged(self):
        with TemporaryDirectory() as directory:
            logger = RunLogger(Path(directory), "600875", started_at=STAMP)
            with self.assertRaises(KeyboardInterrupt):
                with logger:
                    raise KeyboardInterrupt("PRIVATE_INTERRUPT_TEXT")
            text = logger.path.read_text(encoding="utf-8")
            self.assertIn("run_interrupted", text)
            self.assertNotIn("PRIVATE_INTERRUPT_TEXT", text)
            self.assertTrue(logger._handle.closed)

    def test_io_failure_uses_safe_error_and_closes(self):
        with TemporaryDirectory() as directory:
            logger = RunLogger(Path(directory), "600875", started_at=STAMP)
            with self.assertRaises(RunLogError) as caught:
                with logger:
                    with patch.object(logger, "_handle") as handle:
                        handle.write.side_effect = OSError("RAW_DISK_SECRET")
                        logger.write("公开消息")
            self.assertNotIn("RAW_DISK_SECRET", str(caught.exception))
            self.assertTrue(logger._handle.closed)

    def test_summary_omits_original_text_and_model_responses(self):
        bundle = {"agent": {"status": "limited", "model_calls": 3, "tool_calls": 8,
                             "remaining_gaps": [{"id": "cashflow", "description": "现金流成因待核实",
                                                 "raw": "PRIVATE_GAP_DATA"}]},
                  "llm": {"status": "ok", "markdown": "RAW_REPORT", "reasoning_content": "PRIVATE_MODEL_THOUGHT",
                          "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                          "sentiment": {"sample_count": 100, "target_met": True, "counts": {"bullish": 10},
                                        "classifications": [{"id": "RAW_CLASSIFICATION"}]}},
                  "sources": {"guba": {"status": "ok", "interface": "public", "records": [{"content": "RAW_POST"}],
                                        "note": "近七天公开列表节选", "error": "备用入口超时 sk-source-secret"}}}
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", started_at=STAMP) as logger:
                logger.record_bundle(bundle)
            text = logger.path.read_text(encoding="utf-8")
            for private in ("PRIVATE_GAP_DATA", "RAW_REPORT", "PRIVATE_MODEL_THOUGHT", "RAW_CLASSIFICATION", "RAW_POST"):
                self.assertNotIn(private, text)
            self.assertIn("现金流成因待核实", text)
            self.assertIn('"sample_count":100', text)
            self.assertIn('"record_count":1', text)
            self.assertIn('"completion_tokens":50', text)
            self.assertIn("近七天公开列表节选", text)
            self.assertIn("备用入口超时", text)
            self.assertNotIn("sk-source-secret", text)


def tool_call(name, arguments, identifier):
    return SimpleNamespace(id=identifier, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


class ScriptedSession:
    def __init__(self):
        self.calls = 0
        self.verified_name = "DeepSeek-V4.1-Flash"
        self.usage = {}

    def request(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            calls = [tool_call("fetch_stock_data", {"source": "profile", "reason": "核实主营业务"}, "one"),
                     tool_call("inspect_evidence", {"reason": "核查证据缺口"}, "two")]
        else:
            calls = [tool_call("finish_research", {"stop_reason": "现有工具范围仍有未知"}, "three")]
        message = SimpleNamespace(content="RAW_MODEL_MESSAGE", reasoning_content="PRIVATE_MODEL_REASONING", tool_calls=calls)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=message)])


class ScriptedRunner:
    def __init__(self, bundle, settings, **kwargs):
        self.bundle = bundle
        self.finished = False
        self.inspections = 0
        self.queried_sources = set()
        self.finish_reason = "仍需核实现金流"
        self.finish_summary = ""
        self.remaining_questions = []
        self.before_execute = lambda name: None

    def snapshot(self):
        return {}

    def gaps(self):
        return [{"id": "cashflow", "description": "现金流成因待核实", "source": "financial"}]

    def execute(self, name, args, timeout):
        self.before_execute(name)
        if name == "fetch_stock_data":
            self.queried_sources.add("profile")
            self.bundle["sources"]["profile"] = {"status": "ok", "records": [{"title": "公司业务"}]}
        elif name == "inspect_evidence":
            self.inspections += 1
        elif name == "finish_research":
            self.finished = True
        return {"status": "ok", "summary": "公开数据工具完成"}


class AgentEventTests(unittest.TestCase):
    def test_tool_start_is_flushed_before_execution_and_trace_is_copied(self):
        from src.agent import run_agent
        session = ScriptedSession()
        events = []
        with TemporaryDirectory() as directory:
            with RunLogger(Path(directory), "600875", started_at=STAMP) as logger:
                def sink(event):
                    events.append(event.copy())
                    logger.event(event)
                    event["summary"] = "SINK_MUTATED_COPY"

                def make_runner(*args, **kwargs):
                    runner = ScriptedRunner(*args, **kwargs)
                    def check(name):
                        live = logger.path.read_text(encoding="utf-8")
                        self.assertIn("[tool_start]", live)
                        self.assertIn(name, live)
                    runner.before_execute = check
                    return runner

                def final(bundle, settings, session):
                    session.calls += 1
                    return {"status": "ok", "markdown": "最终报告", "usage": {}, "sentiment": None}

                with patch("src.agent.DeepSeekSession", return_value=session), \
                     patch("src.agent.ToolRunner", side_effect=make_runner), \
                     patch("src.agent.refresh_analysis"), patch("src.agent.synthesize", side_effect=final), \
                     patch("src.agent.render_report", return_value="报告"), patch("src.agent.render_log", return_value="日志"):
                    bundle = run_agent("600875", None, Settings(llm_api_key="sk-test-local"), event_sink=sink)
                logger.record_bundle(bundle)
            text = logger.path.read_text(encoding="utf-8")
            self.assertNotIn("RAW_MODEL_MESSAGE", text)
            self.assertNotIn("PRIVATE_MODEL_REASONING", text)
            self.assertNotIn("SINK_MUTATED_COPY", json.dumps(bundle["agent"]["trace"]))
            self.assertIn("核实主营业务", text)
            self.assertIn("现金流成因待核实", text)
            self.assertEqual(bundle["agent"]["model_calls"], 3)
            self.assertEqual(bundle["agent"]["tool_calls"], 3)
            self.assertEqual(sum(item.get("event") == "tool_start" for item in events), 3)
            self.assertTrue(any(item.get("event") == "evidence_coverage" and item.get("gaps") for item in events))

    def test_failed_event_sink_stops_before_next_inference_and_final_report(self):
        from src.agent import run_agent
        session = ScriptedSession()

        def sink(event):
            if event.get("event") == "tool_start":
                raise OSError("RAW_LOG_FAILURE_SECRET")

        with patch("src.agent.DeepSeekSession", return_value=session), \
             patch("src.agent.ToolRunner", side_effect=ScriptedRunner), \
             patch("src.agent.refresh_analysis"), patch("src.agent.synthesize") as final:
            with self.assertRaises(RunLogError) as caught:
                run_agent("600875", None, Settings(llm_api_key="sk-test-local"), event_sink=sink)
        self.assertEqual(session.calls, 1)
        final.assert_not_called()
        self.assertNotIn("RAW_LOG_FAILURE_SECRET", str(caught.exception))

    def test_pipeline_passes_event_sink_to_agent(self):
        from src.pipeline import run_analysis
        sink = lambda event: None
        settings = Settings(llm_api_key="sk-test-local")
        with patch("src.agent.run_agent", return_value={"symbol": "600875"}) as run:
            run_analysis("600875", settings=settings, event_sink=sink)
        self.assertIs(run.call_args.kwargs["event_sink"], sink)


if __name__ == "__main__":
    unittest.main()
