"""Collect, deduplicate and rank market news for the daily brief.

The ranking is deliberately rule-based rather than model-driven: it is
inspectable, free, and gives the same answer twice. A model is only worth
adding for summarising, not for deciding what matters.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import requests

from zen.monitor.feeds import FEEDS, NOISE, SECTIONS

log = logging.getLogger(__name__)

STATE = Path("state/seen.json")
MEMORY_DAYS = 7

# Several publishers reject feedparser's default user-agent outright and return
# an empty document with a 200, so feeds are fetched via requests first.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _parse_feed(url: str):
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,*/*"},
                     timeout=25)
    r.raise_for_status()
    return feedparser.parse(r.content)


@dataclass
class Article:
    title: str
    link: str
    source: str
    trust: float
    published: datetime
    summary: str = ""
    corroboration: int = 1
    section: str = "Markets"
    score: float = 0.0
    also: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        norm = re.sub(r"[^a-z0-9 ]", "", self.title.lower())
        return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def collect(lookback_hours: int = 24) -> list[Article]:
    cutoff = _now() - timedelta(hours=lookback_hours)
    out: list[Article] = []
    for source, (url, trust) in FEEDS.items():
        try:
            parsed = _parse_feed(url)
        except Exception as e:
            log.warning("%s: feed failed (%s)", source, e)
            continue

        kept = 0
        for e in parsed.entries:
            tm = e.get("published_parsed") or e.get("updated_parsed")
            if not tm:
                continue
            pub = datetime(*tm[:6], tzinfo=timezone.utc)
            if pub < cutoff:
                continue
            title = re.sub(r"\s+", " ", e.get("title", "")).strip()
            if not title or any(n in title.lower() for n in NOISE):
                continue
            out.append(Article(
                title=title,
                link=e.get("link", ""),
                source=source,
                trust=trust,
                published=pub,
                summary=re.sub(r"<[^>]+>", "", e.get("summary", ""))[:400].strip(),
            ))
            kept += 1
        log.info("%-22s %3d entries, %2d fresh", source, len(parsed.entries), kept)
    return out


def deduplicate(articles: list[Article], threshold: float = 0.72) -> list[Article]:
    """Collapse near-identical headlines, keeping a corroboration count.

    Several outlets covering one event is itself evidence the event matters,
    so the count is kept rather than thrown away.
    """
    groups: list[Article] = []
    for a in sorted(articles, key=lambda x: (-x.trust, x.published)):
        for g in groups:
            if SequenceMatcher(None, a.title.lower(), g.title.lower()).ratio() >= threshold:
                g.corroboration += 1
                if a.source != g.source and a.source not in g.also:
                    g.also.append(a.source)
                break
        else:
            groups.append(a)
    return groups


def classify(a: Article) -> str:
    text = f"{a.title} {a.summary}".lower()
    best, best_hits = "Markets", 0
    for name, cfg in SECTIONS.items():
        hits = sum(1 for k in cfg["keywords"] if k in text)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def score(a: Article) -> float:
    age_h = (_now() - a.published).total_seconds() / 3600
    recency = max(0.0, 1.0 - age_h / 36)
    corrob = min(a.corroboration, 4) / 4
    return round(0.40 * a.trust + 0.35 * corrob + 0.25 * recency, 4)


def _load_seen() -> dict[str, str]:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text())
    except json.JSONDecodeError:
        log.warning("seen.json unreadable; starting fresh")
        return {}


def _save_seen(seen: dict[str, str]) -> None:
    cutoff = _now() - timedelta(days=MEMORY_DAYS)
    fresh = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > cutoff}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(fresh, indent=0, sort_keys=True))


def build(lookback_hours: int = 24, remember: bool = True) -> dict[str, list[Article]]:
    """Ranked articles per section, excluding anything sent in the last week."""
    seen = _load_seen()
    articles = deduplicate(collect(lookback_hours))

    fresh = []
    for a in articles:
        if a.key in seen:
            continue
        a.section = classify(a)
        a.score = score(a)
        fresh.append(a)

    sections: dict[str, list[Article]] = {}
    for name, cfg in SECTIONS.items():
        picked = sorted([a for a in fresh if a.section == name], key=lambda x: -x.score)
        if picked:
            sections[name] = picked[: cfg["limit"]]

    if remember:
        stamp = _now().isoformat()
        for group in sections.values():
            for a in group:
                seen[a.key] = stamp
        _save_seen(seen)
    return sections
