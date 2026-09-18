"""DeepSeek code path with a stubbed chat model (no network)."""
import json

import app.llm as llm
from app.agents.detection import Triage
from app.agents.sourcing import SourcingDecision
from tests.test_pipeline import disruption, trigger


class FakeChat:
    def __init__(self, replies):
        self.replies = replies
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        schema_name = next(k for k in self.replies if k in messages[0].content)
        return type("Msg", (), {"content": self.replies[schema_name]})()


def enable_llm(monkeypatch, replies):
    fake = FakeChat(replies)
    monkeypatch.setattr(llm.settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(llm, "get_chat_model", lambda: fake)
    return fake


def test_structured_call_parses_fenced_json(monkeypatch):
    reply = "```json\n" + json.dumps({"severity": "high", "title": "t", "summary": "s", "root_cause": "r",
                                      "confidence": 0.9}) + "\n```"
    enable_llm(monkeypatch, {"Triage": reply})
    result, source = llm.structured_call("sys Triage", "user", Triage, lambda: None)
    assert source == "deepseek" and result.severity == "high"


def test_invalid_llm_output_falls_back(monkeypatch):
    enable_llm(monkeypatch, {"Triage": "not json"})
    fb = Triage(severity="low", title="fb", summary="", root_cause="", confidence=0.1)
    result, source = llm.structured_call("sys Triage", "user", Triage, lambda: fb)
    assert source == "fallback" and result is fb


def test_llm_drives_agents_but_cannot_downgrade_or_pick_unknown_option(monkeypatch):
    triage = json.dumps({"severity": "low", "title": "LLM headline", "summary": "LLM summary",
                         "root_cause": "vessel rollover", "confidence": 0.8})
    sourcing = json.dumps({"recommended_option_id": "does-not-exist", "rationale": "x", "risks": [],
                           "next_steps": [], "confidence": 0.9})
    enable_llm(monkeypatch, {"recommended_option_id": sourcing, "severity": triage,
                             "narrative": json.dumps({"narrative": "LLM narrative", "key_risks": ["r"]}),
                             "what_went_well": json.dumps({"summary": "pm", "what_went_well": [],
                                                           "what_went_wrong": [], "lessons": []})})
    run = trigger("shipment_delay")["runs"][0]
    d = disruption(run["disruption_id"])
    assert d.title == "LLM headline" and d.triage["source"] == "deepseek"
    assert d.severity == d.anomaly["rule_severity"]  # LLM said "low"; rules engine floor wins
    assert d.impact["narrative"] == "LLM narrative"
    assert d.recommendation["source"] == "fallback"  # unknown option id rejected
    assert d.recommendation["recommended_option_id"] in {o["id"] for o in d.options}
    assert SourcingDecision.model_validate_json(sourcing)
