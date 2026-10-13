"""Chart failures must remain diagnosable without exposing request secrets."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings
from src.pipeline import save_report


def evidence():
    return {"symbol": "000417", "generated_at": "2026-10-13T12:05:41+08:00",
            "company": {"name": "图表验收", "business": "合成验收业务"}, "technical": {"available": False},
            "sources": {}, "risk": {}, "llm": {"status": "disabled"},
            "agent": {"enabled": False, "status": "disabled"}}


class ChartExportTests(unittest.TestCase):
    def test_failure_keeps_evidence_and_safe_diagnostic(self):
        bundle = evidence()
        with TemporaryDirectory() as directory:
            with patch("src.charts.render_chart", side_effect=ValueError("sk-private-renderer-value raw request")):
                paths = save_report(bundle, Settings(report_dir=Path(directory)))
            diagnostic = bundle["export_diagnostics"][0]
            self.assertEqual(diagnostic["error_class"], "ValueError")
            self.assertEqual(diagnostic["reason_code"], "chart_value_error")
            self.assertTrue(diagnostic["python_version"])
            self.assertTrue(diagnostic["plotly_version"])
            self.assertIn("ValueError", paths.log_path.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(paths.json_path.read_text(encoding="utf-8"))["export_diagnostics"], [diagnostic])
            for path in paths:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("sk-private-renderer-value", text)
                self.assertNotIn("raw request", text)

    def test_successful_retry_clears_stale_export_failure(self):
        bundle = evidence()
        with TemporaryDirectory() as directory:
            settings = Settings(report_dir=Path(directory))
            with patch("src.charts.render_chart", side_effect=TypeError("private detail")):
                first = save_report(bundle, settings)
            before = first.json_path.read_bytes()
            with patch("src.charts.render_chart", return_value="<!doctype html><title>已修复</title>"):
                second = save_report(deepcopy(bundle), settings)
            saved = json.loads(second.json_path.read_text(encoding="utf-8"))
            self.assertNotIn("export_warnings", saved)
            self.assertNotIn("export_diagnostics", saved)
            self.assertNotIn("绘图异常类别", second.log_path.read_text(encoding="utf-8"))
            self.assertNotEqual(first.json_path, second.json_path)
            self.assertEqual(first.json_path.read_bytes(), before)

    def test_missing_dependency_is_classified_without_raw_cause(self):
        error = RuntimeError("private wrapper")
        error.__cause__ = ImportError("sk-private-import-cause")
        bundle = evidence()
        with TemporaryDirectory() as directory:
            with patch("src.charts.render_chart", side_effect=error):
                paths = save_report(bundle, Settings(report_dir=Path(directory)))
            self.assertEqual(bundle["export_diagnostics"][0]["reason_code"], "dependency_unavailable")
            self.assertNotIn("sk-private-import-cause", paths.json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
