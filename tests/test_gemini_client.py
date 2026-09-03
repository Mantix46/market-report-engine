"""Łańcuch modeli Gemini — testy gemini_client (wydzielone z layout_v2)."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_client  # noqa: E402


_DAILY_QUOTA_MSG = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric "
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20"
)
_OVERLOAD_MSG = "503 UNAVAILABLE. This model is currently experiencing high demand."


class _FakeModels:
    def __init__(self, script, calls):
        self.script = script
        self.calls = calls

    def generate_content(self, model, contents, **kwargs):
        self.calls.append(model)
        outcomes = self.script.get(model, [])
        idx = sum(1 for c in self.calls if c == model) - 1
        outcome = outcomes[idx] if idx < len(outcomes) else Exception(_OVERLOAD_MSG)
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {"text": outcome})()


class _FakeGenai:
    def __init__(self, script, calls):
        self._script, self._calls = script, calls
        self.http_options = None

    def Client(self, api_key, http_options=None):
        self.http_options = http_options
        models = _FakeModels(self._script, self._calls)
        return type("C", (), {"models": models, "close": lambda self: None})()


class _HttpOptions:
    def __init__(self, timeout=None):
        self.timeout = timeout


class _Types:
    HttpOptions = _HttpOptions
    AutomaticFunctionCallingConfig = None
    GenerateContentConfig = None


@pytest.fixture
def gemini_env(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(gemini_client, "genai_types", _Types)

    def install(script):
        fake = _FakeGenai(script, calls)
        monkeypatch.setattr(gemini_client, "genai", fake)
        return calls, sleeps, fake
    return install


def test_gemini_chain_falls_through_on_503(gemini_env):
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
        "gemini-3.7-flash": ["RAPORT Z 3.7"],
    })
    text, model = gemini_client.call_gemini("prompt", "key")
    assert text == "RAPORT Z 3.7"
    assert model == "gemini-3.7-flash"
    assert calls == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.7-flash"]
    assert 10 in sleeps


def test_gemini_attempt_timeout_is_capped(gemini_env):
    _calls, _sleeps, fake = gemini_env({"gemini-3.8-flash": ["RAPORT"]})
    gemini_client.call_gemini("prompt", "key")
    assert fake.http_options.timeout <= gemini_client.GEMINI_ATTEMPT_TIMEOUT_SECONDS * 1000
    assert fake.http_options.timeout >= 1


def test_gemini_hard_timeout_returns_control(monkeypatch):
    release_request = threading.Event()

    class BlockingModels:
        def generate_content(self, model, contents, **kwargs):
            release_request.wait()
            return type("R", (), {"text": "ZA PÓŹNO"})()

    class BlockingGenai:
        def Client(self, api_key, http_options=None):
            return type("C", (), {"models": BlockingModels(), "close": lambda self: None})()

    monkeypatch.setattr(gemini_client, "genai", BlockingGenai())
    monkeypatch.setattr(gemini_client, "genai_types", _Types)
    try:
        with pytest.raises(TimeoutError, match="Przekroczono .*limit"):
            gemini_client._generate_content_with_timeout(
                "prompt",
                "key",
                "model",
                timeout_seconds=0.01,
            )
    finally:
        release_request.set()


def test_gemini_chain_skips_daily_quota_fast(gemini_env):
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [Exception(_DAILY_QUOTA_MSG)],
        "gemini-3.7-flash": [Exception(_DAILY_QUOTA_MSG)],
        "gemini-3.5-flash": ["RAPORT Z 3.5"],
    })
    text, model = gemini_client.call_gemini("prompt", "key")
    assert text == "RAPORT Z 3.5"
    assert model == "gemini-3.5-flash"
    assert calls == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"]
    assert sleeps == []


def test_gemini_504_retries_then_fallback(gemini_env):
    deadline = Exception("504 DEADLINE_EXCEEDED. Deadline expired before operation could complete.")
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [deadline, deadline],
        "gemini-3.7-flash": ["RAPORT Z 3.7"],
    })
    text, model = gemini_client.call_gemini("prompt", "key")
    assert text == "RAPORT Z 3.7" and model == "gemini-3.7-flash"
    assert calls == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.7-flash"]
    assert 10 in sleeps


def test_gemini_chain_all_fail_raises(gemini_env):
    gemini_env({
        "gemini-3.8-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
        "gemini-3.7-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
        "gemini-3.5-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG)],
    })
    with pytest.raises(Exception):
        gemini_client.call_gemini("prompt", "key")


def test_build_model_chain_dedup_with_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.7-flash")
    assert gemini_client.build_model_chain() == [
        "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash",
    ]


def test_build_model_chain_default(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert gemini_client.build_model_chain() == [
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash",
    ]


def test_is_daily_quota_error():
    assert gemini_client._is_daily_quota_error(_DAILY_QUOTA_MSG)
    assert gemini_client._is_daily_quota_error("429 ... limit: 0, model: x")
    assert not gemini_client._is_daily_quota_error(_OVERLOAD_MSG)
    assert not gemini_client._is_daily_quota_error(
        "429 RESOURCE_EXHAUSTED limit: 20 PerMinute")
