"""
EventAgent — Live Events Intelligence for Ora
=============================================
Fetches, stores, and deduplicates local events from multiple sources.

Architecture: conveyor belt
  • Pipeline depth : 14 days (always fetching ahead)
  • Serving window : 7 days  (what users see)
  • Daily sync     : adds day 14 as day 1 rolls off
  • DB filter      : starts_at > NOW()  (never serve past events)

Sources (in priority order):
  1. SerpAPI Google Events — richest data; gated on SERPAPI_KEY env var
  2. Eventbrite public API — gated on EVENTBRITE_TOKEN env var
  3. Ticketmaster Discovery API — gated on TICKETMASTER_API_KEY env var
  4. Meetup.com HTML scraper — no auth, best-effort
  5. Generic public JSON-LD scrapers — best-effort for platforms exposing schema.org Event data

Redis caching:
  • Cache key  : events:city:<normalized_city>
  • TTL        : 12 hours (daily sync keeps it fresh)
  • Cache stores raw event dicts BEFORE embedding so re-embed is cheap

Note: Set SERPAPI_KEY in Railway env vars to enable Google Events fetching.
Note: Set EVENTBRITE_TOKEN in Railway env vars to enable Eventbrite fetching.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.config import settings
from core.database import execute, fetch, fetchrow, fetchval

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

REDIS_TTL_SECONDS = 12 * 3600          # 12 hours
FETCH_HORIZON_DAYS = 14                # pull up to 14 days ahead from sources
SERVE_WINDOW_DAYS = 7                  # recommendation endpoint shows next 7 days
MAX_SERVE_WINDOW_DAYS = 14             # hard cap on days_ahead param

EVENT_PLATFORM_SCRAPE_TARGETS: Dict[str, str] = {
    # Public event search pages that commonly expose schema.org Event JSON-LD.
    # These are best-effort and intentionally read-only; API adapters remain
    # preferred whenever a platform offers one and credentials are configured.
    "eventbrite_page": "https://www.eventbrite.com/d/{city_slug}/events/",
    "meetup_page": "https://www.meetup.com/find/?location={city_query}&source=EVENTS",
}

# Category heuristics: keyword → canonical category
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "wellness":    ["wellness", "yoga", "meditation", "breathwork", "mindfulness",
                    "sound bath", "healing", "retreat", "fitness", "health"],
    "music":       ["concert", "music", "band", "dj", "jazz", "orchestra", "choir",
                    "festival", "open mic", "gig", "live music"],
    "tech":        ["tech", "startup", "hackathon", "developer", "ai ", "data science",
                    "coding", "product", "ux", "design sprint"],
    "arts":        ["art", "gallery", "exhibition", "film", "theatre", "theater",
                    "comedy", "improv", "poetry", "spoken word", "dance", "visual"],
    "food":        ["food", "wine", "beer", "tasting", "dining", "culinary", "market",
                    "restaurant", "chef", "cocktail", "brunch", "pop-up"],
    "sports":      ["run", "race", "triathlon", "cycling", "sport", "marathon",
                    "tournament", "league", "match", "hike", "outdoor"],
    "networking":  ["networking", "meetup", "mixer", "professional", "career",
                    "conference", "summit", "workshop", "seminar"],
    "community":   ["community", "volunteer", "fundraiser", "charity", "social",
                    "neighbourhood", "local", "market", "festival", "fair"],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_city(city: str) -> str:
    return city.strip().lower().replace(" ", "_")


def _make_external_id(source: str, raw_id: str) -> str:
    return f"{source}:{raw_id}"


def _infer_category(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return "general"


def _infer_tags(title: str, description: str, category: str) -> List[str]:
    text = f"{title} {description}".lower()
    tags = {category}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            tags.add(cat)
    return list(tags)


def _parse_price(raw: str) -> str:
    """Normalize price strings: 'Free' → 'free', '$10.00' → '$10', etc."""
    if not raw:
        return "unknown"
    low = raw.lower().strip()
    if any(w in low for w in ("free", "no cost", "$0", "complimentary")):
        return "free"
    # Extract dollar amounts
    amounts = re.findall(r"\$?\d+(?:\.\d{2})?", low)
    if len(amounts) >= 2:
        return f"${amounts[0]}–${amounts[1]}"
    if len(amounts) == 1:
        return f"${amounts[0]}"
    return raw.strip()[:40]


def _title_slug(title: str, starts_at: Optional[datetime], city: str) -> str:
    """Create a fuzzy dedup key: normalized title + date + city."""
    title_norm = re.sub(r"[^a-z0-9]", "", title.lower())[:60]
    date_str = starts_at.strftime("%Y%m%d") if starts_at else "nodate"
    city_norm = re.sub(r"[^a-z0-9]", "", city.lower())[:20]
    return f"{title_norm}|{date_str}|{city_norm}"


def _dedup_events(events: List[Dict]) -> List[Dict]:
    """
    Deduplicate a batch of raw event dicts.
    Strategy:
      1. external_id uniqueness (primary key in DB handles this too)
      2. Fuzzy slug: normalized_title + date + city
    """
    seen_ids: set = set()
    seen_slugs: set = set()
    result: List[Dict] = []
    for ev in events:
        eid = ev.get("external_id", "")
        slug = _title_slug(
            ev.get("title", ""),
            ev.get("starts_at"),
            ev.get("city", ""),
        )
        if eid in seen_ids or slug in seen_slugs:
            continue
        seen_ids.add(eid)
        seen_slugs.add(slug)
        result.append(ev)
    return result



def _city_slug(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")


def _fetch_url(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_event_datetime(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc) if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%a, %b %d, %Y", "%b %d, %Y", "%A, %B %d, %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _first_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return html.unescape(re.sub(r"<[^>]+>", " ", value)).strip()
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("text", "name", "url", "content"):
            text = _first_text(value.get(key))
            if text:
                return text
    return str(value).strip()


def _jsonld_nodes(value: Any) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            nodes.extend(_jsonld_nodes(item))
    elif isinstance(value, dict):
        nodes.append(value)
        for key in ("@graph", "itemListElement", "events"):
            if key in value:
                nodes.extend(_jsonld_nodes(value.get(key)))
        item = value.get("item")
        if isinstance(item, dict):
            nodes.extend(_jsonld_nodes(item))
    return nodes


def _is_jsonld_event(item: Dict[str, Any]) -> bool:
    raw_type = item.get("@type") or item.get("type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    return any(str(t).lower().endswith("event") for t in types if t)


def _location_parts(location: Any) -> Tuple[str, str, Optional[float], Optional[float]]:
    if isinstance(location, list):
        location = location[0] if location else {}
    if isinstance(location, str):
        return "", location, None, None
    if not isinstance(location, dict):
        return "", "", None, None
    venue = _first_text(location.get("name"))
    address_obj = location.get("address") or {}
    if isinstance(address_obj, str):
        address = address_obj
    elif isinstance(address_obj, dict):
        address = ", ".join(
            part for part in [
                _first_text(address_obj.get("streetAddress")),
                _first_text(address_obj.get("addressLocality")),
                _first_text(address_obj.get("addressRegion")),
                _first_text(address_obj.get("postalCode")),
                _first_text(address_obj.get("addressCountry")),
            ] if part
        )
    else:
        address = ""
    geo = location.get("geo") or {}
    lat = geo.get("latitude") if isinstance(geo, dict) else None
    lng = geo.get("longitude") if isinstance(geo, dict) else None
    try:
        lat = float(lat) if lat not in (None, "") else None
        lng = float(lng) if lng not in (None, "") else None
    except (TypeError, ValueError):
        lat = lng = None
    return venue, address, lat, lng


def _offer_price(offers: Any) -> str:
    offers_list = offers if isinstance(offers, list) else [offers]
    prices = []
    for offer in offers_list:
        if not isinstance(offer, dict):
            continue
        price = offer.get("price")
        currency = offer.get("priceCurrency") or "$"
        if price in (None, ""):
            continue
        if str(price) in {"0", "0.0", "0.00"}:
            return "free"
        prices.append(f"{currency}{price}" if str(currency) != "$" else f"${price}")
    return "–".join(prices[:2]) if prices else "unknown"


def _image_url(image: Any) -> str:
    if isinstance(image, list):
        return _image_url(image[0]) if image else ""
    if isinstance(image, dict):
        return _first_text(image.get("url") or image.get("contentUrl"))
    return _first_text(image)


def _event_from_jsonld(item: Dict[str, Any], city: str, source: str, page_url: str) -> Optional[Dict]:
    title = _first_text(item.get("name"))
    starts_at = _parse_event_datetime(item.get("startDate") or item.get("start_date"))
    if not title or not starts_at or starts_at < datetime.now(timezone.utc):
        return None
    ends_at = _parse_event_datetime(item.get("endDate") or item.get("end_date"))
    description = _first_text(item.get("description"))[:2000]
    venue, address, lat, lng = _location_parts(item.get("location"))
    event_url = _first_text(item.get("url")) or page_url
    raw_id = item.get("identifier") or item.get("@id") or event_url or f"{title}{starts_at.isoformat()}"
    if isinstance(raw_id, dict):
        raw_id = raw_id.get("value") or raw_id.get("@id") or json.dumps(raw_id, sort_keys=True)
    external_id = _make_external_id(source, hashlib.md5(str(raw_id).encode()).hexdigest()[:16])
    category = _infer_category(title, description)
    return {
        "external_id": external_id,
        "title": title,
        "description": description,
        "category": category,
        "venue_name": venue,
        "address": address,
        "city": city,
        "latitude": lat,
        "longitude": lng,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "url": event_url,
        "image_url": _image_url(item.get("image")),
        "price_range": _offer_price(item.get("offers")),
        "source": source,
        "relevance_tags": _infer_tags(title, description, category),
    }


def _extract_jsonld_events(html_text: str, city: str, source: str, page_url: str) -> List[Dict]:
    parsed: List[Dict] = []
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    for script in scripts:
        try:
            data = json.loads(html.unescape(script.strip()))
        except json.JSONDecodeError:
            continue
        for node in _jsonld_nodes(data):
            if not _is_jsonld_event(node):
                continue
            event = _event_from_jsonld(node, city, source, page_url)
            if event:
                parsed.append(event)
    return _dedup_events(parsed)


def _scrape_jsonld_event_platform(city: str, source: str, url_template: str) -> List[Dict]:
    try:
        url = url_template.format(
            city_slug=_city_slug(city),
            city_query=urllib.parse.quote(city),
        )
        html_text = _fetch_url(url)
        events = _extract_jsonld_events(html_text, city, source, url)
        logger.info(f"{source}: extracted {len(events)} JSON-LD future events for {city}")
        return events
    except Exception as e:
        logger.warning(f"{source} scrape failed for {city}: {e}")
        return []


# ── Source: Ticketmaster Discovery API ───────────────────────────────────────

def _fetch_ticketmaster_events(city: str, api_key: str) -> List[Dict]:
    """Fetch public Ticketmaster events when TICKETMASTER_API_KEY is configured."""
    if not api_key:
        return []
    try:
        now_utc = datetime.now(timezone.utc)
        horizon = now_utc + timedelta(days=FETCH_HORIZON_DAYS)
        params = {
            "apikey": api_key,
            "city": city,
            "countryCode": "CA",
            "startDateTime": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": horizon.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sort": "date,asc",
            "size": "50",
        }
        url = "https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode(params)
        data = json.loads(_fetch_url(url))
        raw_events = ((data.get("_embedded") or {}).get("events") or [])
        parsed: List[Dict] = []
        for ev in raw_events:
            starts_raw = ((ev.get("dates") or {}).get("start") or {}).get("dateTime")
            starts_at = _parse_event_datetime(starts_raw)
            if not starts_at or starts_at < now_utc:
                continue
            title = _first_text(ev.get("name"))
            description = _first_text(ev.get("info") or ev.get("pleaseNote"))
            venues = ((ev.get("_embedded") or {}).get("venues") or [])
            venue = venues[0] if venues else {}
            address_obj = venue.get("address") or {}
            city_obj = venue.get("city") or {}
            state_obj = venue.get("state") or {}
            address = ", ".join(
                part for part in [
                    _first_text(address_obj.get("line1")),
                    _first_text(city_obj.get("name")),
                    _first_text(state_obj.get("stateCode") or state_obj.get("name")),
                ] if part
            )
            loc = venue.get("location") or {}
            try:
                lat = float(loc.get("latitude")) if loc.get("latitude") else None
                lng = float(loc.get("longitude")) if loc.get("longitude") else None
            except (TypeError, ValueError):
                lat = lng = None
            price_ranges = ev.get("priceRanges") or []
            price_range = "unknown"
            if price_ranges:
                pr = price_ranges[0]
                currency = pr.get("currency") or "$"
                min_p = pr.get("min")
                max_p = pr.get("max")
                price_range = f"{currency}{min_p}–{currency}{max_p}" if min_p and max_p else "paid"
            images = ev.get("images") or []
            image = images[0].get("url", "") if images else ""
            category = _infer_category(title, description)
            parsed.append({
                "external_id": _make_external_id("ticketmaster", ev.get("id") or hashlib.md5(title.encode()).hexdigest()[:12]),
                "title": title,
                "description": description[:2000],
                "category": category,
                "venue_name": _first_text(venue.get("name")),
                "address": address,
                "city": city,
                "latitude": lat,
                "longitude": lng,
                "starts_at": starts_at,
                "ends_at": None,
                "url": ev.get("url", ""),
                "image_url": image,
                "price_range": price_range,
                "source": "ticketmaster",
                "relevance_tags": _infer_tags(title, description, category),
            })
        logger.info(f"Ticketmaster: fetched {len(parsed)} future events for {city}")
        return parsed
    except Exception as e:
        logger.warning(f"Ticketmaster fetch failed for {city}: {e}")
        return []

# ── Source 1: SerpAPI Google Events ──────────────────────────────────────────

def _fetch_serpapi_events(city: str, api_key: str) -> List[Dict]:
    """
    Fetch events via SerpAPI Google Events engine.
    Set SERPAPI_KEY in Railway env vars to enable.
    """
    try:
        params = {
            "engine":  "google_events",
            "q":       f"events in {city}",
            "api_key": api_key,
            "hl":      "en",
            "gl":      "us",
            "num":     "40",
        }
        url = "https://serpapi.com/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "ConnectomeEventAgent/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())

        raw_events = data.get("events_results", [])
        parsed: List[Dict] = []

        for ev in raw_events:
            date_info = ev.get("date", {})
            starts_at = _parse_serpapi_date(date_info.get("start_date", ""),
                                             date_info.get("when", ""))
            if not starts_at:
                continue
            # Only store future events
            if starts_at < datetime.now(timezone.utc):
                continue

            title = ev.get("title", "").strip()
            description = ev.get("description", "").strip()
            address_info = ev.get("address", [])
            address = ", ".join(address_info) if isinstance(address_info, list) else str(address_info)
            venue_name = ev.get("venue", {}).get("name", "")
            external_id = _make_external_id("serpapi", ev.get("event_id", "") or
                                             hashlib.md5(f"{title}{starts_at}".encode()).hexdigest()[:12])

            parsed.append({
                "external_id":   external_id,
                "title":         title,
                "description":   description[:2000],
                "category":      _infer_category(title, description),
                "venue_name":    venue_name,
                "address":       address,
                "city":          city,
                "latitude":      None,
                "longitude":     None,
                "starts_at":     starts_at,
                "ends_at":       None,
                "url":           ev.get("link", ""),
                "image_url":     (ev.get("thumbnail") or ""),
                "price_range":   _parse_price(ev.get("ticket_info", [{}])[0].get("source", "")
                                              if ev.get("ticket_info") else ""),
                "source":        "serpapi",
                "relevance_tags": _infer_tags(title, description,
                                              _infer_category(title, description)),
            })

        logger.info(f"SerpAPI: fetched {len(parsed)} future events for {city}")
        return parsed

    except Exception as e:
        logger.warning(f"SerpAPI fetch failed for {city}: {e}")
        return []


def _parse_serpapi_date(start_date: str, when_str: str) -> Optional[datetime]:
    """Parse SerpAPI date strings to UTC datetime."""
    # Try ISO format first
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(start_date.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, AttributeError):
            continue

    # Parse human-readable "when" string like "Mon, Jun 2, 6 PM"
    if when_str:
        try:
            # Extract date part before ","  e.g. "Mon, Jun 2" from "Mon, Jun 2, 6 PM – 9 PM"
            parts = when_str.split(",")
            if len(parts) >= 2:
                year = datetime.now(timezone.utc).year
                date_str = f"{parts[0].strip()} {parts[1].strip()} {year}"
                dt = datetime.strptime(date_str, "%a %b %d %Y")
                return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return None


# ── Source 2: Eventbrite public API ──────────────────────────────────────────

def _fetch_eventbrite_events(city: str, token: str) -> List[Dict]:
    """
    Fetch public events from Eventbrite.
    Set EVENTBRITE_TOKEN in Railway env vars to enable full results.
    Falls back to public (unauthenticated) if token is empty.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        horizon = now_utc + timedelta(days=FETCH_HORIZON_DAYS)

        params = {
            "location.address":       city,
            "location.within":        "25mi",
            "sort_by":                "date",
            "expand":                 "venue",
            "start_date.range_start": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start_date.range_end":   horizon.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "page_size":              "50",
        }
        if token:
            params["token"] = token

        url = "https://www.eventbriteapi.com/v3/events/search/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "ConnectomeEventAgent/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())

        raw_events = data.get("events", [])
        parsed: List[Dict] = []

        for ev in raw_events:
            try:
                starts_str = ev.get("start", {}).get("utc", "")
                starts_at = datetime.strptime(starts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc) if starts_str else None
                if not starts_at or starts_at < now_utc:
                    continue

                ends_str = ev.get("end", {}).get("utc", "")
                ends_at = datetime.strptime(ends_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc) if ends_str else None

                title = (ev.get("name", {}).get("text") or "").strip()
                description = (ev.get("description", {}).get("text") or "").strip()
                venue = ev.get("venue") or {}
                address_obj = venue.get("address") or {}
                address = address_obj.get("localized_address_display", "")
                lat = float(address_obj.get("latitude", 0) or 0) or None
                lng = float(address_obj.get("longitude", 0) or 0) or None

                is_free = ev.get("is_free", False)
                price_range = "free" if is_free else "paid"

                external_id = _make_external_id("eventbrite", ev.get("id", ""))

                parsed.append({
                    "external_id":    external_id,
                    "title":          title,
                    "description":    description[:2000],
                    "category":       _infer_category(title, description),
                    "venue_name":     venue.get("name", ""),
                    "address":        address,
                    "city":           city,
                    "latitude":       lat,
                    "longitude":      lng,
                    "starts_at":      starts_at,
                    "ends_at":        ends_at,
                    "url":            ev.get("url", ""),
                    "image_url":      (ev.get("logo", {}) or {}).get("url", ""),
                    "price_range":    price_range,
                    "source":         "eventbrite",
                    "relevance_tags": _infer_tags(title, description,
                                                  _infer_category(title, description)),
                })
            except Exception as ev_err:
                logger.debug(f"Eventbrite event parse error: {ev_err}")
                continue

        logger.info(f"Eventbrite: fetched {len(parsed)} future events for {city}")
        return parsed

    except Exception as e:
        logger.warning(f"Eventbrite fetch failed for {city}: {e}")
        return []


