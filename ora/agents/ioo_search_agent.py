"""
IOO Search Agent — production-shaped candidate discovery for execution runs.

This module prepares structured, actionable search candidates for an IOO
Execution Protocol without performing irreversible external actions. It is
intentionally side-effect free: until live provider integrations are wired in,
it returns trustworthy search/query surfaces and graceful fallback candidates
that downstream UX, scheduling, and resource agents can rank or present.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote_plus

SWARMSYNC_API_BASE = "https://api.swarmsync.ai"
SWARMSYNC_APP_BASE = "https://swarmsync.ai"

import httpx


@dataclass(frozen=True)
class CandidateSource:
    """Where a candidate came from, or where the user/agent should verify it."""

    name: str
    type: str
    url: str | None = None
    status: str = "query_ready"


@dataclass(frozen=True)
class CandidateNextAction:
    """The next reversible action for this candidate."""

    label: str
    action_type: str = "open_link"
    requires_confirmation: bool = False
    url: str | None = None


@dataclass(frozen=True)
class SearchCandidate:
    """A concrete option or discovery surface for making an IOO node real."""

    id: str
    title: str
    candidate_type: str
    source: CandidateSource
    confidence: float
    rationale: str
    next_action: CandidateNextAction
    metadata: dict[str, Any] = field(default_factory=dict)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except Exception:
        return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, str):
        return [value]
    return []


def _text_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _normalise_node(node: Any) -> dict[str, Any]:
    data = _as_dict(node)
    requirements = _as_dict(data.get("requirements"))
    tags = _text_list(data.get("tags"))
    required_skills = _text_list(data.get("requires_skills")) + _text_list(requirements.get("required_skills"))
    return {
        "id": str(data.get("id") or data.get("node_id") or ""),
        "title": str(data.get("title") or "Untitled IOO node"),
        "description": str(data.get("description") or ""),
        "type": str(data.get("type") or data.get("node_type") or "activity"),
        "domain": str(data.get("domain") or data.get("goal_category") or "IOO"),
        "step_type": str(data.get("step_type") or "hybrid"),
        "tags": list(dict.fromkeys(tags)),
        "requirements": requirements,
        "requires_location": data.get("requires_location"),
        "requires_skills": list(dict.fromkeys(required_skills)),
        "best_time": data.get("best_time"),
        "physical_context": data.get("physical_context"),
    }


def _normalise_context(user_context: Any) -> dict[str, Any]:
    context = _as_dict(user_context)
    profile = _as_dict(context.get("profile"))
    state_json = _as_dict(context.get("state_json"))
    merged = {**profile, **state_json, **context}
    merged["known_skills"] = _text_list(merged.get("known_skills"))
    return merged


def _context_location(context: dict[str, Any], node: dict[str, Any]) -> str | None:
    city = context.get("location_city") or context.get("city")
    country = context.get("location_country") or context.get("country")
    if city and country:
        return f"{city}, {country}"
    return city or country or node.get("requires_location")


def _query_url(provider: str, query: str) -> str:
    encoded = quote_plus(query)
    if provider == "google_maps":
        return f"https://www.google.com/maps/search/?api=1&query={encoded}"
    if provider == "github":
        return f"https://github.com/search?q={encoded}&type=repositories"
    if provider == "linkedin_jobs":
        return f"https://www.linkedin.com/jobs/search/?keywords={encoded}"
    if provider == "idealist":
        return f"https://www.idealist.org/en/jobs?q={encoded}"
    if provider == "volunteermatch":
        return f"https://www.volunteermatch.org/search/?keywords={encoded}"
    if provider == "eventbrite":
        return f"https://www.eventbrite.com/d/online/{encoded}/" if "online" in query.lower() else f"https://www.eventbrite.com/d/{encoded}/"
    return f"https://www.google.com/search?q={encoded}"


def _google_places_link(place_id: str | None, fallback_query: str) -> str:
    if place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{quote_plus(place_id)}"
    return _query_url("google_maps", fallback_query)


def _agent_marketplace_query(node: dict[str, Any], title: str) -> str | None:
    """Return a SwarmSync marketplace query for safely delegable IOO work."""

    tags = {tag.lower() for tag in node["tags"]}
    title_lower = title.lower()
    delegable_terms = {
        "agent",
        "agentic",
        "automation",
        "research",
        "analysis",
        "code",
        "coding",
        "developer",
        "open-source",
        "admin",
        "workflow",
        "market",
        "data",
    }
    if not (tags & delegable_terms or any(term in title_lower for term in delegable_terms)):
        return None

    if tags & {"code", "coding", "developer", "open-source"} or any(term in title_lower for term in ("code", "developer", "github", "open-source")):
        return f"code analysis automation {title}"
    if tags & {"research", "market", "data"} or any(term in title_lower for term in ("research", "analysis", "market", "data")):
        return f"research data analysis {title}"
    return f"agent automation workflow {title}"


def _live_swarmsync_agent_candidates(query: str | None, title: str) -> list[SearchCandidate]:
    """Return public SwarmSync agent-marketplace candidates when relevant.

    This is read-only discovery. It does not register Aura, negotiate AP2 work,
    lock escrow, execute agents, or spend money. Any hiring/action must happen
    only after explicit user confirmation in a separate execution step.
    """

    if not query:
        return []

    try:
        resp = httpx.get(f"{SWARMSYNC_API_BASE}/agents", timeout=6)
        resp.raise_for_status()
        agents = resp.json() or []
    except Exception:
        return []

    terms = [term for term in query.lower().replace("/", " ").split() if len(term) > 2]
    scored: list[tuple[float, dict[str, Any]]] = []
    for agent in agents if isinstance(agents, list) else []:
        if not isinstance(agent, dict):
            continue
        haystack = " ".join(
            str(agent.get(key) or "")
            for key in ("name", "slug", "description", "verificationStatus", "pricingModel")
        ).lower()
        haystack += " " + " ".join(str(item) for item in _as_list(agent.get("tags"))).lower()
        matched_terms = sum(1 for term in terms if term in haystack)
        if matched_terms == 0:
            continue
        trust_score = float(agent.get("trustScore") or 0)
        success_count = float(agent.get("successCount") or 0)
        score = matched_terms + min(trust_score / 100.0, 1.0) + min(success_count / 25.0, 0.5)
        scored.append((score, agent))

    candidates: list[SearchCandidate] = []
    seen_keys: set[str] = set()
    for _, agent in sorted(scored, key=lambda item: item[0], reverse=True):
        slug = str(agent.get("slug") or agent.get("id") or "").strip()
        name = str(agent.get("name") or slug).strip()
        dedupe_key = name.lower() or slug.lower()
        if not slug or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        idx = len(candidates) + 1
        if idx > 3:
            break
        agent_url = f"{SWARMSYNC_APP_BASE}/agents/{quote_plus(slug)}"
        trust_score = agent.get("trustScore")
        status = agent.get("verificationStatus") or agent.get("status")
        price_cents = agent.get("basePriceCents")
        confidence = 0.58 + max(0, 3 - idx) * 0.04
        if trust_score:
            confidence += min(float(trust_score) / 500.0, 0.12)
        candidates.append(
            _candidate(
                cid=f"swarmsync-agent-{idx}",
                title=f"SwarmSync agent: {name}",
                candidate_type="agent_marketplace_option",
                source_name="SwarmSync agent marketplace",
                source_type="live_agent_marketplace",
                source_url=agent_url,
                confidence=confidence,
                rationale=(
                    f"Public SwarmSync listing matched delegable work for '{title}'. "
                    "Use it as an outsourcing/research option only after explicit user approval."
                ),
                next_label="Review agent trust, price, and task fit before approving any hire or escrow",
                metadata={
                    "query": query,
                    "agent_id": agent.get("id"),
                    "slug": slug,
                    "description": agent.get("description"),
                    "verification_status": status,
                    "trust_score": trust_score,
                    "success_count": agent.get("successCount"),
                    "failure_count": agent.get("failureCount"),
                    "pricing_model": agent.get("pricingModel"),
                    "base_price_cents": price_cents,
                    "external_actions_require_confirmation": True,
                    "ap2_negotiation_performed": False,
                },
            )
        )
    return candidates


def _live_google_places_candidates(query: str, location: str | None, title: str) -> list[SearchCandidate]:
    """Return live Google Places candidates when a Places key is configured.

    This is read-only research: it fetches public venue/place metadata and
    returns links. It never books, purchases, messages, or changes user state.
    """
    try:
        from core.config import settings
    except Exception:
        return []

    api_key = getattr(settings, "GOOGLE_PLACES_API_KEY", "") or ""
    if not api_key:
        return []

    text_query = query.strip()
    if location and location.lower() not in text_query.lower():
        text_query = f"{text_query} {location}".strip()
    if not text_query:
        return []

    try:
        resp = httpx.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": ",".join([
                    "places.id",
                    "places.displayName",
                    "places.formattedAddress",
                    "places.googleMapsUri",
                    "places.rating",
                    "places.userRatingCount",
                    "places.priceLevel",
                    "places.websiteUri",
                    "places.currentOpeningHours.openNow",
                    "places.primaryTypeDisplayName",
                ]),
            },
            json={"textQuery": text_query, "maxResultCount": 3},
            timeout=6,
        )
        resp.raise_for_status()
        places = (resp.json() or {}).get("places") or []
    except Exception:
        return []

    candidates: list[SearchCandidate] = []
    for idx, place in enumerate(places[:3], start=1):
        name = ((place.get("displayName") or {}).get("text") or "").strip()
        if not name:
            continue
        maps_url = place.get("googleMapsUri") or _google_places_link(place.get("id"), f"{name} {location or ''}".strip())
        website_url = place.get("websiteUri")
        rating = place.get("rating")
        rating_count = place.get("userRatingCount")
        address = place.get("formattedAddress")
        open_now = ((place.get("currentOpeningHours") or {}).get("openNow"))
        type_label = ((place.get("primaryTypeDisplayName") or {}).get("text"))
        trust_bits = []
        if rating:
            trust_bits.append(f"rating {rating}")
        if rating_count:
            trust_bits.append(f"{rating_count} reviews")
        if open_now is not None:
            trust_bits.append("open now" if open_now else "check hours")
        trust = ", ".join(trust_bits) or "Google Places live result"
        confidence = 0.82 - ((idx - 1) * 0.04)
        candidates.append(
            _candidate(
                cid=f"google-place-{idx}",
                title=name,
                candidate_type="live_place",
                source_name="Google Places",
                source_type="live_places_result",
                source_url=maps_url,
                confidence=confidence,
                rationale=f"Live place result for '{title}' with public trust signals: {trust}.",
                next_label="Open Maps, check fit, hours, reviews, and route before committing",
                metadata={
                    "query": text_query,
                    "address": address,
                    "rating": rating,
                    "review_count": rating_count,
                    "open_now": open_now,
                    "place_type": type_label,
                    "website_url": website_url,
                    "maps_url": maps_url,
                    "location_used": location,
                },
            )
        )
    return candidates


def _candidate(
    *,
    cid: str,
    title: str,
    candidate_type: str,
    source_name: str,
    source_type: str,
    source_url: str | None,
    confidence: float,
    rationale: str,
    next_label: str,
    metadata: dict[str, Any] | None = None,
) -> SearchCandidate:
    return SearchCandidate(
        id=cid,
        title=title,
        candidate_type=candidate_type,
        source=CandidateSource(name=source_name, type=source_type, url=source_url),
        confidence=round(max(0.0, min(1.0, confidence)), 2),
        rationale=rationale,
        next_action=CandidateNextAction(label=next_label, url=source_url),
        metadata=metadata or {},
    )


def build_search_agent_payload(node: Any, user_context: Any, intent: str = "do_now") -> dict[str, Any]:
    """
    Return structured SearchAgent output for an IOO execution run.

    The payload is shaped like a future live-search integration: candidates have
    source/confidence/rationale/next_action metadata, and the top-level status
    records whether live providers were available or a graceful fallback was used.
    """

    n = _normalise_node(node)
    context = _normalise_context(user_context)
    location = _context_location(context, n)
    tags = {tag.lower() for tag in n["tags"]}
    title = n["title"]
    title_lower = title.lower()
    query_parts = [title]
    if n["domain"] and n["domain"] != "IOO":
        query_parts.append(n["domain"])
    if location and n["step_type"] in {"physical", "hybrid"}:
        query_parts.append(location)
    base_query = " ".join(query_parts)

    candidates: list[SearchCandidate] = []
    confidence_base = 0.66 if location or n["step_type"] == "digital" else 0.54

    live_places_available = False
    if n["step_type"] in {"physical", "hybrid"}:
        live_place_candidates = _live_google_places_candidates(base_query, location, title)
        if live_place_candidates:
            candidates.extend(live_place_candidates)
            live_places_available = True

    swarmsync_available = False
    swarmsync_query = _agent_marketplace_query(n, title)
    swarmsync_candidates = _live_swarmsync_agent_candidates(swarmsync_query, title)
    if swarmsync_candidates:
        candidates.extend(swarmsync_candidates)
        swarmsync_available = True

    if n["step_type"] in {"physical", "hybrid"}:
        maps_query = base_query if location else f"{title} near me"
        candidates.append(
            _candidate(
                cid="local-map-search",
                title=f"Local options for {title}",
                candidate_type="local_discovery",
                source_name="Google Maps search",
                source_type="maps_query",
                source_url=_query_url("google_maps", maps_query),
                confidence=confidence_base,
                rationale="Physical or hybrid IOO nodes need nearby, reviewable options before the user commits.",
                next_label="Open map results and shortlist 2-3 realistic options",
                metadata={"location_used": location, "query": maps_query},
            )
        )

    if tags & {"event", "events", "concerts", "festivals", "markets", "culture"} or n["type"] == "experience":
        event_query = f"{title} {location or 'online'}"
        candidates.append(
            _candidate(
                cid="event-listing-search",
                title=f"Event listings for {title}",
                candidate_type="event_listing",
                source_name="Eventbrite discovery",
                source_type="event_query",
                source_url=_query_url("eventbrite", event_query),
                confidence=0.62 if location else 0.5,
                rationale="Experience-oriented nodes benefit from current listings, dates, prices, and availability signals.",
                next_label="Check dates, price, distance, and availability without buying tickets yet",
                metadata={"location_used": location, "query": event_query},
            )
        )

    if tags & {"volunteering", "volunteer", "service", "community"} or "volunteer" in title_lower:
        volunteer_query = f"{title} {location or ''}".strip()
        candidates.append(
            _candidate(
                cid="volunteer-opportunity-search",
                title=f"Volunteer opportunities for {title}",
                candidate_type="volunteer_opportunity",
                source_name="VolunteerMatch search",
                source_type="opportunity_query",
                source_url=_query_url("volunteermatch", volunteer_query),
                confidence=0.68 if location else 0.58,
                rationale="Service nodes should surface real organisations, role requirements, and application paths.",
                next_label="Open opportunities and save one that fits skills, timing, and mission",
                metadata={"location_used": location, "query": volunteer_query},
            )
        )

    if tags & {"career", "impact-jobs", "job", "income", "work-abroad"} or "job" in title_lower:
        job_query = f"{title} impact mission aligned"
        candidates.append(
            _candidate(
                cid="mission-job-search",
                title=f"Mission-aligned roles for {title}",
                candidate_type="job_listing",
                source_name="LinkedIn Jobs search",
                source_type="job_query",
                source_url=_query_url("linkedin_jobs", job_query),
                confidence=0.6,
                rationale="Career execution needs concrete role listings and application pages, not generic advice.",
                next_label="Review role requirements and pick one application target",
                metadata={"query": job_query},
            )
        )
        candidates.append(
            _candidate(
                cid="impact-job-search",
                title=f"Impact-sector roles for {title}",
                candidate_type="job_listing",
                source_name="Idealist search",
                source_type="job_query",
                source_url=_query_url("idealist", job_query),
                confidence=0.57,
                rationale="Idealist-style listings are a better fit for mission and service-oriented career nodes.",
                next_label="Compare impact roles and note one realistic next application step",
                metadata={"query": job_query},
            )
        )

    if tags & {"open-source", "github", "dao", "contribution"} or "open-source" in title_lower:
        repo_query = f"{title} good first issue help wanted"
        candidates.append(
            _candidate(
                cid="open-source-repo-search",
                title=f"Open-source projects for {title}",
                candidate_type="repository_search",
                source_name="GitHub repository search",
                source_type="code_host_query",
                source_url=_query_url("github", repo_query),
                confidence=0.64,
                rationale="Contribution nodes need real repositories with visible issues, maintainers, and contribution paths.",
                next_label="Open repositories and choose one issue to inspect before committing",
                metadata={"query": repo_query, "preferred_labels": ["good first issue", "help wanted"]},
            )
        )

    tutorial_query = f"{title} beginner guide practical steps"
    candidates.append(
        _candidate(
            cid="practical-guide-search",
            title=f"Practical guide for {title}",
            candidate_type="learning_or_prep_path",
            source_name="Web search",
            source_type="web_query",
            source_url=_query_url("google", tutorial_query),
            confidence=0.52,
            rationale="A reversible prep path gives the user a safe first action even when no live provider is selected yet.",
            next_label="Open a guide and extract the first concrete prep step",
            metadata={"query": tutorial_query, "required_skills": n["requires_skills"]},
        )
    )

    if not candidates:
        candidates.append(
            _candidate(
                cid="manual-clarification-fallback",
                title=f"Clarify execution path for {title}",
                candidate_type="fallback_clarification",
                source_name="IOO node context",
                source_type="internal_context",
                source_url=None,
                confidence=0.35,
                rationale="No safe search surface could be inferred from the node metadata, so Ora should ask for one constraint before searching.",
                next_label="Ask the user for preferred location, budget, timing, or format",
                metadata={"available_tags": n["tags"], "step_type": n["step_type"]},
            )
        )

    sorted_candidates = sorted(candidates, key=lambda item: item.confidence, reverse=True)[:5]
    live_integrations = {
        "web_search": "not_configured",
        "google_places": "available" if live_places_available else "not_configured_or_no_results",
        "swarmsync": "available" if swarmsync_available else "not_applicable_or_no_results",
        "aventi": "not_configured",
        "eviva": "not_configured",
    }
    live_results_available = any(
        candidate.source.type.startswith("live_") for candidate in sorted_candidates
    )
    fallback_used = not live_results_available

    return {
        "role": "SearchAgent",
        "status": "candidates_ready" if live_results_available else "fallback_ready",
        "mode": "live_search" if live_results_available else "query_plan",
        "intent": intent if intent in {"do_now", "do_later"} else "do_now",
        "summary": "Prepared reversible discovery candidates; no booking, purchase, message, or application was performed.",
        "integrations": live_integrations,
        "fallback": {
            "used": fallback_used,
            "reason": (
                "Live provider results are included."
                if live_results_available
                else "Live providers returned no results or are not configured; returning provider-ready query links and prep paths instead."
            ),
            "user_safe": True,
        },
        "candidates": [asdict(candidate) for candidate in sorted_candidates],
        "selection_guidance": {
            "rank_by": ["fit_with_user_constraints", "trust_signal_strength", "low_friction_first_action", "fulfilment_potential"],
            "avoid": ["booking_without_confirmation", "purchase_without_confirmation", "sending_messages_without_confirmation"],
        },
    }


__all__ = [
    "CandidateNextAction",
    "CandidateSource",
    "SearchCandidate",
    "build_search_agent_payload",
]
