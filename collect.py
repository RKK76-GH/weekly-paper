#!/usr/bin/env python3
"""
Story collector for Richard's weekly paper.

Runs on GitHub every two hours. Each run it:
  1. reads every source in sources.json (politely: honest User-Agent, robots.txt respected),
  2. keeps only each story's headline, short summary, link and date,
  3. drops opinion pieces, live blogs, videos, sport/entertainment (general news only)
     and anything older than a week,
  4. saves the week's stories to data/stories.json,
  5. writes digest.md: a compact, de-duplicated list of the week's candidate stories,
     grouped by section, which Claude reads to write each edition.

Uses only Python's standard library, so nothing needs installing.
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    from zoneinfo import ZoneInfo
    SYDNEY = ZoneInfo("Australia/Sydney")
except Exception:  # zone data missing: fall back to a fixed offset (label only)
    SYDNEY = dt.timezone(dt.timedelta(hours=10))

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "sources.json")
STORIES_PATH = os.path.join(ROOT, "data", "stories.json")
STATE_PATH = os.path.join(ROOT, "data", "state.json")
DIGEST_PATH = os.path.join(ROOT, "digest.md")

USER_AGENT = ("PersonalNewspaperCollector/1.0 "
              "(one person's private weekly news digest; non-commercial)")
TIMEOUT_SECONDS = 30
PAUSE_SECONDS = 2
KEEP_DAYS = 8          # how long stories stay in data/stories.json
DIGEST_DAYS = 7        # how far back the digest looks
SUMMARY_CHARS = 200

# How many story groups each digest section may list (keeps Claude's reading cost down).
DIGEST_CAPS = {
    "big": 40, "politics": 40, "other_general": 25,
    "it": 60, "tech": 50, "cars": 80, "science": 50, "green": 50, "qpr": 20,
}
# Preferred outlet to link to when several cover the same story (free sites first; SMH can be paywalled).
OUTLET_PREFERENCE = ["ABC", "Guardian", "BBC", "SMH"]

STOPWORDS = set("""
a an the and or but if of to in on at by for from with into onto over under after before about
as is are was were be been being has have had do does did not no yes it its it's this that these
those their them they there here his her he she him we our you your who whom what when where why
how which will would could should may might can just more most than then so such also amid via
says said say new first last next year years week weeks day days month time people calls call
warns warning report reports reveals revealed reveal australia australian australians government
after against back still up out off over down up top big amid being one two three four five
""".split())

ITEM_RE = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1>", re.S | re.I)
CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
TRACKING_PREFIXES = ("utm_", "at_", "ns_", "cmp", "ocid", "fbclid", "gclid")


# ---------------------------------------------------------------- helpers

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text):
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")


def clean_text(text):
    """Turn feed markup into plain readable text."""
    text = CDATA_RE.sub(r"\1", text or "")
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:–- ") + "…"


def normalise_link(url):
    """Remove tracking parameters so the same story always has the same address."""
    parts = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(TRACKING_PREFIXES)]
    return urlunparse((parts.scheme, parts.netloc.lower(), parts.path, "", urlencode(query), ""))


def story_id(link):
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]


def parse_date(text):
    text = clean_text(text)
    if not text:
        return None
    try:
        moment = parsedate_to_datetime(text)
    except Exception:
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


# ---------------------------------------------------------------- fetching

_robots_cache = {}


def robots_allowed(url):
    """True/False from the site's robots.txt; True if it has none or it can't be read."""
    parts = urlparse(url)
    key = f"{parts.scheme}://{parts.netloc}"
    if key not in _robots_cache:
        parser = None
        request = urllib.request.Request(key + "/robots.txt", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                lines = response.read().decode("utf-8", errors="replace").splitlines()
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(lines)
        except Exception:
            parser = None
        _robots_cache[key] = parser
    parser = _robots_cache[key]
    return True if parser is None else parser.can_fetch(USER_AGENT, url)


def fetch(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, "
                  "text/html;q=0.8, */*;q=0.5",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace"), ""
    except urllib.error.HTTPError as err:
        return "", f"HTTP {err.code}"
    except Exception as err:
        return "", f"{type(err).__name__}: {err}"[:120]


# ---------------------------------------------------------------- parsing

def tag_text(block, names):
    for name in names:
        found = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.S | re.I)
        if found and clean_text(found.group(1)):
            return found.group(1)
    return ""


def item_link(block):
    found = re.search(r"<link\b[^>]*>\s*(?:<!\[CDATA\[)?\s*(https?://[^<\s\]]+)", block, re.I)
    if found:
        return html.unescape(found.group(1))
    for found in re.finditer(r"<link\b([^>]*)/?>", block, re.I):
        href = re.search(r"href=[\"']([^\"']+)", found.group(1))
        rel = re.search(r"rel=[\"']([^\"']+)", found.group(1))
        if href and (not rel or rel.group(1) == "alternate"):
            return html.unescape(href.group(1))
    found = re.search(r"<guid\b[^>]*>\s*(https?://[^<\s]+)", block, re.I)
    return html.unescape(found.group(1)) if found else ""


def item_categories(block):
    names = [clean_text(m) for m in re.findall(r"<category\b[^>]*>(.*?)</category>", block, re.S | re.I)]
    names += re.findall(r"<category\b[^>]*term=[\"']([^\"']+)", block, re.I)
    return [n for n in names if n][:8]


def parse_feed(body):
    """Return a list of stories (dicts) from an RSS or Atom feed, or None if it isn't a feed."""
    if not re.search(r"<(rss|feed|rdf:RDF)\b", body[:5000], re.I):
        return None
    stories = []
    for match in ITEM_RE.finditer(body):
        block = match.group(2)
        title = clean_text(tag_text(block, ["title"]))
        link = item_link(block)
        if not title or not link:
            continue
        summary = clean_text(tag_text(block, ["description", "summary", "content:encoded", "content"]))
        if summary.lower().startswith(title.lower()):
            summary = summary[len(title):].lstrip(" .:-–")
        stories.append({
            "title": title,
            "link": normalise_link(link),
            "summary": shorten(summary, SUMMARY_CHARS),
            "published": parse_date(tag_text(block, ["pubDate", "published", "updated", "dc:date"])),
            "categories": item_categories(block),
        })
    return stories


