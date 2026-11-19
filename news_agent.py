#!/usr/bin/env python3
"""Popular-news digest for Telegram, covering Russia and international headlines.

Uses Google News RSS editorial prominence as a popularity *proxy*, not view counts.
Selects all stories above a configurable significance threshold (no numeric cap).
Requires only Python standard library. No paid AI API or API keys.
"""
from __future__ import annotations

import concurrent.futures
import difflib
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def rss(*, country: str, lang: str, category: str | None = None) -> str:
    # Separate regional editions for local and global coverage.
    language_tag = f"{lang}-{country}" if lang == "en" else lang
    base = "https://news.google.com/rss"
    if category:
        base += f"/headlines/section/topic/{category}"
    return f"{base}?hl={language_tag}&gl={country}&ceid={country}:{lang}"


FEEDS = {
    # Broad Russian-language selection, including major thematic sections.
    "Россия · Главное": rss(country="RU", lang="ru"),
    "Россия · Мир": rss(country="RU", lang="ru", category="WORLD"),
    "Россия · Страна": rss(country="RU", lang="ru", category="NATION"),
    "Россия · Экономика": rss(country="RU", lang="ru", category="BUSINESS"),
    "Россия · Технологии": rss(country="RU", lang="ru", category="TECHNOLOGY"),
    "Россия · Наука": rss(country="RU", lang="ru", category="SCIENCE"),
    "Россия · Здоровье": rss(country="RU", lang="ru", category="HEALTH"),
    "Россия · Спорт": rss(country="RU", lang="ru", category="SPORTS"),
    "Россия · Развлечения": rss(country="RU", lang="ru", category="ENTERTAINMENT"),
    # International editions reduce the bias of relying on one country's news feed.
    "Мир · США": rss(country="US", lang="en"),
    "Мир · США / World": rss(country="US", lang="en", category="WORLD"),
    "Мир · Британия": rss(country="GB", lang="en"),
    "Мир · Индия": rss(country="IN", lang="en"),
    "Мир · Австралия": rss(country="AU", lang="en"),
    "Мир · Канада": rss(country="CA", lang="en"),
    "Мир · Сингапур": rss(country="SG", lang="en"),
    "Мир · Южная Африка": rss(country="ZA", lang="en"),
    "Мир · Нигерия": rss(country="NG", lang="en"),
    "Мир · Филиппины": rss(country="PH", lang="en"),
    "Мир · Ирландия": rss(country="IE", lang="en"),
}
USER_AGENT = "Mozilla/5.0 (compatible; PersonalNewsDigest/2.0)"
STOPWORDS = {
    "the", "and", "from", "with", "that", "this", "after", "about", "over", "into",
    "россии", "россия", "сегодня", "стало", "после", "который", "которые", "новости",
}


def request(url: str, *, payload: dict | None = None, headers: dict | None = None) -> bytes:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=25) as res:
        return res.read()


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def clean_title(title: str) -> str:
    # Google News frequently appends the source after a dash.
    return re.sub(r"\s+-\s+[^-]{2,90}$", "", title).strip()


