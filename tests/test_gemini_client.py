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
    monkeypatch.setattr(gemini_client.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(gemini_client, "genai_types", _Types)

    def install(script):
        fake = _FakeGenai(script, calls)
        monkeypatch.setattr(gemini_client, "genai", fake)
        return calls, sleeps, fake
    return install


def test_38_success_does_not_call_later_models(gemini_env):
    calls, sleeps, fake = gemini_env({"gemini-3.8-flash": ["RAPORT"]})
    text, model = gemini_client.call_gemini("prompt", "key")
    assert text == "RAPORT" and model == "gemini-3.8-flash"
    assert calls == ["gemini-3.8-flash"]
    assert sleeps == []
    assert fake.http_options.timeout == gemini_client.GEMINI_ATTEMPT_TIMEOUTS["gemini-3.8-flash"] * 1000




def test_daily_quota_on_38_skips_wait(gemini_env):
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [Exception(_DAILY_QUOTA_MSG)],
        "gemini-3.5-flash": ["RAPORT Z 3.5"],
    })
    text, model = gemini_client.call_gemini("prompt", "key")
    assert text == "RAPORT Z 3.5" and model == "gemini-3.5-flash"
    assert calls == ["gemini-3.8-flash", "gemini-3.5-flash"]
    assert sleeps == []




def test_35_retries_after_client_timeout(gemini_env):
    slow = TimeoutError("Przekroczono pozostały limit 180s dla modelu gemini-3.5-flash.")
    calls, _sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [Exception(_DAILY_QUOTA_MSG)],
        "gemini-3.5-flash": [slow, "RAPORT Z 3.5"],
    })
    text, model = gemini_client.call_gemini("prompt", "key")
    assert text == "RAPORT Z 3.5" and model == "gemini-3.5-flash"
    assert calls == ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash"]




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




def test_gemini_chain_all_fail_raises(gemini_env):
    gemini_env({
        "gemini-3.8-flash": [Exception(_OVERLOAD_MSG)],
        "gemini-3.5-flash": [Exception(_OVERLOAD_MSG)],
        "gemini-3.7-flash": [Exception(_OVERLOAD_MSG)],
    })
    with pytest.raises(Exception, match="503"):
        gemini_client.call_gemini("prompt", "key")


def test_build_model_chain_dedup_with_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.7-flash")
    assert gemini_client.build_model_chain() == [
        "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash",
    ]


def test_build_model_chain_default(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert gemini_client.build_model_chain() == [
        "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.7-flash",
    ]


def test_build_model_chain_empty_env_uses_default(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "")
    assert gemini_client.build_model_chain() == [
        "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.7-flash",
    ]


def test_is_daily_quota_error():
    assert gemini_client._is_daily_quota_error(_DAILY_QUOTA_MSG)
    assert gemini_client._is_daily_quota_error("429 ... limit: 0, model: x")
    assert not gemini_client._is_daily_quota_error(_OVERLOAD_MSG)
    assert not gemini_client._is_daily_quota_error(
        "429 RESOURCE_EXHAUSTED limit: 20 PerMinute")
    assert gemini_client._is_slow_error(TimeoutError("limit"))
    assert gemini_client._is_slow_error(Exception("504 DEADLINE_EXCEEDED"))
    assert not gemini_client._is_slow_error(Exception(_OVERLOAD_MSG))


def test_503_retries_same_model_with_backoff_then_success(gemini_env):
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [Exception(_OVERLOAD_MSG), Exception(_OVERLOAD_MSG), "RAPORT Z 3.8"],
    })
    text, model = gemini_client.call_gemini("prompt", "key")
    assert text == "RAPORT Z 3.8" and model == "gemini-3.8-flash"
    assert calls == ["gemini-3.8-flash"] * 3
    assert sleeps == [5, 15]


@pytest.mark.parametrize("msg", [
    "429 RESOURCE_EXHAUSTED. Rate limit per minute.",
    "500 INTERNAL. An internal error has occurred.",
    "502 Bad Gateway",
    "504 DEADLINE_EXCEEDED. Deadline expired.",
])
def test_other_transient_errors_are_retried(gemini_env, msg):
    calls, sleeps, _fake = gemini_env({"gemini-3.8-flash": [Exception(msg), "OK"]})
    assert gemini_client.call_gemini("prompt", "key") == ("OK", "gemini-3.8-flash")
    assert sleeps == [5]


def test_503_exhausts_attempts_then_next_model(gemini_env):
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [Exception(_OVERLOAD_MSG)] * 4,
        "gemini-3.5-flash": [Exception(_OVERLOAD_MSG), "RAPORT Z 3.5"],
    })
    text, model = gemini_client.call_gemini("prompt", "key")
    assert model == "gemini-3.5-flash"
    assert calls == ["gemini-3.8-flash"] * 4 + ["gemini-3.5-flash"] * 2
    assert sleeps == [5, 15, 30, 5]


def test_timeout_on_38_moves_on_without_wait(gemini_env):
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [TimeoutError("limit 60s")],
        "gemini-3.5-flash": ["RAPORT Z 3.5"],
    })
    assert gemini_client.call_gemini("prompt", "key") == ("RAPORT Z 3.5", "gemini-3.5-flash")
    assert sleeps == []


def test_permanent_error_skips_model(gemini_env):
    calls, sleeps, _fake = gemini_env({
        "gemini-3.8-flash": [Exception("400 INVALID_ARGUMENT")],
        "gemini-3.5-flash": ["OK"],
    })
    assert gemini_client.call_gemini("prompt", "key") == ("OK", "gemini-3.5-flash")
    assert calls == ["gemini-3.8-flash", "gemini-3.5-flash"] and sleeps == []


def test_run64_scenario_uses_budget_then_raises(monkeypatch):
    """Run #64: 3.8 timeout, potem same 503 — teraz ponawiamy aż do końca budżetu."""
    clock = {"t": 0.0}
    calls = []
    monkeypatch.setattr(gemini_client.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        gemini_client.time, "sleep", lambda seconds: clock.__setitem__("t", clock["t"] + seconds)
    )
    monkeypatch.setattr(gemini_client.random, "uniform", lambda a, b: 0.0)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(gemini_client, "genai_types", _Types)

    def fake_generate(prompt, api_key, model_name, timeout_seconds):
        calls.append(model_name)
        if model_name == "gemini-3.8-flash":
            clock["t"] += timeout_seconds
            raise TimeoutError("limit")
        clock["t"] += 1
        raise Exception(_OVERLOAD_MSG)

    monkeypatch.setattr(gemini_client, "_generate_content_with_timeout", fake_generate)
    with pytest.raises(Exception):
        gemini_client.call_gemini("prompt", "key")
    assert calls.count("gemini-3.5-flash") >= 4
    assert calls.count("gemini-3.7-flash") >= 4
    assert clock["t"] > 300  # budżet realnie wykorzystany (wcześniej ~62 s)
    assert clock["t"] <= gemini_client.GEMINI_TIMEOUT_SECONDS


def test_backoff_has_jitter_bounded():
    for i, base in enumerate(gemini_client.GEMINI_RETRY_BACKOFF_SECONDS):
        d = gemini_client._backoff_delay(i)
        assert base <= d <= base * (1 + gemini_client.GEMINI_RETRY_JITTER)
    assert gemini_client._is_transient_error(Exception(_OVERLOAD_MSG))
    assert not gemini_client._is_transient_error(Exception(_DAILY_QUOTA_MSG))