LFW_LINK_RE = re.compile(
    r"<a\b[^>]*href=[\"']((?:https?://www\.fansnetwork\.co\.uk)?/(?:football/queensparkrangers/)?"
    r"news/(\d+)/[^\"']*)[\"'][^>]*>(.*?)</a>", re.S | re.I)
REPORT_RE = re.compile(r"^(.*?[-–—]\s*report)\b", re.I)


def parse_lfw_page(body):
    """Loft For Words news page: return match reports only (title ending '- Report'),
    ignoring the 'On this day' archive links."""
    found = {}
    for href, number, text in LFW_LINK_RE.findall(body):
        if "on-this-day" in href:
            continue
        text = clean_text(text)
        entry = found.setdefault(int(number), {"title": "", "summary": "", "href": href})
        report = REPORT_RE.match(text)
        if report:
            entry["title"] = report.group(1)
            if "/football/queensparkrangers/" in href:
                entry["href"] = href
        elif len(text) > len(entry["summary"]):
            entry["summary"] = re.sub(r"\s+\d+$", "", text)  # drop trailing comment count
    stories = []
    for number, entry in found.items():
        if not entry["title"]:
            continue
        link = entry["href"]
        if link.startswith("/"):
            link = "https://www.fansnetwork.co.uk" + link
        stories.append({
            "title": entry["title"], "link": normalise_link(link), "number": number,
            "summary": shorten(entry["summary"], SUMMARY_CHARS), "published": None, "categories": [],
        })
    return stories


# ---------------------------------------------------------------- filtering