# ── Source 3: Meetup.com HTML scraper ────────────────────────────────────────

def _scrape_meetup_events(city: str) -> List[Dict]:
    """
    Best-effort HTML scrape of Meetup.com event listings.
    No auth required. Fragile — HTML structure can change.
    """
    try:
        city_slug = city.lower().replace(" ", "-")
        url = (f"https://www.meetup.com/find/?location={urllib.parse.quote(city)}"
               f"&source=EVENTS&distance=tenMiles")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Extract JSON-LD structured data (most reliable approach)
        json_ld_matches = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL
        )

        parsed: List[Dict] = []
        now_utc = datetime.now(timezone.utc)

        for match in json_ld_matches:
            try:
                data = json.loads(match.strip())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Event", "SocialEvent"):
                        continue
                    title = item.get("name", "").strip()
                    if not title:
                        continue

                    start_raw = item.get("startDate", "")
                    starts_at = None
                    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
                        try:
                            starts_at = datetime.strptime(start_raw, fmt)
                            if starts_at.tzinfo is None:
                                starts_at = starts_at.replace(tzinfo=timezone.utc)
                            starts_at = starts_at.astimezone(timezone.utc)
                            break
                        except ValueError:
                            continue

                    if not starts_at or starts_at < now_utc:
                        continue

                    description = item.get("description", "").strip()
                    location = item.get("location") or {}
                    venue_name = location.get("name", "")
                    address_obj = location.get("address") or {}
                    address = (
                        address_obj.get("streetAddress", "") + " " +
                        address_obj.get("addressLocality", "")
                    ).strip()

                    event_url = item.get("url", "")
                    image = item.get("image", "")
                    if isinstance(image, list):
                        image = image[0] if image else ""

                    slug = hashlib.md5(f"{title}{starts_at}".encode()).hexdigest()[:12]
                    external_id = _make_external_id("meetup_scrape", slug)

                    parsed.append({
                        "external_id":    external_id,
                        "title":          title,
                        "description":    description[:2000],
                        "category":       _infer_category(title, description),
                        "venue_name":     venue_name,
                        "address":        address,
                        "city":           city,
                        "latitude":       None,
                        "longitude":      None,
                        "starts_at":      starts_at,
                        "ends_at":        None,
                        "url":            event_url,
                        "image_url":      image if isinstance(image, str) else "",
                        "price_range":    "unknown",
                        "source":         "meetup_scrape",
                        "relevance_tags": _infer_tags(title, description,
                                                      _infer_category(title, description)),
                    })
            except json.JSONDecodeError:
                continue
            except Exception as item_err:
                logger.debug(f"Meetup JSON-LD parse error: {item_err}")
                continue

        logger.info(f"Meetup scrape: found {len(parsed)} future events for {city}")
        return parsed

    except Exception as e:
        logger.warning(f"Meetup scrape failed for {city}: {e}")
        return []


