"""The AI step, with the Anthropic client replaced by a fake (no network, no cost)."""

import json
from types import SimpleNamespace

import anthropic
import pytest

from dusterscan import ai
from dusterscan.report_html import render_report
from tests.test_scan import run_scan

SAMPLE = {
    "overall_status": "problems",
    "overall_assessment": "Lean condition and a pending misfire on cylinder 4.",
    "safe_to_drive": "yes_with_caution",
    "safe_to_drive_reason": "Misfires can overheat the catalytic converter.",
    "findings": [{
        "title": "Intake air leak causing lean mixture", "severity": "high", "system": "Engine",
        "related_codes": ["P0171", "P0304"], "evidence": "LTFT +13%, P0171 stored",
        "explanation": "Extra unmeasured air is entering the engine.",
        "likely_causes": [{"cause": "Cracked intake hose", "likelihood": "high"}],
        "tests": ["Smoke test the intake"], "fixes": ["Replace the cracked hose"],
        "diy_difficulty": "moderate", "urgency": "soon",
    }],
    "watch_list": [{"item": "Coolant temperature", "why": "baseline", "how_to_monitor": "gauge"}],
    "data_gaps": ["No Renault-specific live data"],
    "maintenance_tips": ["Check spark plugs"],
}


class FakeStream:
    def __init__(self, calls, message):
        self.calls, self.message = calls, message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.message


def fake_client(calls, message):
    def stream(**kwargs):
        calls.append(kwargs)
        return FakeStream(calls, message)
    return lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def make_message(text, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, model=ai.MODEL,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )


def test_analyze_request_and_render(monkeypatch):
    _, report = run_scan()
    calls = []
    monkeypatch.setattr(anthropic, "Anthropic", fake_client(calls, make_message(json.dumps(SAMPLE))))

    result = ai.analyze(report, "sk-test", "es", "rough idle when cold")

    req = calls[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["format"]["schema"] is ai.SCHEMA
    assert req["fallbacks"] == "default"
    user = req["messages"][0]["content"]
    assert "Spanish" in user and "rough idle when cold" in user and "P0171" in user
    # the model gets readable English, not translation keys
    assert "System too lean (bank 1)" in user and '"key":' not in user
    assert "scan_log" not in user and "adapter_log" not in user and "61 80" not in user
    assert result["_language"] == "es"

    html_es = render_report(report, result, "es")
    assert "Intake air leak causing lean mixture" in html_es and "Manejar con precaución" in html_es
    assert "Este análisis se escribió" not in html_es
    html_en = render_report(report, result, "en")
    assert "Drive with caution" in html_en and "This analysis was written in Spanish" in html_en


def test_refusal_is_reported(monkeypatch):
    _, report = run_scan()
    monkeypatch.setattr(anthropic, "Anthropic", fake_client([], make_message("", "refusal")))
    with pytest.raises(ai.AIError):
        ai.analyze(report, "sk-test")


def test_schema_is_strict():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(ai.SCHEMA)
