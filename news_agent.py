#!/usr/bin/env python3
"""Daily autonomous AI-written news summaries sent straight to Telegram.

Uses news RSS + public article extracts as factual input and Cloudflare Workers AI
on the Workers Free plan for original Russian-language summaries. No paywalls.
The free-tier quota can run out; no guarantee of a particular number of summaries.
"""
from __future__ import annotations

import concurrent.futures
import difflib
import email.utils
import html
import ipaddress
import json
import random
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

USER_AGENT = "Mozilla/5.0 (compatible; PersonalNewsDigest/3.0; +https://github.com)"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
STOPWORDS = {
    "the", "and", "from", "with", "that", "this", "after", "about", "over", "into",
    "россии", "россия", "сегодня", "стало", "после", "который", "которые", "новости",
}


def rss(*, country: str, lang: str, category: str | None = None) -> str:
    lang_tag = f"{lang}-{country}" if lang == "en" else lang
    base = "https://news.google.com/rss"
    if category:
        base += f"/headlines/section/topic/{category}"
    return f"{base}?hl={lang_tag}&gl={country}&ceid={country}:{lang}"


# Google News aggregates multiple publishers; rank is a popularity *proxy*.
GOOGLE_FEEDS = {
    "Россия · Главное": rss(country="RU", lang="ru"),
    "Россия · Мир": rss(country="RU", lang="ru", category="WORLD"),
    "Россия · Страна": rss(country="RU", lang="ru", category="NATION"),
    "Россия · Экономика": rss(country="RU", lang="ru", category="BUSINESS"),
    "Россия · Технологии": rss(country="RU", lang="ru", category="TECHNOLOGY"),
    "Россия · Наука": rss(country="RU", lang="ru", category="SCIENCE"),
    "Россия · Здоровье": rss(country="RU", lang="ru", category="HEALTH"),
    "Россия · Спорт": rss(country="RU", lang="ru", category="SPORTS"),
    "Россия · Культура": rss(country="RU", lang="ru", category="ENTERTAINMENT"),
    "Мир · США": rss(country="US", lang="en"),
    "Мир · США/World": rss(country="US", lang="en", category="WORLD"),
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

# Links from publishers' RSS are directly fetchable; failures are nonfatal.
# Each publisher has its own perspective: we do not present one as unbiased.
PUBLISHER_FEEDS = {
    "РИА Новости": "https://ria.ru/export/rss2/index.xml",
    "Интерфакс": "https://www.interfax.ru/rss.asp",
    "ТАСС": "https://tass.ru/rss/v2.xml",
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC Technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "The Guardian World": "https://www.theguardian.com/world/rss",
    "The Guardian Science": "https://www.theguardian.com/science/rss",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "New York Times World": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "New York Times Science": "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
}


def text_from_html(markup: str) -> str:
    # RSS descriptions can contain HTML. Avoid treating metadata lists as news text.
    markup = re.sub(r"<\s*br\s*/?\s*>", "\n", markup or "", flags=re.I)
    markup = re.sub(r"<\s*/\s*p\s*>", "\n\n", markup, flags=re.I)
    markup = re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"[ \t]+", " ", html.unescape(markup)).strip()


def clean_title(title: str) -> str:
    return re.sub(r"\s+-\s+[^-]{2,90}$", "", text_from_html(title)).strip()


def safe_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or "." not in host:
            return False
        if host in ("localhost",) or host.endswith((".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def request(url: str, *, payload: dict | None = None, timeout: int = 16,
            max_bytes: int = 3_000_000) -> tuple[bytes, str]:
    if not safe_url(url):
        raise ValueError("Untrusted URL")
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
        if not safe_url(final):