# ── Embedding ─────────────────────────────────────────────────────────────────

async def _generate_embedding(text: str, openai_client) -> Optional[List[float]]:
    """Generate an OpenAI text-embedding-3-small embedding."""
    if not openai_client:
        return None
    try:
        text = text.strip()[:4000]  # keep within token budget
        resp = await openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        return resp.data[0].embedding
    except Exception as e:
        logger.warning(f"Embedding generation failed: {e}")
        return None


# ── DB upsert ─────────────────────────────────────────────────────────────────

async def _upsert_events(events: List[Dict], openai_client) -> int:
    """
    Upsert events into the DB.
    Deduplicates at DB level on external_id.
    Also checks title+starts_at+city slug to catch cross-source dupes.
    Returns count of new events inserted.
    """
    if not events:
        return 0

    inserted = 0

    for ev in events:
        try:
            # Cross-source fuzzy dedup: check if near-identical event exists
            if ev.get("starts_at"):
                existing = await fetchrow(
                    """
                    SELECT id FROM events
                    WHERE city = $1
                      AND starts_at BETWEEN $2 AND $3
                      AND LOWER(title) = LOWER($4)
                    LIMIT 1
                    """,
                    ev["city"],
                    ev["starts_at"] - timedelta(hours=2),
                    ev["starts_at"] + timedelta(hours=2),
                    ev["title"],
                )
                if existing:
                    continue  # cross-source duplicate

            # Generate embedding
            embed_text = f"{ev.get('title', '')}: {ev.get('description', '')}"
            embedding = await _generate_embedding(embed_text, openai_client)
            embed_str = f"[{','.join(str(x) for x in embedding)}]" if embedding else None

            await execute(
                """
                INSERT INTO events (
                    external_id, title, description, category,
                    venue_name, address, city, latitude, longitude,
                    starts_at, ends_at, url, image_url,
                    price_range, source, relevance_tags, embedding,
                    updated_at
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8, $9,
                    $10, $11, $12, $13,
                    $14, $15, $16, $17::vector,
                    NOW()
                )
                ON CONFLICT (external_id) DO UPDATE SET
                    title         = EXCLUDED.title,
                    description   = EXCLUDED.description,
                    category      = EXCLUDED.category,
                    venue_name    = EXCLUDED.venue_name,
                    address       = EXCLUDED.address,
                    starts_at     = EXCLUDED.starts_at,
                    ends_at       = EXCLUDED.ends_at,
                    url           = EXCLUDED.url,
                    image_url     = EXCLUDED.image_url,
                    price_range   = EXCLUDED.price_range,
                    relevance_tags = EXCLUDED.relevance_tags,
                    embedding     = EXCLUDED.embedding,
                    updated_at    = NOW()
                """,
                ev.get("external_id"),
                ev.get("title", "")[:500],
                ev.get("description", ""),
                ev.get("category", "general"),
                ev.get("venue_name", ""),
                ev.get("address", ""),
                ev.get("city", ""),
                ev.get("latitude"),
                ev.get("longitude"),
                ev.get("starts_at"),
                ev.get("ends_at"),
                ev.get("url", ""),
                ev.get("image_url", ""),
                ev.get("price_range", "unknown"),
                ev.get("source", "unknown"),
                ev.get("relevance_tags", []),
                embed_str,
            )
            try:
                from ora.agents.ioo_graph_agent import get_graph_agent

                await get_graph_agent().upsert_world_signal_node({
                    "id": ev.get("external_id"),
                    "external_id": ev.get("external_id"),
                    "signal_type": "event",
                    "source": ev.get("source", "events"),
                    "title": ev.get("title", ""),
                    "summary": ev.get("description", ""),
                    "url": ev.get("url", ""),
                    "location": ev.get("city", ""),
                    "city": ev.get("city", ""),
                    "tags": ev.get("relevance_tags", []) or [ev.get("category", "general")],
                    "relevance_score": 0.74,
                    "starts_at": ev.get("starts_at"),
                })
            except Exception as graph_e:
                logger.debug(f"EventAgent IOO graph ingest skipped for '{ev.get('title', '?')}': {graph_e}")
            inserted += 1

        except Exception as e:
            logger.warning(f"Upsert failed for '{ev.get('title', '?')}': {e}")
            continue

    return inserted


