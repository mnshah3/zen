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
import time

import requests

log = logging.getLogger(__name__)

# Google retires model names without warning -- a pinned list went stale and
# every model in it returned 404 with "no longer available to new users". The
# available models are therefore discovered at runtime and only used as a
# fallback ordering if that discovery fails.
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS",
    "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-flash-latest"
).split(",") if m.strip()]

# How hard to try before giving up. Free-tier capacity fluctuates minute to
# minute, so a second pass after a short wait recovers most 503s.
MAX_MODELS = 5
PASSES = 3
BACKOFF_SECONDS = 20

BASE = "https://generativelanguage.googleapis.com/v1beta"
ENDPOINT = BASE + "/models/{model}:generateContent"


def discover_models(api_key: str) -> list[str]:
    """Ask Google which models this key can actually call.

    Cheaper than guessing and immune to renames. Flash variants are preferred:
    they are the ones covered by the free tier and are fast enough that one
    request can carry the whole brief.
    """
    try:
        r = requests.get(f"{BASE}/models", headers={"x-goog-api-key": api_key},
                         timeout=30)
        r.raise_for_status()
        models = r.json().get("models", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("gemini: model discovery failed (%s); using fallback list", e)
        return FALLBACK_MODELS

    usable = [
        m["name"].removeprefix("models/") for m in models
        if "generateContent" in (m.get("supportedGenerationMethods") or [])
    ]
    if not usable:
        return FALLBACK_MODELS

    def rank(name: str) -> tuple:
        low = name.lower()
        return (
            0 if "flash" in low else 1,
            # Preview and experimental builds are the most contended on the
            # free tier and returned 503 across the board; stable first.
            1 if any(t in low for t in ("preview", "exp", "thinking")) else 0,
            0 if "lite" not in low else 1,
            [-int(p) for p in re.findall(r"\d+", low)] or [0],
        )

    ordered = sorted(usable, key=rank)
    log.info("gemini: %d models available, preferring %s",
             len(usable), ", ".join(ordered[:3]))
    return ordered[:MAX_MODELS]

PROMPT = """You are writing the morning market brief for one reader in India. \
He follows markets closely but is not a specialist in every sector, and he \
wants to finish your paragraph understanding the story well enough not to open \
the link.

For each numbered story write a SHORT PARAGRAPH of two or three sentences, \
45 to 70 words, in the simplest English that is still precise.

Structure each paragraph:
1. What actually happened, leading with the concrete number or fact.
2. Why it matters -- the mechanism, in plain words. What does this change, and \
for whom?
3. Only if genuinely useful: what would confirm or kill this in the coming days.

Rules, strictly:
- Write as if explaining to a smart friend over coffee. Short words, active voice.
- Never use jargon without unpacking it in the same breath. Not "re-rated" but \
"investors decided it deserves a higher price for the same profits".
- For a global story, say concretely how it reaches India -- through oil prices, \
foreign fund flows, the rupee, export demand. If it genuinely does not reach \
India, say so in one line and stop.
- Never predict prices. Never advise buying or selling. Never say "investors \
should".
- Do not restate the headline. The reader has already read it. Add what it left out.
- If a story is trivial, say so plainly in one short sentence instead of \
inflating it to fill the space.
- If you do not know something, leave it out rather than guessing.

Return ONLY a JSON array, one object per story, no markdown fence:
[{"i": 1, "s": "your paragraph"}, ...]

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
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
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


def _transient(why: str) -> bool:
    """503 means 'high demand, try again later'; 429 means rate limited.

    Both are worth waiting out. A 404 or a bad key is not -- retrying those
    just burns workflow minutes.
    """
    return why.startswith("HTTP 503") or why.startswith("HTTP 429") \
        or why.startswith("request failed")


def _call(prompt: str, api_key: str) -> str | None:
    """Rotate across models, then wait and rotate again.

    Free-tier capacity is per model and fluctuates minute to minute, so
    trying the next model immediately beats hammering one that is busy.
    Coming back for a second pass after a pause beats giving up: the whole
    fleet being briefly saturated is common and self-resolving.
    """
    models = discover_models(api_key)

    for attempt in range(1, PASSES + 1):
        transient_seen = False
        for model in models:
            text, why = _call_model(model, prompt, api_key)
            if text:
                log.info("gemini: %s responded (pass %d)", model, attempt)
                return text
            log.warning("gemini: %s unusable -- %s", model, why)
            transient_seen |= _transient(why)

        if not transient_seen:
            break                      # nothing retryable; stop wasting time
        if attempt < PASSES:
            wait = BACKOFF_SECONDS * attempt
            log.info("gemini: all models busy, waiting %ds before pass %d",
                     wait, attempt + 1)
            time.sleep(wait)

    log.error("gemini: exhausted %d models over %d passes; using extracted facts",
              len(models), PASSES)
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
