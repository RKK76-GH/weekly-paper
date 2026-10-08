#!/usr/bin/env python3
"""
One-off feed test for Richard's personal weekly newspaper.

Fetches each source once, records what came back, and writes the results to
test-results.md (and to the GitHub Actions run summary).

Uses only Python's standard library, so nothing needs installing.
It identifies itself honestly, checks each site's robots.txt, and pauses
between requests so it is polite to every site.
"""

import datetime as dt
import html
import os
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

USER_AGENT = (
    "PersonalNewspaperCollector/0.1 "
    "(one person's private weekly news digest; non-commercial)"
)

# (name, address, kind, where the address came from)
# kind: "feed" = RSS/Atom feed, "page" = ordinary web page
# Round 2: candidates for the "Technology breakthroughs" and "Green economy" sections.
# Round 1 (the nine original sources) all passed on 7 Oct 2026 and are recorded in the project.
# kind "abc-topic" = an ABC topic page; the script finds the page's ContentId and
# builds the feed address from it (abc.net.au/news/feed/<ContentId>/rss.xml).
SOURCES = [
    # --- Technology breakthroughs ---
    ("BBC - Science & Environment", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "feed",
     "Listed on feeder.co"),
    ("BBC - Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "feed",
     "Listed on feeder.co"),
    ("Guardian - Science", "https://www.theguardian.com/science/rss", "feed",
     "ASSUMED from Guardian's address pattern"),
    ("Guardian - Technology", "https://www.theguardian.com/technology/rss", "feed",
     "ASSUMED from Guardian's address pattern"),
    ("Nature", "https://www.nature.com/nature.rss", "feed",
     "Seen in a dataset of feed files"),
    ("Quanta Magazine", "https://www.quantamagazine.org/feed/", "feed",
     "ASSUMED (standard WordPress feed address)"),
    ("ABC - Science", "https://www.abc.net.au/news/science", "abc-topic",
     "Feed address discovered from page"),
    # --- Green economy ---
    ("Guardian - Climate crisis", "https://www.theguardian.com/environment/climate-crisis/rss", "feed",
     "ASSUMED from Guardian's address pattern"),
    ("Guardian - Renewable energy", "https://www.theguardian.com/environment/renewableenergy/rss", "feed",
     "ASSUMED from Guardian's address pattern"),
    ("Guardian - Australia environment", "https://www.theguardian.com/au/environment/rss", "feed",
     "ASSUMED from Guardian's address pattern"),
    ("Carbon Brief", "https://www.carbonbrief.org/feed", "feed",
     "ASSUMED (standard WordPress feed address)"),
    ("RenewEconomy", "https://reneweconomy.com.au/feed", "feed",
     "ASSUMED (standard WordPress feed address); advocacy-leaning, tested for comparison"),
    ("ABC - Environment", "https://www.abc.net.au/news/environment", "abc-topic",
     "Feed address discovered from page"),
    ("ABC - Climate change topic", "https://www.abc.net.au/news/topic/climate-change", "abc-topic",
     "Feed address discovered from page"),
]

HEADLINES_TO_SHOW = 8

PAUSE_SECONDS = 2
TIMEOUT_SECONDS = 30

ITEM_RE = re.compile(r"<(item|entry)[\s>].*?</\1>", re.S | re.I)
DATE_RE = re.compile(r"<(pubDate|published|updated|dc:date)>\s*([^<]+?)\s*</\1>", re.I)
FULLTEXT_RE = re.compile(r"<content(:encoded)?[\s>]", re.I)
TITLE_RE = re.compile(r"<title[^>]*>\s*([^<]+?)\s*</title>", re.I)


def robots_allowed(url):
    """Return 'yes', 'no' or 'unknown' for whether robots.txt allows this address.

    Fetches robots.txt with our own User-Agent (some sites refuse Python's default
    one, which would otherwise be misread as 'disallow everything').
    """
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            lines = response.read().decode("utf-8", errors="replace").splitlines()
    except urllib.error.HTTPError as err:
        # No robots.txt (404/410) means no restrictions; anything else is unclear.
        return "yes" if err.code in (404, 410) else "unknown"
    except Exception:
        return "unknown"
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(lines)
    return "yes" if parser.can_fetch(USER_AGENT, url) else "no"