# ── Prune expired events ───────────────────────────────────────────────────────

async def prune_past_events() -> int:
    """Remove events that have already started. Called after each sync."""
    result = await execute(
        "DELETE FROM events WHERE starts_at < NOW() - INTERVAL '1 hour'"
    )
    # asyncpg returns 'DELETE N'
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count:
        logger.info(f"Pruned {count} past events from DB")
    return count


# ── Main public interface ──────────────────────────────────────────────────────

class EventAgent:
    """
    Fetches and stores local events for a city.

    Conveyor belt model:
      - FETCH_HORIZON_DAYS (14): window pulled from sources
      - SERVE_WINDOW_DAYS (7):   window shown to users
      - Daily sync keeps the pipeline stocked as days roll off
    """

    def __init__(self, openai_client=None):
        self.openai = openai_client
        self._serpapi_key = getattr(settings, "SERPAPI_KEY", "") or os.getenv("SERPAPI_KEY", "")
        self._eb_token = getattr(settings, "EVENTBRITE_TOKEN", "") or os.getenv("EVENTBRITE_TOKEN", "")
        self._ticketmaster_key = getattr(settings, "TICKETMASTER_API_KEY", "") or os.getenv("TICKETMASTER_API_KEY", "")

    async def sync_city(self, city: str, force: bool = False) -> Dict[str, Any]:
        """
        Fetch fresh events for a city and upsert into DB.

        Steps:
          1. Check Redis cache (12h TTL) — skip if fresh unless force=True
          2. Fetch from all available sources
          3. Deduplicate across sources
          4. Upsert into DB (with cross-source fuzzy dedup)
          5. Prune expired events
          6. Update Redis cache

        Returns summary dict with counts per source.
        """
        from core.redis_client import get_redis

        cache_key = f"events:city:{_normalize_city(city)}"

        # Check cache
        if not force:
            try:
                r = await get_redis()
                cached = await r.get(cache_key)
                if cached:
                    logger.info(f"EventAgent: cache hit for {city}, skipping sync")
                    return json.loads(cached)
            except Exception as redis_err:
                logger.debug(f"Redis cache check failed: {redis_err}")

        # Fetch from sources
        all_events: List[Dict] = []
        source_counts: Dict[str, int] = {}

        # Source 1: SerpAPI
        if self._serpapi_key:
            serp_events = _fetch_serpapi_events(city, self._serpapi_key)
            all_events.extend(serp_events)
            source_counts["serpapi"] = len(serp_events)
        else:
            logger.info("EventAgent: SERPAPI_KEY not set — skipping Google Events (set in Railway env vars)")
            source_counts["serpapi"] = 0

        # Source 2: Eventbrite
        eb_events = _fetch_eventbrite_events(city, self._eb_token)
        all_events.extend(eb_events)
        source_counts["eventbrite"] = len(eb_events)

        # Source 3: Ticketmaster Discovery API
        if self._ticketmaster_key:
            tm_events = _fetch_ticketmaster_events(city, self._ticketmaster_key)
            all_events.extend(tm_events)
            source_counts["ticketmaster"] = len(tm_events)
        else:
            source_counts["ticketmaster"] = 0

        # Source 4: legacy Meetup JSON-LD scrape
        meetup_events = _scrape_meetup_events(city)
        all_events.extend(meetup_events)
        source_counts["meetup_scrape"] = len(meetup_events)

        # Source 5+: generic public JSON-LD platform scrapers
        for source, url_template in EVENT_PLATFORM_SCRAPE_TARGETS.items():
            if source == "meetup_page":
                # The legacy Meetup parser above has richer handling; keep this
                # target documented but avoid double-fetching by default.
                continue
            platform_events = _scrape_jsonld_event_platform(city, source, url_template)
            all_events.extend(platform_events)
            source_counts[source] = len(platform_events)

        # Filter to fetch horizon window
        horizon = datetime.now(timezone.utc) + timedelta(days=FETCH_HORIZON_DAYS)
        all_events = [
            ev for ev in all_events
            if ev.get("starts_at") and ev["starts_at"] < horizon
        ]

        # Dedup within this batch
        all_events = _dedup_events(all_events)

        # Upsert into DB
        inserted = await _upsert_events(all_events, self.openai)

        # Prune expired events
        pruned = await prune_past_events()

        summary = {
            "city":          city,
            "total_fetched": len(all_events),
            "inserted":      inserted,
            "pruned":        pruned,
            "sources":       source_counts,
            "synced_at":     datetime.now(timezone.utc).isoformat(),
        }

        # Cache summary in Redis
        try:
            r = await get_redis()
            await r.setex(cache_key, REDIS_TTL_SECONDS, json.dumps(summary))
        except Exception as redis_err:
            logger.debug(f"Redis cache write failed: {redis_err}")

        logger.info(
            f"EventAgent sync complete for {city}: "
            f"{len(all_events)} fetched, {inserted} inserted, {pruned} pruned"
        )
        return summary

    async def get_events_for_city(
        self,
        city: str,
        days_ahead: int = SERVE_WINDOW_DAYS,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """
        Return upcoming events for a city from the DB.
        Always filters starts_at > NOW().
        Capped at MAX_SERVE_WINDOW_DAYS.
        """
        days_ahead = min(max(1, days_ahead), MAX_SERVE_WINDOW_DAYS)
        until = datetime.now(timezone.utc) + timedelta(days=days_ahead)

        params: List[Any] = [city, until]
        cat_clause = ""
        if category:
            params.append(category)
            cat_clause = f"AND category = ${len(params)}"

        rows = await fetch(
            f"""
            SELECT id, external_id, title, description, category,
                   venue_name, address, city, latitude, longitude,
                   starts_at, ends_at, url, image_url, price_range,
                   source, relevance_tags
            FROM events
            WHERE city = $1
              AND starts_at > NOW()
              AND starts_at < $2
              {cat_clause}
            ORDER BY starts_at ASC
            LIMIT {limit}
            """,
            *params,
        )
        return [dict(r) for r in rows]

    async def get_recommended_events(
        self,
        user_id: str,
        days_ahead: int = SERVE_WINDOW_DAYS,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Return personalized events for a user.
        Uses:
          1. user.city to scope events
          2. user.event_preferences for category filtering
          3. Semantic similarity between user embedding and event embeddings
        Falls back to recency-sorted events if no preferences/embedding.
        """
        from core.database import fetchrow as _fetchrow

        user = await _fetchrow(
            "SELECT city, event_preferences, embedding FROM users WHERE id = $1",
            user_id,
        )
        if not user:
            return []

        city = user.get("city", "")
        if not city:
            return []

        prefs = user.get("event_preferences") or []
        user_embedding = user.get("embedding")  # stored as string '[...]'
        days_ahead = min(max(1, days_ahead), MAX_SERVE_WINDOW_DAYS)
        until = datetime.now(timezone.utc) + timedelta(days=days_ahead)

        # Preference-based category filter
        pref_clause = ""
        params: List[Any] = [city, until]
        if prefs:
            params.append(prefs)
            pref_clause = f"AND (relevance_tags && ${len(params)} OR category = ANY(${len(params)}))"

        # Semantic similarity ranking
        order_clause = "ORDER BY starts_at ASC"
        if user_embedding:
            try:
                order_clause = f"ORDER BY embedding <=> '{user_embedding}' ASC, starts_at ASC"
            except Exception:
                pass

        rows = await fetch(
            f"""
            SELECT id, external_id, title, description, category,
                   venue_name, address, city, latitude, longitude,
                   starts_at, ends_at, url, image_url, price_range,
                   source, relevance_tags
            FROM events
            WHERE city = $1
              AND starts_at > NOW()
              AND starts_at < $2
              AND embedding IS NOT NULL
              {pref_clause}
            {order_clause}
            LIMIT {limit}
            """,
            *params,
        )

        # Fallback: if no results with embedding filter, relax and return any upcoming
        if not rows:
            rows = await fetch(
                f"""
                SELECT id, external_id, title, description, category,
                       venue_name, address, city, latitude, longitude,
                       starts_at, ends_at, url, image_url, price_range,
                       source, relevance_tags
                FROM events
                WHERE city = $1
                  AND starts_at > NOW()
                  AND starts_at < $2
                  {pref_clause}
                ORDER BY starts_at ASC
                LIMIT {limit}
                """,
                *params,
            )

        return [dict(r) for r in rows]
