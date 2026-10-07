"""Klient Gemini: łańcuch modeli, timeout per próba, wyłączone AFC.

Wydzielony z layout_v2, żeby renderer raportu nie trzymał logiki HTTP.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Optional

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

logger = logging.getLogger(__name__)

GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"
# Główny model, potem ten, który realnie kończy raport, na końcu krótka próba.
GEMINI_MODEL_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
]
GEMINI_FALLBACK_CHAIN = GEMINI_MODEL_CHAIN[1:]
GEMINI_TIMEOUT_SECONDS = 360
# 3.8: jedna próba i przejście na 3.5 po 60 s od jej startu.
# 3.5: 180 s, bo limit 75 s kończył się 504 po stronie Google.
# 3.7: ostatnia krótka próba, gdy 3.5 też nie odpowie.
GEMINI_ATTEMPT_TIMEOUTS = {
    "gemini-3.8-flash": 60,
    "gemini-3.5-flash": 180,
    "gemini-3.7-flash": 20,
}
GEMINI_PRIMARY_SWITCH_SECONDS = 60
GEMINI_ATTEMPT_TIMEOUT_SECONDS = GEMINI_ATTEMPT_TIMEOUTS[GEMINI_DEFAULT_MODEL]


def build_model_chain() -> list[str]:
    """Podstawowy model (GEMINI_MODEL lub gemini-3.8-flash) + reszta łańcucha.

    Pusty GEMINI_MODEL (np. sekret Actions bez wartości) zostawia domyślną kolejność.
    """
    primary = os.getenv("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL
    chain = []
    for model in [primary] + GEMINI_MODEL_CHAIN:
        if model not in chain:
            chain.append(model)
    return chain


def _attempt_limit(model_name: str) -> float:
    return float(GEMINI_ATTEMPT_TIMEOUTS.get(model_name, GEMINI_PRIMARY_SWITCH_SECONDS))


def _is_slow_error(err: BaseException) -> bool:
    """Timeout albo 504: model liczył za długo, druga próba 3.5 ma sens."""
    if isinstance(err, TimeoutError):
        return True
    text = str(err).lower()
    return any(token in text for token in ("504", "deadline", "timeout", "timed out"))


def _generate_content_config():
    """Wyłącza AFC — inaczej SDK woła narzędzia i request wisi aż do 504."""
    if genai_types is None:
        return None
    afc = getattr(genai_types, "AutomaticFunctionCallingConfig", None)
    cfg_cls = getattr(genai_types, "GenerateContentConfig", None)
    if afc is None or cfg_cls is None:
        return None
    return cfg_cls(automatic_function_calling=afc(disable=True))


def _is_daily_quota_error(err_text: str) -> bool:
    t = err_text.lower()
    return ("resource_exhausted" in t or "429" in t) and (
        "perday" in t or "per day" in t or "limit: 0" in t
    )


def _generate_content_with_timeout(
    prompt: str,
    api_key: str,
    model_name: str,
    timeout_seconds: float,
):
    result_queue = queue.Queue(maxsize=1)
    timeout_ms = max(1, int(timeout_seconds * 1000))

    def run_request():
        client = None
        try:
            client = genai.Client(
                api_key=api_key,
                http_options=genai_types.HttpOptions(
                    timeout=timeout_ms,
                ),
            )
            kwargs = {
                "model": model_name,
                "contents": prompt,
            }
            config = _generate_content_config()
            if config is not None:
                kwargs["config"] = config
            response = client.models.generate_content(**kwargs)
            result_queue.put((True, response))
        except BaseException as exc:
            result_queue.put((False, exc))
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except TypeError:
                    pass

    request_thread = threading.Thread(
        target=run_request,
        name=f"gemini-{model_name}",
        daemon=True,
    )
    request_thread.start()
    request_thread.join(timeout_seconds)

    if request_thread.is_alive():
        raise TimeoutError(
            f"Przekroczono pozostały limit {timeout_seconds:.0f}s "
            f"dla modelu {model_name}."
        )

    succeeded, result = result_queue.get_nowait()
    if succeeded:
        return result
    raise result


def _sleep_until(target: float, deadline: float) -> None:
    """Dosypia do `target`, ale nie dalej niż do łącznego budżetu."""
    delay = min(target, deadline) - time.monotonic()
    if delay > 0:
        logger.info(f"Gemini — czekam {delay:.0f}s przed kolejnym modelem...")
        time.sleep(delay)


def _budget_left(deadline: float, last_error: Optional[Exception]) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"Przekroczono łączny limit {GEMINI_TIMEOUT_SECONDS}s dla Gemini."
        ) from last_error
    return remaining


def call_gemini(prompt: str, api_key: str) -> tuple[str, str]:
    """Wywołanie Gemini z łańcuchem modeli zapasowych.

    Zwraca (tekst, nazwa_modelu). Po wyczerpaniu łańcucha rzuca wyjątek.

    gemini-3.8-flash ma jedną próbę (60 s). Jeśli nie zwróci tekstu, 3.5 startuje
    60 s po początku tej próby — wcześniejszy 503 nie przeskakuje od razu.
    Dobowa quota przeskakuje model bez czekania. gemini-3.5-flash ma drugą próbę
    tylko po timeoutcie albo 504. Przy 503 idzie dalej. gemini-3.7-flash to jedna
    krótka próba na końcu.
    """
    chain = build_model_chain()
    deadline = time.monotonic() + GEMINI_TIMEOUT_SECONDS
    logger.info(f"Gemini — łańcuch modeli: {' -> '.join(chain)}")
    limits = ", ".join(
        f"{model.split('-')[1]} {secs}s" for model, secs in GEMINI_ATTEMPT_TIMEOUTS.items()
    )
    logger.info(
        f"Gemini — łączny timeout: {GEMINI_TIMEOUT_SECONDS}s (limity prób: {limits})."
    )

    last_error: Optional[Exception] = None
    for index, model_name in enumerate(chain):
        _budget_left(deadline, last_error)
        max_attempts = 2 if model_name == "gemini-3.5-flash" else 1
        for attempt in range(1, max_attempts + 1):
            remaining = _budget_left(deadline, last_error)
            started = time.monotonic()
            try:
                logger.info(
                    f"Gemini — [{model_name}] próba {attempt}/{max_attempts}..."
                )
                response = _generate_content_with_timeout(
                    prompt,
                    api_key,
                    model_name,
                    min(remaining, _attempt_limit(model_name)),
                )
                if not response.text:
                    raise Exception("Pusta odpowiedź z Gemini.")
                logger.info(f"Gemini — odpowiedź z modelu {model_name}.")
                return response.text, model_name
            except Exception as e:
                last_error = e
                logger.warning(f"Gemini — [{model_name}] próba {attempt} nieudana: {e}")
                if _is_daily_quota_error(str(e)):
                    logger.info(
                        f"Gemini — [{model_name}] wyczerpana dobowa quota, "
                        "przechodzę do kolejnego modelu."
                    )
                    break
                if (
                    model_name == "gemini-3.5-flash"
                    and attempt < max_attempts
                    and _is_slow_error(e)
                ):
                    continue
                next_model = chain[index + 1] if index + 1 < len(chain) else None
                if model_name == "gemini-3.8-flash" and next_model == "gemini-3.5-flash":
                    _sleep_until(started + GEMINI_PRIMARY_SWITCH_SECONDS, deadline)
                break
    raise last_error or Exception("Gemini: wszystkie modele nieudane")