def fetch(url):
    """Fetch an address once and describe what came back."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                  "text/xml, text/html;q=0.8, */*;q=0.5",
    })
    started = time.monotonic()
    result = {"status": None, "bytes": 0, "content_type": "", "body": "", "error": ""}
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
            result.update(
                status=response.status,
                bytes=len(raw),
                content_type=response.headers.get("Content-Type", ""),
                body=raw.decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as err:
        result.update(status=err.code, error=f"HTTP {err.code} {err.reason}")
    except Exception as err:  # timeouts, DNS failures, refused connections
        result["error"] = f"{type(err).__name__}: {err}"
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def parse_date(text):
    """Parse an RSS or Atom date; return a timezone-aware datetime or None."""
    text = text.strip()
    parsed = None
    try:
        parsed = parsedate_to_datetime(text)
    except Exception:
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def analyse_feed(body):
    """Count stories, find the date range they cover, and check for full article text."""
    looks_like_feed = bool(re.search(r"<(rss|feed|rdf:RDF)[\s>]", body[:3000], re.I))
    items = [match.group(0) for match in ITEM_RE.finditer(body)]
    dates = []
    for item in items:
        found = DATE_RE.search(item)
        if found:
            parsed = parse_date(found.group(2))
            if parsed:
                dates.append(parsed)
    covers = "no dates found"
    if dates:
        oldest, newest = min(dates), max(dates)
        days = (newest - oldest).total_seconds() / 86400
        covers = f"{oldest:%d %b} to {newest:%d %b} ({days:.1f} days)"
    return {
        "looks_like_feed": looks_like_feed,
        "items": len(items),
        "covers": covers,
        "full_text": "yes" if FULLTEXT_RE.search(body) else "no",
    }


CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
ABC_META_RE = re.compile(
    r"<meta[^>]+name=[\"']ContentId[\"'][^>]+content=[\"'](\d+)"
    r"|<meta[^>]+content=[\"'](\d+)[\"'][^>]+name=[\"']ContentId[\"']", re.I)
ABC_ANY_RE = re.compile(r"ContentId[\"']?\s*[:=]\s*[\"']?(\d{3,12})", re.I)


def headlines(body, limit):
    """Return the first few story titles from a feed, cleaned up for reading."""
    titles = []
    for match in ITEM_RE.finditer(body):
        found = re.search(r"<title[^>]*>(.*?)</title>", match.group(0), re.S | re.I)
        if not found:
            continue
        text = CDATA_RE.sub(r"\1", found.group(1))
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        text = " ".join(text.split())
        if text:
            titles.append(text.replace("|", "/")[:140])
        if len(titles) >= limit:
            break
    return titles


def discover_abc_feed(page_url):
    """Find an ABC topic page's ContentId and return the matching feed address, or None."""
    got = fetch(page_url)
    if got["status"] != 200 or not got["body"]:
        return None, got["error"] or f"HTTP {got['status']}"
    found = ABC_META_RE.search(got["body"])
    content_id = (found.group(1) or found.group(2)) if found else None
    if not content_id:
        found = ABC_ANY_RE.search(got["body"])
        content_id = found.group(1) if found else None
    if not content_id:
        return None, "Page loaded but no ContentId found"
    return f"https://www.abc.net.au/news/feed/{content_id}/rss.xml", ""


def analyse_page(body):
    """For ordinary web pages, just report the page title."""
    found = TITLE_RE.search(body)
    return {"title": found.group(1)[:80] if found else "(no title found)"}


def main():
    run_time = dt.datetime.now(dt.timezone.utc)
    rows = []
    for index, (name, url, kind, origin) in enumerate(SOURCES):
        if index:
            time.sleep(PAUSE_SECONDS)
        discovered_note = ""
        if kind == "abc-topic":
            feed_url, problem = discover_abc_feed(url)
            if not feed_url:
                print(f"{name}: could not find feed ({problem})")
                rows.append({
                    "name": name, "url": url, "origin": origin, "robots": "",
                    "result": f"No feed found: {problem}", "kb": 0, "tokens": 0, "items": "",
                    "covers": "", "full_text": "", "notes": "", "headlines": [],
                })
                continue
            discovered_note = f"Feed found at {feed_url}"
            url, kind = feed_url, "feed"
            time.sleep(PAUSE_SECONDS)
        robots = robots_allowed(url)
        if robots == "no":
            # Respect the site's wishes: record it and move on without fetching.
            print(f"{name}: skipped, robots.txt disallows")
            rows.append({
                "name": name, "url": url, "origin": origin, "robots": robots,
                "result": "Skipped (robots.txt)", "kb": 0, "tokens": 0, "items": "",
                "covers": "", "full_text": "", "notes": "Site asks automated tools not to fetch this",
                "headlines": [],
            })
            continue
        got = fetch(url)
        kb = round(got["bytes"] / 1024, 1)
        rough_tokens = got["bytes"] // 4  # rough rule of thumb: about 4 characters per token
        row = {
            "name": name, "url": url, "origin": origin, "robots": robots,
            "result": "OK" if got["status"] == 200 and got["bytes"] else (got["error"] or f"HTTP {got['status']}"),
            "kb": kb, "tokens": rough_tokens, "items": "", "covers": "", "full_text": "",
            "notes": discovered_note, "headlines": [],
        }
        if got["status"] == 200 and got["body"]:
            if kind == "feed":
                info = analyse_feed(got["body"])
                row.update(items=info["items"], covers=info["covers"], full_text=info["full_text"],
                           headlines=headlines(got["body"], HEADLINES_TO_SHOW))
                if not info["looks_like_feed"]:
                    row["notes"] = f"Not a feed (content type: {got['content_type'][:40]})"
            else:
                row["notes"] = "Web page: " + analyse_page(got["body"])["title"]
        print(f"{name}: {row['result']} | {kb} KB | items {row['items']} | {row['covers']}")
        rows.append(row)

    lines = [
        "# Feed test results",
        "",
        f"Run at {run_time:%Y-%m-%d %H:%M} UTC "
        + ("from GitHub's servers." if os.environ.get("GITHUB_ACTIONS") else "(not on GitHub)."),
        "",
        "Token figures are a rough estimate (bytes divided by 4), before any trimming.",
        "",
        "| Source | Result | Robots allow? | Size (KB) | ~Tokens | Stories | Date range | Full text? | Notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['result']} | {row['robots']} | {row['kb']} | {row['tokens']:,} | "
            f"{row['items']} | {row['covers']} | {row['full_text']} | {row['notes']} |"
        )
    lines += ["", "## Sample headlines (newest first)", ""]
    for row in rows:
        if row["headlines"]:
            lines.append(f"### {row['name']}")
            lines += [f"- {title}" for title in row["headlines"]]
            lines.append("")
    lines += ["", "## Addresses tested", ""]
    for row in rows:
        lines.append(f"- **{row['name']}**: {row['url']} ({row['origin']})")
    report = "\n".join(lines) + "\n"

    with open("test-results.md", "w", encoding="utf-8") as handle:
        handle.write(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(report)


if __name__ == "__main__":
    main()
