"""Plain-English explanations via Gemini's free tier.

Design rules:

  Optional. No key, a rate limit, a bad response -- the brief still goes out,
  just with extracted facts instead of prose. Delivery never depends on a
  third party being up.

  One request for the whole brief, not one per story. The free tier is rated
  per minute, and twenty separate calls would trip it.

  The model explains; it does not choose. Ranking and selection stay in
  rules where they can be inspected. What comes back is treated as text to
  display, never as instructions to follow.
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

log = logging.getLogger(__name__)

# Free-tier model availability changes without notice, so several are tried in
# order rather than pinning one and silently producing no explanations.
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS",
    "gemini-2.5-flash,gemini-2.0-flash,gemini-flash-latest,gemini-2.5-flash-lite"
).split(",") if m.strip()]

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")

PROMPT = """You are writing a market brief for one reader: a finance graduate \
in India who follows markets closely but is not a specialist in every sector. \
He wants facts, not filler.

For each numbered story below write ONE sentence, maximum 28 words, that says \
what actually happened and why it matters to an Indian equity investor. \
Follow these rules strictly:

- Lead with the concrete fact or number. No throat-clearing.
- Plain English. If you must use a technical term, define it in three words.
- If the story is a global one, say specifically how it reaches India.
- Never speculate about price direction. Never give advice.
- If a story is trivial, say so in three words rather than inflating it.

Return ONLY a JSON array, one object per story, no markdown fence:
[{"i": 1, "s": "your sentence"}, ...]

Stories:
%s
"""


def _call_model(model: str, prompt: str, api_key: str,
                timeout: int = 90) -> tuple[str | None, str]:
    """Returns (text, diagnosis). The diagnosis is logged so a silent failure
    in a scheduled run can be identified from the Actions log alone."""
    try:
        r = requests.post(
            ENDPOINT.format(model=model),
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
            },
            timeout=timeout,
        )
    except requests.RequestException as e:
        return None, f"request failed ({type(e).__name__}: {e})"

    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"

    try:
        payload = r.json()
    except ValueError:
        return None, "response was not JSON"

    candidates = payload.get("candidates") or []
    if not candidates:
        # A prompt blocked by safety filters returns 200 with no candidates.
        return None, f"no candidates (feedback: {payload.get('promptFeedback')})"

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        return None, f"empty text (finishReason: {candidates[0].get('finishReason')})"
    return text, "ok"


def _call(prompt: str, api_key: str) -> str | None:
    for model in MODELS:
        text, why = _call_model(model, prompt, api_key)
        if text:
            log.info("gemini: %s responded", model)
            return text
        log.warning("gemini: %s unusable -- %s", model, why)
    log.error("gemini: all %d models failed; falling back to extracted facts",
              len(MODELS))
    return None


def _parse(raw: str, expected: int) -> dict[int, str]:
    """Pull the JSON array out, tolerating a stray markdown fence."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            log.warning("no JSON array in gemini output")
            return {}
        try:
            items = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}

    out = {}
    for item in items if isinstance(items, list) else []:
        try:
            i, s = int(item["i"]), str(item["s"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= i <= expected and s:
            out[i] = s
    return out


def explain(articles: list, api_key: str | None = None) -> dict[int, str]:
    """Return {1-based index: one-sentence explanation}.

    An empty dict is a perfectly acceptable outcome -- the caller falls back
    to extracted facts.
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.info("GEMINI_API_KEY not set; brief will use extracted facts only")
        return {}
    if not articles:
        return {}

    lines = []
    for i, a in enumerate(articles, start=1):
        summary = (a.summary or "")[:280]
        lines.append(f"{i}. [{a.source}] {a.title}\n   {summary}")

    raw = _call(PROMPT % "\n".join(lines), api_key)
    if raw is None:
        return {}

    result = _parse(raw, len(articles))
    log.info("gemini explained %d of %d stories", len(result), len(articles))
    return result
