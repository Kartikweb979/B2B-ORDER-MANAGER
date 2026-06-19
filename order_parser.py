"""
Parse messy B2B WhatsApp order inquiries into structured JSON.

Usage:
  set GEMINI_API_KEY=your_key
  python order_parser.py "Bhaiya 2000 pizza boxes bhej do brown kraft paper mein 3-ply, kal tak"
"""

from __future__ import annotations

import json
from dotenv import load_dotenv
load_dotenv()
import os
import sys
import time
from datetime import date
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

ORDER_FIELDS = (
    "ClientName",
    "ProductType",
    "Quantity",
    "PlyType",
    "Material",
    "DeliveryDate",
)

# gemini-2.0-flash was shut down 2026-06-01; use a current free-tier model.
DEFAULT_MODEL = "gemini-2.5-flash-lite"
FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash-lite")

MAX_RETRIES = 4
RETRY_BASE_DELAY_SEC = 2.0
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

SYSTEM_PROMPT = """You are an expert B2B order parser for a corrugated box manufacturing business. 
Extract data from the user's messy Hindi/English/Hinglish message into ONLY a strict JSON format with these exact keys:
 ClientName, ProductType, Quantity, PlyType, Material, and DeliveryDate.

CRITICAL RULES FOR EXTRACTION:
1. Pay very close attention to conversational Hinglish for names. 
If the user says 'Bhaiya Sonu bolra hu', 'Ramesh here', or 'Amit ki taraf se', extract 'Sonu', 'Ramesh', or 'Amit' as the ClientName. 
Do not use 'Not Provided' if a name is hidden in casual greetings.
2. If a specific detail is genuinely missing, only then return 'Not Provided'.
3. Output valid JSON only. No markdown, no explanation."""

USER_PROMPT_TEMPLATE = """Today's date: {today}

Order message:
{message}"""


def _empty_order() -> dict[str, str]:
    return {field: "Not Provided" for field in ORDER_FIELDS}


def _normalize_order(data: Any) -> dict[str, str]:
    result = _empty_order()
    if not isinstance(data, dict):
        return result

    for field in ORDER_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"null", "none", "n/a", "na", "-"}:
            result[field] = text

    return result


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return exc.code in RETRYABLE_STATUS_CODES
    if isinstance(exc, genai_errors.ClientError):
        return exc.code == 429
    msg = str(exc).lower()
    return any(token in msg for token in ("503", "429", "unavailable", "high demand", "overloaded"))


def _should_try_fallback_model(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ClientError) and exc.code in {401, 403}:
        return False
    if isinstance(exc, genai_errors.APIError):
        return exc.code in RETRYABLE_STATUS_CODES or exc.code == 404
    return _is_retryable_error(exc)


def _models_to_try() -> list[str]:
    primary = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    env_fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", "")
    fallbacks = (
        [m.strip() for m in env_fallbacks.split(",") if m.strip()]
        if env_fallbacks
        else list(FALLBACK_MODELS)
    )

    models = [primary]
    for model in fallbacks:
        if model not in models:
            models.append(model)
    return models


def _generate_with_retries(
    client: genai.Client,
    model: str,
    prompt: str,
    config: types.GenerateContentConfig,
) -> Any:
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            return client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            last_error = exc
            if isinstance(exc, genai_errors.APIError) and exc.code == 404:
                raise
            if _is_retryable_error(exc) and attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY_SEC * (2 ** attempt)
                print(f"[WARN] {model} attempt {attempt + 1}/{MAX_RETRIES} failed ({exc}); retrying in {delay:.0f}s")
                time.sleep(delay)
                continue
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("generate_content failed without an error")


def format_parse_error(exc: Exception) -> str:
    """Return a short, user-friendly message for Telegram or CLI output."""
    if isinstance(exc, genai_errors.ServerError) and exc.code == 503:
        return (
            "Gemini is temporarily overloaded (high demand). "
            "I retried automatically — please send the order again in a minute."
        )
    if isinstance(exc, genai_errors.ClientError) and exc.code == 429:
        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        return (
            f"Gemini API quota exceeded for model {model}. "
            "Wait a minute and retry, or set GEMINI_MODEL to another model."
        )
    if isinstance(exc, genai_errors.ClientError) and exc.code in {401, 403}:
        return "Gemini API key is invalid or not authorized. Check GEMINI_API_KEY."
    return f"Parse failed: {exc}"


def _parse_json_response(text: str) -> dict[str, str]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return _normalize_order(json.loads(text))
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return _normalize_order(json.loads(text[start : end + 1]))
            except json.JSONDecodeError:
                pass
    return _empty_order()


def parse_order(message: str, api_key: str | None = None) -> dict[str, str]:
    """Parse raw WhatsApp text into a structured order dict."""
    message = message.strip()
    if not message:
        return _empty_order()

    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("Set GEMINI_API_KEY or pass api_key.")

    client = genai.Client(api_key=key)
    prompt = USER_PROMPT_TEMPLATE.format(today=date.today().isoformat(), message=message)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0,
        response_mime_type="application/json",
    )

    failures: list[tuple[str, Exception]] = []
    for model in _models_to_try():
        try:
            response = _generate_with_retries(client, model, prompt, config)
            return _parse_json_response(response.text or "")
        except Exception as exc:
            failures.append((model, exc))
            print(f"[WARN] Model {model} unavailable: {exc}")
            if not _should_try_fallback_model(exc):
                raise
            continue

    if failures:
        raise failures[-1][1]
    raise RuntimeError("No Gemini models configured")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Parse WhatsApp order text to JSON.")
    parser.add_argument("message", nargs="?", help="Raw order message text")
    args = parser.parse_args()

    text = args.message
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()

    if not text:
        parser.error("Provide a message as an argument or via stdin.")

    try:
        result = parse_order(text)
    except Exception as exc:
        print(json.dumps({"error": format_parse_error(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
