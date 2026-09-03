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
GEMINI_FALLBACK_CHAIN = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash"]
GEMINI_MODEL_BACKOFF = [0, 10]
GEMINI_TIMEOUT_SECONDS = 360
GEMINI_ATTEMPT_TIMEOUT_SECONDS = 75


def build_model_chain() -> list[str]:
    """Podstawowy model (GEMINI_MODEL lub gemini-3.8-flash) + zapasowe."""
    primary = os.getenv("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL
    chain = []
    for model in [primary] + GEMINI_FALLBACK_CHAIN:
        if model not in chain:
            chain.append(model)
    return chain


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


def call_gemini(prompt: str, api_key: str) -> tuple[str, str]:
    """Wywołanie Gemini z łańcuchem modeli zapasowych.

    Zwraca (tekst, nazwa_modelu). Po wyczerpaniu łańcucha rzuca wyjątek.
    """
    chain = build_model_chain()
    deadline = time.monotonic() + GEMINI_TIMEOUT_SECONDS
    logger.info(f"Gemini — łańcuch modeli: {' -> '.join(chain)}")
    logger.info(
        f"Gemini — łączny timeout: {GEMINI_TIMEOUT_SECONDS}s "
        f"(max {GEMINI_ATTEMPT_TIMEOUT_SECONDS}s na próbę)."
    )

    last_error: Optional[Exception] = None
    for model_name in chain:
        for attempt, delay in enumerate(GEMINI_MODEL_BACKOFF, start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Przekroczono łączny limit {GEMINI_TIMEOUT_SECONDS}s dla Gemini."
                ) from last_error
            if delay:
                if delay >= remaining:
                    raise TimeoutError(
                        f"Przekroczono łączny limit {GEMINI_TIMEOUT_SECONDS}s dla Gemini."
                    ) from last_error
                logger.info(f"Gemini — [{model_name}] czekam {delay}s przed próbą {attempt}...")
                time.sleep(delay)
                remaining = deadline - time.monotonic()
            try:
                logger.info(
                    f"Gemini — [{model_name}] próba {attempt}/{len(GEMINI_MODEL_BACKOFF)}..."
                )
                attempt_timeout = min(remaining, float(GEMINI_ATTEMPT_TIMEOUT_SECONDS))
                response = _generate_content_with_timeout(
                    prompt,
                    api_key,
                    model_name,
                    attempt_timeout,
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
                        f"przechodzę do kolejnego modelu."
                    )
                    break
    raise last_error or Exception("Gemini: wszystkie modele nieudane")
