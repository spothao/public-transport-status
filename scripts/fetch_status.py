#!/usr/bin/env python3
"""Fetch KTM/LRT disruption news and write status.json for the GitHub Pages site."""
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# Line queries: (English-edition query, Malay-edition query).
# Avoid bare "KTM" — collides with the MotoGP team. Malay edition (hl=ms) has
# far better coverage of local rail disruptions.
LINES = {
    "ktm-komuter": (
        'Komuter (delay OR breakdown OR stranded OR disrupted)',
        'Komuter (lewat OR tergendala OR gangguan OR kelewatan OR tumbang)',
    ),
    "ktm-ets": (
        '"KTM ETS" (delay OR breakdown OR disrupted)',
        'ETS Keretapi (lewat OR tergendala OR gangguan OR kelewatan)',
    ),
    "lrt-kelana-jaya": (
        '"LRT Kelana Jaya" OR "Kelana Jaya Line" (delay OR breakdown OR stranded OR disrupted)',
        '"LRT Laluan Kelana Jaya" OR "LRT Kelana Jaya" (lewat OR tergendala OR ganggu OR kerosakan)',
    ),
    "lrt-ampang": (
        '"LRT Ampang" OR "Ampang Line" OR "Sri Petaling Line" (delay OR breakdown OR disrupted)',
        '"LRT Laluan Ampang" OR "LRT Sri Petaling" (lewat OR tergendala OR ganggu OR kerosakan)',
    ),
    "mrt-kajang": (
        '"MRT Kajang" OR "Kajang Line" (delay OR breakdown OR disrupted)',
        '"MRT Laluan Kajang" (lewat OR tergendala OR ganggu OR kerosakan)',
    ),
    "mrt-putrajaya": (
        '"MRT Putrajaya" OR "Putrajaya Line" (delay OR breakdown OR disrupted)',
        '"MRT Laluan Putrajaya" (lewat OR tergendala OR ganggu OR kerosakan)',
    ),
    "monorail": (
        '"KL Monorail" (delay OR breakdown OR disrupted)',
        'Monorail KL (lewat OR tergendala OR ganggu)',
    ),
}

BREAKDOWN_WORDS = ["breakdown", "broke down", "stranded", "stuck", "rescue", "derail", "tergendala", "dirosakkan"]
DELAY_WORDS = [
    "delay", "delayed", "delays", "late", "slow", "lewat", "kelewatan", "tertunda",
    "ganggu", "gangguan", "disruption", "disrupted", "interruption", "tumbang", "kerosakan",
]
WINDOW_HOURS = 24


def fetch_rss(query: str, lang: str) -> list[dict]:
    ceid = "MY:ms" if lang == "ms" else "MY:en"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": lang, "gl": "MY", "ceid": ceid}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read())
    items = []
    for item in root.iter("item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "pubDate": (item.findtext("pubDate") or "").strip(),
        })
    return items


def classify(title: str) -> str | None:
    t = title.lower()
    for w in BREAKDOWN_WORDS:
        if w in t:
            return "breakdown"
    for w in DELAY_WORDS:
        if w in t:
            return "delayed"
    return None


def main() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    lines_out: dict[str, dict] = {}

    for line_id, (q_en, q_ms) in LINES.items():
        status = "normal"
        best: dict | None = None  # most severe + most recent match
        for q, lang in [(q_en, "en"), (q_ms, "ms")]:
            for item in fetch_rss(q, lang):
                published = None
                if item["pubDate"]:
                    try:
                        published = parsedate_to_datetime(item["pubDate"])
                    except Exception:
                        pass
                if published is None or published < cutoff:
                    continue
                s = classify(item["title"])
                if s is None:
                    continue
                # Google News titles: "Headline - Publisher" — strip publisher suffix
                title = re.sub(r"\s+-\s+[^-]+$", "", item["title"])
                cand = {"status": s, "headline": title, "url": item["link"], "published": published.isoformat()}
                if best is None:
                    best = cand
                elif s == "breakdown" and best["status"] != "breakdown":
                    best = cand
                elif s == best["status"] and published.isoformat() > best["published"]:
                    best = cand
        if best:
            status = best["status"]
        else:
            best = {"status": "normal", "headline": "No disruption reported in the last 24 hours", "url": "", "published": None}
        lines_out[line_id] = {"status": status, **best}

    out = {"generated_at": now.isoformat(), "lines": lines_out}
    Path("status.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v["status"] for k, v in lines_out.items()}, indent=2))


if __name__ == "__main__":
    main()