def compile_rules(config):
    lower = lambda items: [s.lower() for s in items]
    return {
        "links": lower(config.get("exclude_link_contains", [])),
        "titles": [re.compile(p, re.I) for p in config.get("exclude_title_matches", [])],
        "general_links": lower(config.get("general_exclude_link_contains", [])),
        "general_categories": set(lower(config.get("general_exclude_categories", []))),
        "politics": lower(config.get("politics_keywords", [])),
        "highlight": config.get("highlight_names", []),
    }


def excluded(story, pool, rules):
    link = story["link"].lower()
    if any(part in link for part in rules["links"]):
        return True
    if any(pattern.search(story["title"]) for pattern in rules["titles"]):
        return True
    if pool == "general":
        if any(part in link for part in rules["general_links"]):
            return True
        if any(c.lower() in rules["general_categories"] for c in story["categories"]):
            return True
    return False


# ---------------------------------------------------------------- collecting

def collect(config, stories, state, rules, run_time):
    oldest_allowed = run_time - dt.timedelta(days=KEEP_DAYS)
    for index, source in enumerate(config["sources"]):
        if index:
            time.sleep(PAUSE_SECONDS)
        name = source["name"]
        health = state.setdefault("sources", {}).setdefault(name, {})
        found, problem, used_url = None, "", ""
        # Try the address that worked last time first, then the rest in order.
        urls = list(source["urls"])
        if health.get("url_used") in urls:
            urls.remove(health["url_used"])
            urls.insert(0, health["url_used"])
        for url in urls:
            if not robots_allowed(url):
                problem = "robots.txt asks us not to fetch this"
                continue
            body, problem = fetch(url)
            if not body:
                continue
            parsed = parse_lfw_page(body) if source.get("type") == "lfw-page" else parse_feed(body)
            if parsed is None:
                problem = "address did not return a feed"
                continue
            found, used_url, problem = parsed, url, ""
            break

        if found is None:
            health.update(last_error=problem or "no address worked", last_error_at=iso(run_time))
            print(f"FAIL  {name}: {health['last_error']}")
            continue

        # Loft For Words: the first ever run only records where we are up to,
        # so last season's reports don't appear as new.
        if source.get("type") == "lfw-page":
            highest = max((s["number"] for s in found), default=0)
            if "max_report" not in health:
                health["max_report"] = highest
                found = []
            else:
                previous = health["max_report"]
                found = [s for s in found if s["number"] > previous]
                health["max_report"] = max(previous, highest)

        new_count = 0
        for item in found:
            if excluded(item, source["pool"], rules):
                continue
            published = item["published"]
            if published and published > run_time + dt.timedelta(days=1):
                published = None  # nonsense future date
            if published and published < oldest_allowed:
                continue
            key = story_id(item["link"])
            existing = stories.get(key)
            if existing:
                existing["last_seen"] = iso(run_time)
                existing["times_seen"] = existing.get("times_seen", 1) + 1
                continue
            stories[key] = {
                "id": key, "source": name, "outlet": source["outlet"], "pool": source["pool"],
                "title": item["title"], "link": item["link"], "summary": item["summary"],
                "categories": item["categories"],
                "published": iso(published) if published else None,
                "first_seen": iso(run_time), "last_seen": iso(run_time), "times_seen": 1,
            }
            new_count += 1
        health.update(last_success=iso(run_time), url_used=used_url,
                      last_count=len(found), last_new=new_count)
        health.pop("last_error", None)
        health.pop("last_error_at", None)
        print(f"OK    {name}: {len(found)} in feed, {new_count} new")


def story_date(story):
    return from_iso(story["published"] or story["first_seen"])


def prune(stories, run_time):
    cutoff = run_time - dt.timedelta(days=KEEP_DAYS)
    for key in [k for k, s in stories.items() if story_date(s) < cutoff]:
        del stories[key]


# ---------------------------------------------------------------- grouping duplicates

