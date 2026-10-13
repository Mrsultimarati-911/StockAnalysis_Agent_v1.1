"""Offline acceptance checks for the standalone configured runner."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.cli import main
from src.config import ConfigError, MAX_MODEL_CALLS, MAX_TOOL_CALLS, Settings, load_run_config
from src.runlog import RunLogError, RunLogger


def document():
    return {"llm": {"api_key": "sk-local-test-only", "model": "deepseek-flash",
                    "base_url": "https://api.deepseek.com"},
            "task": {"stock_code": "600875", "use_llm": True, "refresh_data": False},
            "limits": {"max_model_calls": 12, "max_tool_calls": 36}}


class StandaloneConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "config.json"

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_relative_defaults_and_log_override_follow_config_location(self):
        self.write(document())
        config = load_run_config(self.path)
        self.assertEqual(config.settings.report_dir, self.directory / "outputs")
        self.assertEqual(config.settings.log_dir, self.directory / "logs")
        data = document()
        data["output"] = {"directory": "results"}
        data["logging"] = {"directory": "run_history"}
        self.write(data)
        settings = load_run_config(self.path).settings
        self.assertEqual(settings.report_dir, self.directory / "results")
        self.assertEqual(settings.log_dir, self.directory / "run_history")
        self.assertEqual((MAX_MODEL_CALLS, MAX_TOOL_CALLS), (12, 36))

    def test_model_limits_and_invalid_logging_rejected_offline(self):
        for section, key, value in (("llm", "model", "deepseek-chat"),
                                    ("llm", "base_url", "https://example.org"),
                                    ("limits", "max_model_calls", 13),
                                    ("limits", "max_tool_calls", 40),
                                    ("logging", "directory", "")):
            with self.subTest(section=section, key=key):
                data = document()
                data.setdefault(section, {})[key] = value
                self.write(data)
                with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
                    with self.assertRaises(ConfigError):
                        load_run_config(self.path)

    def test_template_is_public_and_has_standalone_defaults(self):
        config = load_run_config(PROJECT / "config.example.json")
        self.assertEqual(config.settings.llm_api_key, "")
        self.assertEqual(config.settings.report_dir, PROJECT / "outputs")
        self.assertEqual(config.settings.log_dir, PROJECT / "logs")
        self.assertNotIn("sk-local-test-only", repr(load_run_config(self._public_config())))

    def _public_config(self):
        self.write(document())
        return self.path


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "config.json"
        self.data = document()
        self.write()
        self.stdout, self.stderr = StringIO(), StringIO()
        self.calls, self.saved = [], []
        self.bundle = {"symbol": "600875", "llm": {"status": "ok", "usage": {"total_tokens": 42}},
                       "agent": {"status": "complete", "model_calls": 2, "tool_calls": 3,
                                 "stop_code": "model_finished", "stop_reason": "已有证据足够。",
                                 "remaining_gaps": []}}
        self.fake = types.ModuleType("src.pipeline")

        def analyze(*args, **kwargs):
            self.calls.append((args, kwargs))
            kwargs["progress"]("正在获取行情。", 1, 12)
            kwargs["event_sink"]({"kind": "tool", "name": "fetch_source", "status": "ok",
                                  "reason": "补查最新行情。", "summary": "已取得行情样本。",
                                  "arguments": {"source": "price"}})
            # Verify the file is readable before the analysis has returned.
            text = self.logs()[0].read_text(encoding="utf-8")
            self.assertIn("补查最新行情", text)
            return copy.deepcopy(self.bundle)

        def save(bundle, settings=None):
            self.saved.append(bundle)
            directory = settings.report_dir
            directory.mkdir(parents=True, exist_ok=True)
            paths = tuple(directory / name for name in ("x_result.md", "x_log.md", "x_chart.html", "x.json"))
            for target in paths:
                target.write_text("offline fixture", encoding="utf-8")
            return paths

        self.fake.run_analysis, self.fake.save_report = analyze, save

    def write(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def logs(self):
        return sorted(self.directory.rglob("*_log.txt"))

    def invoke(self, arguments=None):
        with patch.dict("sys.modules", {"src.pipeline": self.fake}), \
                patch("src.cli.ROOT", self.directory), \
                patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return main((arguments or ["run"]) + ["--config", str(self.path)])

    def all_text(self):
        return self.stdout.getvalue() + self.stderr.getvalue() + "".join(
            target.read_text(encoding="utf-8") for target in self.logs())

    def test_run_starts_durable_log_and_saves_both_log_types(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.logs()), 1)
        self.assertRegex(self.logs()[0].name, r"^600875_\d{8}_\d{4}_log\.txt$")
        text = self.all_text()
        for expected in ("run_start", "fetch_source", "outputs_saved", "run_finished", '"model_calls":2'):
            self.assertIn(expected, text)
        self.assertIn("[runtime_environment]", text)
        self.assertIn('"python_executable":', text)
        self.assertIn('"plotly_version":', text)
        self.assertIn("x_log.md", text)
        self.assertNotIn("sk-local-test-only", text)
        self.assertEqual(self.calls[0][1]["settings"].report_dir, self.directory / "outputs")

    def test_partial_completion_is_saved_and_has_exit_two_in_txt(self):
        self.bundle["agent"]["status"] = "limited"
        self.bundle["agent"]["remaining_gaps"] = [{"id": "cashflow", "description": "现金流解释尚缺。"}]
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(len(self.saved), 1)
        self.assertIn('"exit_code":2', self.all_text())
        self.assertIn("现金流解释尚缺", self.all_text())

    def test_export_fallback_has_exit_two_and_saved_paths(self):
        self.bundle["export_warnings"] = ["图表不完整。"]
        self.bundle["export_diagnostics"] = [{"component": "offline_chart", "error_class": "ValueError",
                                               "reason_code": "chart_value_error", "plotly_version": "6.3.0"}]
        self.assertEqual(self.invoke(), 2)
        self.assertIn("x_chart.html", self.all_text())
        self.assertIn("[chart_export_failed]", self.all_text())
        self.assertIn('"error_class":"ValueError"', self.all_text())
        self.assertIn('"exit_code":2', self.all_text())

    def test_missing_key_creates_log_before_rejecting_run(self):
        self.data["llm"]["api_key"] = ""
        self.write()
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.calls, [])
        self.assertIn("missing_api_key", self.all_text())
        self.assertEqual(len(self.logs()), 1)

    def test_fatal_exception_records_safe_phase_without_raw_credentials(self):
        def fail(*args, **kwargs):
            kwargs["progress"]("请求 sk-local-test-only 与 Bearer private-test", 1, 12)
            raise RuntimeError("sk-local-test-only raw SDK request")
        self.fake.run_analysis = fail
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(len(self.logs()), 1)
        self.assertIn("run_failed", self.all_text())
        self.assertIn('"phase":"analysis"', self.all_text())
        self.assertIn('"error_class":"RuntimeError"', self.all_text())
        self.assertNotIn("sk-local-test-only", self.all_text())
        self.assertNotIn("private-test", self.all_text())
        self.assertNotIn("raw SDK", self.all_text())

    def test_cancelled_run_preserves_earlier_progress_and_exit_one(self):
        def cancel(*args, **kwargs):
            kwargs["progress"]("已完成模型身份核验。", 0, 12)
            raise KeyboardInterrupt()
        self.fake.run_analysis = cancel
        self.assertEqual(self.invoke(), 1)
        self.assertIn("run_cancelled", self.all_text())
        self.assertIn("已完成模型身份核验", self.all_text())
        self.assertEqual(self.saved, [])

    def test_malformed_configuration_uses_safe_unknown_log(self):
        self.path.write_text('{"llm":{"api_key":"sk-never-echo"', encoding="utf-8")
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.logs()), 1)
        self.assertRegex(self.logs()[0].name, r"^unknown_\d{8}_\d{4}_log\.txt$")
        self.assertIn("configuration_error", self.all_text())
        self.assertNotIn("sk-never-echo", self.all_text())

    def test_cannot_open_log_prevents_any_analysis(self):
        with patch("src.cli.RunLogger.__enter__", side_effect=RunLogError("safe failure")):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.saved, [])

    def test_failed_event_write_interrupts_work_before_another_action(self):
        later_actions = []
        def analyze(*args, **kwargs):
            kwargs["event_sink"]({"kind": "model", "name": "request_start", "summary": "准备请求模型。"})
            later_actions.append("paid request must not happen")
            return self.bundle
        self.fake.run_analysis = analyze
        with patch.object(RunLogger, "event", side_effect=RunLogError("safe failure")):
            self.assertEqual(self.invoke(), 1)
        self.assertEqual(later_actions, [])
        self.assertEqual(self.saved, [])

    def test_legacy_override_retains_configured_logging_directory(self):
        self.data["logging"] = {"directory": "history"}
        self.write()
        self.bundle["llm"]["status"] = "disabled"
        self.assertEqual(self.invoke(["analyze", "000001", "--no-llm", "--output-dir", "other_results"]), 0)
        self.assertEqual(self.calls[0][0], ("000001",))
        self.assertEqual(self.calls[0][1]["settings"].report_dir, self.directory / "other_results")
        self.assertEqual(self.logs()[0].parent, self.directory / "history")


class ScriptTests(unittest.TestCase):
    def test_script_no_arguments_dispatches_to_configured_run(self):
        spec = importlib.util.spec_from_file_location("stock_agent_run_main_test", PROJECT / "run_main.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        with patch("src.cli.main", return_value=0) as cli_main:
            self.assertEqual(script.main([]), 0)
            cli_main.assert_called_once_with(["run"])
        with patch("src.cli.main", return_value=0) as cli_main:
            self.assertEqual(script.main(["--config", "public.json"]), 0)
            cli_main.assert_called_once_with(["run", "--config", "public.json"])

    def test_foreign_cwd_default_config_is_adjacent_to_script(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "standalone"
            project.mkdir()
            shutil.copy2(PROJECT / "run_main.py", project / "run_main.py")
            shutil.copytree(PROJECT / "src", project / "src", ignore=shutil.ignore_patterns("__pycache__"))
            data = document()
            data["task"].update(stock_code="000417", use_llm=False)
            (project / "config.json").write_text(json.dumps(data), encoding="utf-8")
            foreign = base / "elsewhere"
            foreign.mkdir()
            (foreign / "config.json").write_text("not a configuration", encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            result = subprocess.run([sys.executable, str(project / "run_main.py"), "--check"],
                                    cwd=foreign, env=env, capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("000417", result.stdout)
            self.assertIn(str(project / "outputs"), result.stdout)
            self.assertIn(str(project / "logs"), result.stdout)
            self.assertNotIn("sk-local-test-only", result.stdout + result.stderr)
            self.assertFalse((foreign / "logs").exists())


if __name__ == "__main__":
    unittest.main()