def parse_feed(xml: bytes, label: str, since: datetime, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    root = ET.fromstring(xml)
    output = []
    for position, item in enumerate(root.findall(".//channel/item")[:65]):
        pubtext = item.findtext("pubDate", "")
        try:
            when = email.utils.parsedate_to_datetime(pubtext)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        when = when.astimezone(timezone.utc)
        if not (since <= when <= now + timedelta(minutes=15)):
            continue
        title = clean_title(clean_text(item.findtext("title", "")))
        link = (item.findtext("link") or "").strip()
        description = clean_text(item.findtext("description", ""))[:450]
        if not title or not link.startswith("https://"):
            continue
        source_el = item.find("source")
        source = clean_text(source_el.text) if source_el is not None and source_el.text else "Google News"
        output.append({"title": title, "url": link, "source": source,
                       "description": description, "published": when,
                       "feed": label, "position": position})
    return output


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(t for t in re.findall(r"[\wа-яё]{4,}", title.lower(), re.UNICODE)
                     if t not in STOPWORDS)


def title_similarity(a: str, b: str, ta: frozenset[str] | None = None,
                     tb: frozenset[str] | None = None) -> float:
    ta, tb = ta if ta is not None else title_tokens(a), tb if tb is not None else title_tokens(b)
    # Numerical differences frequently distinguish otherwise similar stories.
    numbers_a, numbers_b = set(re.findall(r"\d+", a)), set(re.findall(r"\d+", b))
    if numbers_a and numbers_b and numbers_a != numbers_b:
        return 0.0
    overlap = len(ta & tb)
    if not overlap:
        return 0.0
    jaccard = overlap / len(ta | tb)
    # Avoid expensive string comparison on obviously different headlines.
    if jaccard < 0.28:
        return jaccard
    return max(jaccard, difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio())


def rank_stories(items: list[dict], min_score: float = 34) -> list[dict]:
    """No fixed story-count limit; choose all with sufficient feed prominence.

    The score is a heuristic. It does NOT measure actual views/reads/shares.
    """
    clusters: list[dict] = []
    by_link: dict[str, dict] = {}
    for item in sorted(items, key=lambda x: (x["position"], x["feed"])):
        tokens = title_tokens(item["title"])
        match = by_link.get(item["url"])
        if match is None:
            match = next((c for c in clusters
                          if title_similarity(c["title"], item["title"], c["tokens"], tokens) >= 0.70), None)
        if match is None:
            match = {**item, "feeds": set(), "sources": set(), "tokens": tokens,
                     "best_position": item["position"]}
            clusters.append(match)
        match["feeds"].add(item["feed"])
        match["sources"].add(item["source"])
        match["best_position"] = min(match["best_position"], item["position"])
        # Link to a representative article from the most prominent listing.
        if item["position"] < match["position"]:
            match["title"] = item["title"]
            match["url"] = item["url"]
            match["source"] = item["source"]
            match["position"] = item["position"]
            match["tokens"] = tokens
        if item["published"] > match["published"]:
            match["published"] = item["published"]
        if len(item["description"]) > len(match.get("description", "")):
            match["description"] = item["description"]
        by_link[item["url"]] = match

    for story in clusters:
        # A position near the top of any edition matters; independent editorial
        # appearances and publishers increase confidence that a story is prominent.
        story["score"] = (42 - min(story["best_position"], 42)
                          + 11 * (len(story["feeds"]) - 1)
                          + 3 * (len(story["sources"]) - 1))
    return sorted((s for s in clusters if s["score"] >= min_score),
                  key=lambda s: (s["score"], s["published"]), reverse=True)


def render_blocks(stories: list[dict], local_time: datetime) -> list[str]:
    """Telegram HTML text. Titles preserve the original language, no AI translation."""
    header = (
        f"🗞 <b>Главные новости России и мира за последние 24 часа</b>"
        f"\n📅 {local_time:%d.%m.%Y} · без лимита по числу сюжетов"
    )
    if not stories:
        return [header, "Не обнаружено достаточно заметных сюжетов по выбранному порогу."]
    blocks = [header]
    for i, story in enumerate(stories, 1):
        title = html.escape(story["title"])
        url = html.escape(story["url"], quote=True)
        source = html.escape(story["source"])
        pub = story["published"].astimezone(local_time.tzinfo)
        entry = (f'<b>{i}. <a href="{url}">{title}</a></b>'
                 f'\n<em>{source} · {pub:%d.%m %H:%M}</em>')
        blocks.append(entry)
    blocks.append(
        "<i>Всего уникальных заметных сюжетов: " + str(len(stories))
        + ". Порядок основан на положении в новостных лентах, "
        + "а не на точных просмотрах или репостах. "
        + "Заголовки — на языке оригинала.</i>"
    )
    return blocks


def pack_telegram_messages(blocks: list[str], max_len: int = 3400) -> list[str]:
    """Split only between complete HTML blocks; never break tags or links."""
    if not blocks:
        return []
    chunks = []
    current = ""
    for block in blocks:
        if len(block) > max_len:
            raise ValueError("Single formatted news block too large for Telegram")
        proposed = f"{current}\n\n{block}" if current else block
        if len(proposed) > max_len:
            chunks.append(current)
            current = block
        else:
            current = proposed
    if current:
        chunks.append(current)
    return chunks


def send_telegram(blocks: list[str]) -> None:
    token = os.environ["TG_BOT_TOKEN"].strip()
    chat_id = os.environ["TG_CHAT_ID"].strip()
    for i, chunk in enumerate(pack_telegram_messages(blocks), 1):
        response = json.loads(request(f"https://api.telegram.org/bot{token}/sendMessage",
                                      payload={"chat_id": chat_id, "text": chunk,
                                               "parse_mode": "HTML",
                                               "link_preview_options": {"is_disabled": True}}))
        if not response.get("ok"):
            raise RuntimeError(f"Telegram did not confirm message #{i}")
        time.sleep(0.3)


def main() -> None:
    local_tz = ZoneInfo(os.getenv("NEWS_TIMEZONE", "Europe/Helsinki"))
    local_time = datetime.now(local_tz)
    dry_run = "--dry-run" in sys.argv
    if not dry_run and (not os.getenv("TG_BOT_TOKEN") or not os.getenv("TG_CHAT_ID")):
        raise RuntimeError("Missing TG_BOT_TOKEN or TG_CHAT_ID")

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=24)
    all_news: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
        tasks = {executor.submit(request, url): name for name, url in FEEDS.items()}
        for task in concurrent.futures.as_completed(tasks):
            name = tasks[task]
            try:
                articles = parse_feed(task.result(), name, cutoff, now_utc)
                all_news.extend(articles)
                print(f"{name}: {len(articles)} recent items")
            except (urllib.error.URLError, ET.ParseError, TimeoutError) as exc:
                print(f"RSS unavailable [{name}]: {type(exc).__name__}", file=sys.stderr)

    if not all_news:
        raise RuntimeError("No RSS sources accessible; digest not sent")
    threshold = float(os.getenv("NEWS_MIN_SCORE", "34"))
    stories = rank_stories(all_news, min_score=threshold)
    blocks = render_blocks(stories, local_time)
    messages = pack_telegram_messages(blocks)
    if dry_run:
        print(f"PREVIEW: {len(stories)} stories; {len(messages)} Telegram messages")
        print("\n\n".join(messages))
        return
    send_telegram(blocks)
    print(f"Sent {len(stories)} distinct prominent stories in "
          f"{len(messages)} Telegram messages")


if __name__ == "__main__":
    main()
