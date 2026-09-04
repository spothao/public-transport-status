#!/usr/bin/env python3
"""Fetch KTM/LRT disruption news and write status.json for the GitHub Pages site."""
import json
import re
import sys
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

# myrapid.com.my is Incapsula-walled; Jina Reader renders it and exposes the
# official "Get the latest train services status" table + Prasarana news posts.
MYRAPID_URL = "https://r.jina.ai/https://myrapid.com.my/"

# Official table row label -> our line ids (Ampang & Sri Petaling share a card).
RAPIDKL_TABLE_LINES = {
    "Ampang Line": "lrt-ampang",
    "Sri Petaling Line": "lrt-ampang",
    "Kelana Jaya Line": "lrt-kelana-jaya",
    "KL Monorail Line": "monorail",
    "Kajang Line": "mrt-kajang",
    "Putrajaya Line": "mrt-putrajaya",
}

RAPIDKL_NEWS_LINES = {
    "kelana jaya": "lrt-kelana-jaya",
    "ampang": "lrt-ampang",
    "sri petaling": "lrt-ampang",
    "monorail": "monorail",
    "kajang": "mrt-kajang",
    "putrajaya": "mrt-putrajaya",
}

RESUMED_WORDS = ["kembali beroperasi", "dipulihkan", "back to normal", "resumed", "seperti biasa"]


def fetch_url(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def classify_status(text: str) -> str:
    t = text.lower()
    for w in BREAKDOWN_WORDS:
        if w in t:
            return "breakdown"
    for w in DELAY_WORDS:
        if w in t:
            return "delayed"
    return "normal"


def is_resolved(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in RESUMED_WORDS)


def fetch_official_rapidkl() -> dict[str, dict]:
    """Parse the official Rapid KL status table + latest Prasarana news posts."""
    markdown = fetch_url(MYRAPID_URL)
    result: dict[str, dict] = {}

    # Status table rows: | Ampang Line | - | Normal Service | remark | ... |
    table_section = re.search(r"## Get the latest train services status(.*?)(?:## |\Z)", markdown, re.S)
    if table_section:
        for row in table_section.group(1).splitlines():
            cols = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cols) < 4 or cols[0] not in RAPIDKL_TABLE_LINES:
                continue
            line_id = RAPIDKL_TABLE_LINES[cols[0]]
            remark = cols[3] if len(cols) > 3 else ""
            status = "normal" if "normal" in cols[2].lower() else classify_status(cols[2] + " " + remark)
            entry = result.setdefault(line_id, {"status": "normal", "remark": ""})
            if status != "normal":
                entry["status"] = status
                entry["remark"] = remark or cols[2]
            elif not entry["remark"]:
                entry["remark"] = remark

    # News posts: "## [TITLE](url)" followed by a date line, under News on Prasarana.
    # Section ends at the next non-link "## " heading (post titles are themselves "## [..").
    # Posts are newest-first: only the newest post per line is authoritative, so a
    # resolution post ("kembali beroperasi") correctly clears an older disruption post.
    news_section = re.search(r"## News on Prasarana(.*?)(?=\n## (?!\[)|\Z)", markdown, re.S)
    if news_section:
        seen: set[str] = set()
        for m in re.finditer(r"## \[([^\]]+)\]\((https://myrapid\.com\.my/[^)]+)\)", news_section.group(1)):
            title, url = m.group(1), m.group(2)
            for needle, line_id in RAPIDKL_NEWS_LINES.items():
                if needle not in title.lower() or line_id in seen:
                    continue
                seen.add(line_id)
                entry = result.setdefault(line_id, {"status": "normal", "remark": ""})
                entry["headline"] = title
                entry["url"] = url
                if not is_resolved(title):
                    entry["status"] = classify_status(title)
                break
    return result


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

    try:
        official = fetch_official_rapidkl()
    except Exception as e:
        print(f"official source unavailable, falling back to news only: {e}", file=sys.stderr)
        official = {}

    for line_id, (q_en, q_ms) in LINES.items():
        news_best: dict | None = None  # most severe + most recent news match
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
                if news_best is None:
                    news_best = cand
                elif s == "breakdown" and news_best["status"] != "breakdown":
                    news_best = cand
                elif s == news_best["status"] and published.isoformat() > news_best["published"]:
                    news_best = cand

        off = official.get(line_id)
        if off and off["status"] != "normal":
            # official alert wins over news
            entry = {"status": off["status"], "source": "official",
                     "headline": off.get("headline") or off.get("remark") or "Official service alert",
                     "url": off.get("url", ""), "published": None}
        elif off:
            # official says normal; news-reported incident kept as context but status stays normal
            headline = off.get("headline")
            if not headline and news_best:
                headline = "Recently reported in news (official status: Normal): " + news_best["headline"]
            entry = {"status": "normal", "source": "official",
                     "headline": headline or "Normal service (per Rapid KL official)",
                     "url": off.get("url") or (news_best or {}).get("url", ""), "published": None}
        elif news_best:
            entry = {"status": news_best["status"], "source": "news", **news_best}
        else:
            entry = {"status": "normal", "source": "news",
                     "headline": "No disruption reported in the last 24 hours", "url": "", "published": None}
        lines_out[line_id] = entry

    out = {"generated_at": now.isoformat(), "lines": lines_out}
    Path("status.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v["status"] for k, v in lines_out.items()}, indent=2))


if __name__ == "__main__":
    main()