def words(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))  # Sánchez -> Sanchez
    tokens = re.findall(r"[a-z0-9]+", text.lower().replace("'", "").replace("’", ""))
    return {t for t in tokens if len(t) >= 3 and t not in STOPWORDS}


def same_story(a, b):
    shared = len(a & b)
    if shared < 2:
        return False
    overlap = shared / len(a | b)
    return (shared >= 3 and overlap >= 0.25) or overlap >= 0.5


def group_stories(items):
    """Group stories that look like the same news (similar headlines within 3 days)."""
    groups = []
    for story in sorted(items, key=story_date):
        tokens = words(story["title"])
        moment = story_date(story)
        home = None
        for group in groups:
            if abs((moment - group["latest"]).total_seconds()) > 3 * 86400:
                continue
            if any(same_story(tokens, t) for t in group["tokens"]):
                home = group
                break
        if home is None:
            home = {"members": [], "tokens": [], "latest": moment}
            groups.append(home)
        home["members"].append(story)
        home["tokens"].append(tokens)
        home["latest"] = max(home["latest"], moment)
    for group in groups:
        members = group["members"]
        group["outlets"] = sorted({m["outlet"] for m in members})
        group["seen"] = sum(m.get("times_seen", 1) for m in members)
        def preference(m):
            rank = OUTLET_PREFERENCE.index(m["outlet"]) if m["outlet"] in OUTLET_PREFERENCE else 9
            return (rank, -len(m["summary"]))
        group["lead"] = sorted(members, key=preference)[0]
    return groups


# ---------------------------------------------------------------- digest

def matches_any(text, phrases):
    text = text.lower()
    return any(re.search(r"\b" + re.escape(p) + r"\b", text) for p in phrases)


def describe(group, highlight):
    lead = group["lead"]
    others = [o for o in group["outlets"] if o != lead["outlet"]]
    outlets = lead["outlet"] + (f" +{len(others)} ({', '.join(others)})" if others else "")
    flag = ""
    for name in highlight:
        if any(name.lower() in (m["title"] + " " + m["summary"]).lower() for m in group["members"]):
            flag = f" **[{name}]**"
    when = story_date(lead).astimezone(SYDNEY).strftime("%d %b")
    line = f"- {lead['title']}{flag} — {outlets} · {when} · {lead['link']}"
    if lead["summary"]:
        line += f"\n  {lead['summary']}"
    return line


