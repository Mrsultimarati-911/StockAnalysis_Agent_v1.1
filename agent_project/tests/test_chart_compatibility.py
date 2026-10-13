"""Offline chart regression coverage for the supported Plotly 6.x range."""
from __future__ import annotations

from datetime import date, timedelta
import json
import math
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import plotly.graph_objects as go
import plotly.io as pio

from src.charts import render_chart
from src.technical import analyze_technical


def chart_bundle():
    days = []
    day = date(2026, 10, 9)
    while len(days) < 140:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    records = []
    for index, day in enumerate(reversed(days)):
        close = 10 + index * 0.04 + math.sin(index / 4) * 0.25
        records.append({"date": day.isoformat(), "open": close - 0.05,
                        "high": close + 0.2, "low": close - 0.2, "close": close,
                        "volume": 1000 + index * 10})
    cutoff = "2026-10-09T15:30:00+08:00"
    return {"symbol": "000417", "generated_at": cutoff,
            "company": {"name": "测试公司"}, "sources": {"price": {"records": records}},
            "technical": analyze_technical(records, as_of=cutoff)}


def chart_payload(document):
    match = re.search(r'<script id="stock-chart-data" type="application/json">(.*?)</script>',
                      document, re.S)
    if not match:
        raise AssertionError("chart evidence metadata missing")
    return json.loads(match.group(1))


class ChartCompatibilityTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def test_default_plotly_render_keeps_ohlc_and_offline_layers(self):
        bundle = chart_bundle()
        original = go.Candlestick
        calls = []

        def capture(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)

        with patch("plotly.graph_objects.Candlestick", side_effect=capture), \
                patch("plotly.io.to_html", wraps=pio.to_html) as export:
            document = render_chart(bundle)
        self.assertEqual(len(calls), 1)
        candle = calls[0]
        self.assertEqual(candle["hoverinfo"], "text")
        self.assertNotIn("hovertemplate", candle)
        self.assertEqual(len(candle["hovertext"]), 140)
        latest = bundle["sources"]["price"]["records"][-1]
        for label, field in (("开盘", "open"), ("最高", "high"), ("最低", "low"), ("收盘", "close")):
            self.assertIn(f"{label} {latest[field]:.4f}", candle["hovertext"][-1])
        self.assertIn("2026-10-09", candle["hovertext"][-1])
        self.assertIn("前复权 · 元/股", candle["hovertext"][-1])
        data = chart_payload(document)
        self.assertTrue(data["verified"])
        self.assertEqual(data["sample_size"], 140)
        self.assertEqual(candle["open"], data["series"]["open"])
        self.assertEqual(candle["close"], data["series"]["close"])
        # Plotly versions differ in Unicode escaping, so inspect the real traces.
        trace_names = {trace.name for trace in export.call_args.args[0].data}
        for name in ("MA5", "MA10", "MA20", "MA60", "MA120", "成交量", "MACD柱", "DIF", "DEA"):
            self.assertIn(name, trace_names)
        self.assertTrue('"type":"candlestick"' in document, "missing candlestick layer")
        self.assertNotRegex(document, r'<script\b[^>]*\bsrc\s*=')
        self.assertNotRegex(document, r'<link\b[^>]*\bhref\s*=')

    def test_plotly_63_rejection_rule_accepts_chart_and_preserves_payload(self):
        bundle = chart_bundle()
        expected = chart_payload(render_chart(bundle))
        original = go.Candlestick
        calls = []

        def legacy_candlestick(*args, **kwargs):
            # Match Plotly 6.3's real constructor rejection while retaining real
            # graph objects and serialization for every supported property.
            if "hovertemplate" in kwargs:
                raise ValueError("Invalid property specified for Candlestick: 'hovertemplate'")
            calls.append(kwargs)
            return original(*args, **kwargs)

        with patch("plotly.graph_objects.Candlestick", side_effect=legacy_candlestick):
            document = render_chart(bundle)
        self.assertEqual(len(calls), 1)
        self.assertEqual(chart_payload(document), expected)
        self.assertTrue('"type":"candlestick"' in document, "missing candlestick layer")
        self.assertTrue('"hoverinfo":"text"' in document, "missing legacy-compatible hover")

    def test_untrusted_labels_remain_escaped_with_legacy_hover(self):
        bundle = chart_bundle()
        injected = '</script><script>alert("xss")</script>'
        bundle["company"]["name"] = injected
        bundle["technical"]["supports"] = [{"price": 10.5, "label": injected}]
        document = render_chart(bundle)
        self.assertNotIn(injected, document)
        self.assertIn("&lt;/script&gt;", document)
        self.assertEqual(chart_payload(document)["levels"][0]["basis"], injected)
        self.assertTrue('"type":"candlestick"' in document, "missing candlestick layer")


if __name__ == "__main__":
    unittest.main()
