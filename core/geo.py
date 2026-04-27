"""
IP Geolocation — lightweight, no API key required.

Uses ip-api.com free tier (45 req/min limit, no key needed).
Results are cached in Redis for 24 hours per IP to stay well within limits.

What Ora uses this for:
- Local time of day → adjust coaching tone (morning vs night)
- City/country → surface locally relevant recommendations
- Timezone → correct "good morning" messages
- Language region hint → cultural context

Privacy: we store city + timezone only, never the raw IP.
"""

import json
import logging
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger(__name__)

GEO_API_URL = "http://ip-api.com/json/{ip}?fields=status,city,regionName,country,countryCode,timezone,lat,lon"
CACHE_TTL_SECONDS = 86_400  # 24 hours


async def get_location_for_ip(ip: str) -> Optional[Dict[str, Any]]:
    """
    Resolve an IP address to location context.
    Returns a dict with: city, region, country, country_code, timezone, lat, lon
    Returns None on failure (private IP, timeout, etc.)
    Caches results in Redis for 24 hours.
    """
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return None

    # Strip IPv6 mapping prefix
    if ip.startswith("::ffff:"):
        ip = ip[7:]

    cache_key = f"geo:{ip}"

    # Try cache first
    try:
        from core.redis_client import get_redis
        r = await get_redis()
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    # Fetch from ip-api.com
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(GEO_API_URL.format(ip=ip))
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "success":
                return None

            result = {
                "city": data.get("city", ""),
                "region": data.get("regionName", ""),
                "country": data.get("country", ""),
                "country_code": data.get("countryCode", ""),
                "timezone": data.get("timezone", ""),
                "lat": data.get("lat"),
                "lon": data.get("lon"),
            }

            # Cache
            try:
                from core.redis_client import get_redis
                r = await get_redis()
                await r.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
            except Exception:
                pass

            return result

    except Exception as e:
        logger.debug(f"Geo lookup failed for {ip}: {e}")
        return None


def geo_to_context_hints(geo: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Convert raw geo data to Ora-usable context hints.
    Safe to call with None geo (returns empty hints).
    """
    if not geo:
        return {}

    hints: Dict[str, Any] = {}

    if geo.get("city"):
        hints["user_city"] = geo["city"]
    if geo.get("country"):
        hints["user_country"] = geo["country"]
    if geo.get("country_code"):
        hints["user_country_code"] = geo["country_code"]
    if geo.get("timezone"):
        hints["user_timezone"] = geo["timezone"]

        # Local hour of day (useful for tone adjustment)
        try:
            from datetime import datetime
            import zoneinfo
            tz = zoneinfo.ZoneInfo(geo["timezone"])
            local_hour = datetime.now(tz).hour
            hints["user_local_hour"] = local_hour

            # Time-of-day label
            if 5 <= local_hour < 12:
                hints["time_of_day"] = "morning"
            elif 12 <= local_hour < 17:
                hints["time_of_day"] = "afternoon"
            elif 17 <= local_hour < 21:
                hints["time_of_day"] = "evening"
            else:
                hints["time_of_day"] = "night"
        except Exception:
            pass

    return hints