def build_digest(config, stories, state, rules, run_time):
    since = run_time - dt.timedelta(days=DIGEST_DAYS)
    recent = [s for s in stories.values() if story_date(s) >= since]
    pools = {}
    for story in recent:
        pools.setdefault(story["pool"], []).append(story)

    local = run_time.astimezone(SYDNEY)
    healthy = [s["name"] for s in config["sources"]
               if "last_error" not in state["sources"].get(s["name"], {"last_error": 1})]
    failing = [s["name"] for s in config["sources"] if s["name"] not in healthy]
    lines = [
        "# Weekly paper – story digest",
        "",
        f"Updated {local:%a %d %b %Y, %H:%M} Sydney time. Stories from {since.astimezone(SYDNEY):%d %b} "
        f"to {local:%d %b}. {len(recent)} stories held from {len(healthy)} of {len(config['sources'])} sources.",
    ]
    if failing:
        lines.append(f"**Sources with problems:** {', '.join(failing)} (details at the end).")
    lines += ["", "Format: headline — lead outlet (+ other outlets covering it) · date · link, then summary.", ""]

    def section(title, groups, cap, note=""):
        lines.append(f"## {title}")
        if note:
            lines.append(note)
        if not groups:
            lines.extend(["(none this week)", ""])
            return
        for group in groups[:cap]:
            lines.append(describe(group, rules["highlight"]))
        if len(groups) > cap:
            lines.append(f"(+{len(groups) - cap} more not listed)")
        lines.append("")

    newest_first = lambda groups: sorted(groups, key=lambda g: g["latest"], reverse=True)

    general = group_stories(pools.get("general", []))
    by_weight = sorted(general, key=lambda g: (len(g["outlets"]), g["seen"], g["latest"]), reverse=True)
    big = [g for g in by_weight if len(g["outlets"]) >= 2]
    used = {id(g) for g in big[:DIGEST_CAPS["big"]]}

    politics_sources = {s["name"] for s in config["sources"] if s.get("politics")}

    def political(group):
        # Australian sources only, so overseas elections and tax stories don't crowd the list.
        return any(m["source"] in politics_sources and
                   matches_any(m["title"] + " " + m["summary"] + " " + " ".join(m["categories"]),
                               rules["politics"]) for m in group["members"])
    politics = [g for g in by_weight if id(g) not in used and political(g)]
    used |= {id(g) for g in politics[:DIGEST_CAPS["politics"]]}
    other = [g for g in by_weight if id(g) not in used]

    section("Big stories – covered by two or more outlets (Australia and world)", big, DIGEST_CAPS["big"],
            "Ranked by number of outlets, then by how long they stayed in the feeds.")
    section("Australian politics and economy – other candidates", politics, DIGEST_CAPS["politics"])
    section("Other prominent general stories – one outlet", other, DIGEST_CAPS["other_general"])
    section("IT services (iTnews, CRN, ARN)", newest_first(group_stories(pools.get("it", []))),
            DIGEST_CAPS["it"])
    section("Technology news (BBC, Guardian)", newest_first(group_stories(pools.get("tech", []))),
            DIGEST_CAPS["tech"])
    section("Cars (CarExpert)", newest_first(group_stories(pools.get("cars", []))), DIGEST_CAPS["cars"])
    section("Science (Guardian, BBC)", newest_first(group_stories(pools.get("science", []))),
            DIGEST_CAPS["science"])
    section("Green economy (ABC, Guardian, Carbon Brief)", newest_first(group_stories(pools.get("green", []))),
            DIGEST_CAPS["green"])
    section("QPR match reports (Loft For Words)", newest_first(group_stories(pools.get("qpr", []))),
            DIGEST_CAPS["qpr"])

    lines += ["## Source health", "",
              "| Source | Status | Last success (Sydney) | In feed last run | Address used |",
              "|---|---|---|---|---|"]
    for source in config["sources"]:
        health = state["sources"].get(source["name"], {})
        status = "Problem: " + health["last_error"] if "last_error" in health else "OK"
        if source.get("trial"):
            status += " (trial)"
        last = health.get("last_success")
        last = from_iso(last).astimezone(SYDNEY).strftime("%d %b %H:%M") if last else "never"
        lines.append(f"| {source['name']} | {status} | {last} | {health.get('last_count', '')} | "
                     f"{health.get('url_used', '')} |")
    text = "\n".join(lines) + "\n"
    text += f"\n_Approximate reading cost of this digest: {len(text) // 4:,} tokens._\n"
    return text


# ---------------------------------------------------------------- main

def main():
    run_time = now_utc().replace(microsecond=0)
    config = load_json(CONFIG_PATH, None)
    if not config:
        raise SystemExit("sources.json is missing or not valid JSON")
    rules = compile_rules(config)
    stories = load_json(STORIES_PATH, {}).get("stories", {})
    state = load_json(STATE_PATH, {})
    state.setdefault("sources", {})

    collect(config, stories, state, rules, run_time)
    prune(stories, run_time)
    state["last_run"] = iso(run_time)

    save_json(STORIES_PATH, {"updated": iso(run_time), "stories": stories})
    save_json(STATE_PATH, state)
    digest = build_digest(config, stories, state, rules, run_time)
    with open(DIGEST_PATH, "w", encoding="utf-8") as handle:
        handle.write(digest)
    print(f"Digest written: {len(digest) // 4:,} tokens approx, {len(stories)} stories held")


if __name__ == "__main__":
    main()
