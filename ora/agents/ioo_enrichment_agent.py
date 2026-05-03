"""
IOOEnrichmentAgent — daily research loop for growing the IOO graph.

The agent researches evidence-backed pathways across human goal categories,
turns them into candidate IOO nodes, stores proposals for review, and
auto-promotes high-confidence, clear/actionable steps into the live graph.
"""

import hashlib
import json
import logging
import random
import re
from datetime import date
from typing import Any, Dict, List, Optional

from core.config import settings
from core.database import execute, fetch, fetchrow
from ora.agents.ioo_graph_agent import get_graph_agent

logger = logging.getLogger(__name__)


class IOOEnrichmentAgent:
    CATEGORIES_BY_DOMAIN = {
        "iVive": [
            "iVive physical mental spiritual creative financial self-growth",
            "iVive rest recovery sleep nervous system restoration",
            "iVive rest Sabbath digital detox low-stimulation renewal",
            "iVive rest active recovery after burnout or overwork",
            "health/fitness",
            "mental wellness",
            "finance",
            "learning",
        ],
        "Eviva": [
            "Eviva global meaningful work opportunities",
            "Eviva volunteering roles with NGOs and social enterprises",
            "Eviva open-source projects looking for contributors",
            "Eviva impact-driven jobs remote 2026",
            "Eviva meaningful volunteering opportunities 2026",
            "career",
            "community",
        ],
        "Aventi": [
            "Aventi fun adventure events dating travel friendship",
            "Aventi local micro-adventures and friendship opportunities",
            "Aventi creative dates, social hobbies, and travel readiness",
        ],
    }
    CATEGORIES = [category for categories in CATEGORIES_BY_DOMAIN.values() for category in categories]

    QUERY_TEMPLATES = [
        "complete step-by-step guide to {goal}",
        "what do you need to achieve {goal}",
        "science-backed pathway to {goal}",
        "evidence based steps habits metrics for {goal}",
    ]

    EVIVA_SOURCE_QUERIES = [
        "site:volunteermatch.org volunteering opportunities skills remote 2026",
        "site:idealist.org remote impact jobs volunteering 2026",
        "site:80000hours.org impact careers job board",
        "GitHub trending open source projects looking for contributors good first issue",
        "meaningful volunteering opportunities 2026",
        "impact-driven jobs remote 2026",
    ]

    def __init__(self, openai_client=None):
        self.openai = openai_client

    def pick_categories(self, count: int = 4) -> List[str]:
        """Pick a daily balanced slice across the three lived-action domains.

        Rest is an iVive aspect, not a fourth domain. The daily cycle still
        keeps recovery alive by pairing one iVive rest/recovery category with
        iVive capacity, Eviva contribution, and Aventi aliveness.
        """
        seed = int(hashlib.sha256(str(date.today()).encode()).hexdigest(), 16)
        ivive_categories = self.CATEGORIES_BY_DOMAIN["iVive"]
        rest_categories = [c for c in ivive_categories if "rest" in c.lower() or "sleep" in c.lower() or "recovery" in c.lower()]
        capacity_categories = [c for c in ivive_categories if c not in rest_categories]
        picked: List[str] = [
            capacity_categories[seed % len(capacity_categories)],
            self.CATEGORIES_BY_DOMAIN["Eviva"][(seed + 1) % len(self.CATEGORIES_BY_DOMAIN["Eviva"])],
            self.CATEGORIES_BY_DOMAIN["Aventi"][(seed + 2) % len(self.CATEGORIES_BY_DOMAIN["Aventi"])],
        ]
        if count > len(picked) and rest_categories:
            picked.append(rest_categories[(seed + 3) % len(rest_categories)])
        if count <= len(picked):
            return picked[:count]
        remaining = [category for category in self.CATEGORIES if category not in picked]
        start = seed % len(remaining) if remaining else 0
        while len(picked) < count and remaining:
            picked.append(remaining[(start + len(picked)) % len(remaining)])
        return picked

    async def _search_web(self, query: str) -> List[Dict[str, str]]:
        """Lightweight web search using DuckDuckGo HTML as a no-key fallback."""
        try:
            import html
            import httpx
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                r = await client.get("https://duckduckgo.com/html/", params={"q": query})
                r.raise_for_status()
            results = []
            blocks = re.findall(r'<div class="result.*?</div>\s*</div>', r.text, flags=re.S)[:5]
            for block in blocks:
                link = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
                snippet = re.search(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', block, flags=re.S)
                if link:
                    title = re.sub(r"<.*?>", " ", link.group(2))
                    snip = re.sub(r"<.*?>", " ", snippet.group(1)) if snippet else ""
                    results.append({
                        "title": html.unescape(" ".join(title.split())),
                        "url": html.unescape(link.group(1)),
                        "snippet": html.unescape(" ".join(snip.split())),
                    })
            return results
        except Exception as e:
            logger.debug(f"IOO enrichment search failed for {query}: {e}")
            return []

    def _fallback_steps(self, category: str) -> List[Dict[str, Any]]:
        templates = {
            "Eviva global meaningful work opportunities": [
                ("Find a volunteering role that matches your skills", "Search global volunteer listings and shortlist roles where your abilities can create real value.", "hybrid", ["Eviva", "volunteering", "skills"]),
                ("Apply to one mission-aligned job this month", "Find one impact role aligned with your values and submit a serious application.", "hybrid", ["Eviva", "impact-jobs", "career"]),
                ("Contribute to an open-source project", "Find a project with good-first-issues and make one useful contribution.", "digital", ["Eviva", "open-source", "github"]),
            ],
            "Eviva volunteering roles with NGOs and social enterprises": [
                ("Find a volunteering role that matches your skills", "Use VolunteerMatch, Idealist, or local NGO directories to identify one fitting role.", "hybrid", ["Eviva", "volunteering", "NGO"]),
                ("Join a local community initiative", "Find a real local initiative and attend one meeting or event.", "physical", ["Eviva", "community", "local"]),
            ],
            "Eviva open-source projects looking for contributors": [
                ("Contribute to an open-source project", "Find a project with contributor guidance and submit a useful issue, doc, or PR.", "digital", ["Eviva", "open-source", "github"]),
            ],
            "Eviva impact-driven jobs remote 2026": [
                ("Apply to one mission-aligned job this month", "Build a short list of remote impact roles and apply to one high-fit opportunity.", "hybrid", ["Eviva", "impact-jobs", "remote"]),
            ],
            "Eviva meaningful volunteering opportunities 2026": [
                ("Find a volunteering role that matches your skills", "Search current global and local volunteer roles, then pick one worthy next step.", "hybrid", ["Eviva", "volunteering", "purpose"]),
            ],
            "health/fitness": [
                ("Schedule 3-4 movement sessions per week", "Create recurring training blocks and start with manageable intensity.", "physical", ["fitness", "habit"]),
                ("Track daily protein intake for 3 days", "Measure current protein intake before changing the diet.", "hybrid", ["nutrition", "tracking"]),
                ("Sleep 7-9 hours for recovery", "Protect sleep as the baseline for energy, mood, and adaptation.", "physical", ["sleep", "recovery"]),
            ],
            "mental wellness": [
                ("Complete a 5-minute daily mood check-in", "Track mood and triggers for one week.", "digital", ["mental wellness", "tracking"]),
                ("Try a guided breathing exercise", "Use a short breath practice to downshift the nervous system.", "digital", ["breathing", "calm"]),
            ],
            "Aventi local micro-adventures and friendship opportunities": [
                ("Invite one person to a 60-minute local micro-adventure", "Pick a specific walk, market, gallery, or café route and send one low-pressure invitation.", "physical", ["Aventi", "friendship", "micro-adventure"]),
                ("Build a three-option weekend adventure shortlist", "Choose three realistic nearby experiences with time, cost, transit, and social fit noted.", "hybrid", ["Aventi", "planning", "weekend"]),
            ],
            "Aventi creative dates, social hobbies, and travel readiness": [
                ("Try one social hobby taster session", "Book or attend one beginner-friendly class, meetup, or creative group where conversation happens naturally.", "physical", ["Aventi", "social", "hobby"]),
                ("Create a simple travel-readiness checklist", "List passport, budget, dates, destination constraints, and one first booking/research action.", "digital", ["Aventi", "travel", "readiness"]),
            ],
            "iVive rest recovery sleep nervous system restoration": [
                ("Choose tonight's shutdown time and protect it", "Set a specific lights-out target, remove one sleep blocker, and start winding down 45 minutes before bed.", "physical", ["iVive", "rest", "sleep", "recovery"]),
                ("Do a 10-minute nervous-system reset", "Use breath, stretching, yoga nidra, or a slow walk to shift out of stress before choosing the next action.", "physical", ["iVive", "rest", "nervous-system", "reset"]),
            ],
            "iVive rest Sabbath digital detox low-stimulation renewal": [
                ("Schedule a two-hour low-stimulation block", "Pick a calendar window with no feeds, messages, or productivity pressure; choose one restorative activity.", "hybrid", ["iVive", "rest", "digital-detox", "renewal"]),
                ("Prepare a phone-free recovery menu", "Write three offline options—walk, bath, reading, prayer, music, cooking—so rest has an easy next step.", "digital", ["iVive", "rest", "offline", "renewal"]),
            ],
            "iVive rest active recovery after burnout or overwork": [
                ("Replace one hard task with active recovery", "Choose one non-urgent task to defer and do a gentle body-based recovery action instead.", "physical", ["iVive", "rest", "burnout", "active-recovery"]),
                ("Name the next minimum viable obligation", "Reduce today's pressure to the one obligation that actually matters, then leave space for recovery.", "digital", ["iVive", "rest", "prioritisation", "burnout"]),
            ],
            "finance": [
                ("List all recurring monthly expenses", "Create visibility before making any financial changes.", "digital", ["finance", "budget"]),
                ("Set up an automatic savings transfer", "Make saving happen by default each payday.", "digital", ["finance", "automation"]),
            ],
        }
        base = templates.get(category, [
            (f"Define one measurable {category} outcome", "Turn the broad goal into a concrete target with a date and metric.", "digital", [category, "planning"]),
            (f"Complete one small real-world {category} action", "Take a small physical or social step that makes the goal real.", "physical", [category, "action"]),
        ])
        return [
            {"title": t, "description": d, "step_type": st, "tags": tags, "confidence": 0.72}
            for t, d, st, tags in base
        ]

    async def _extract_steps(self, category: str, results: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        if not getattr(settings, "OPENAI_API_KEY", ""):
            return self._fallback_steps(category)
        try:
            import httpx
            source_text = "\n".join(f"- {r['title']}: {r.get('snippet','')} ({r.get('url','')})" for r in results[:8])
            prompt = f"""
Extract actionable IOO graph nodes for the goal category: {category}.
Use the search evidence below. Return JSON only: {{"steps": [{{"title": str, "description": str, "step_type": "digital|physical|hybrid", "tags": [str], "confidence": 0-1, "source_url": str}}]}}
Prefer clear opportunities, prerequisites, habits, metrics, physical actions, and digital actions. For Eviva categories, extract concrete jobs, volunteering roles, open-source contribution paths, community initiatives, impact startups, and prerequisites that may need iVive capability-building. For Aventi, prefer specific lived experiences, social invitations, local adventures, dating/friendship pathways, and travel-readiness bridges. For iVive rest categories, prefer recovery, sleep, nervous-system, Sabbath/digital-detox, and burnout-repair nodes that make action sustainable; keep their domain as iVive and mark rest/recovery in tags. Avoid vague motivation, generic content, and broad labels like "exercise more" unless the title itself contains a concrete next action.

Evidence:
{source_text}
"""
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    json={
                        "model": "gpt-4o-mini",
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                    },
                )
                r.raise_for_status()
            data = json.loads(r.json()["choices"][0]["message"]["content"])
            return data.get("steps") or self._fallback_steps(category)
        except Exception as e:
            logger.warning(f"IOO enrichment extraction failed for {category}: {e}")
            return self._fallback_steps(category)

    async def _similar_exists(self, title: str) -> bool:
        exact = await fetchrow(
            "SELECT id FROM ioo_nodes WHERE lower(title) = lower($1) OR title ILIKE $2 LIMIT 1",
            title, f"%{title[:40]}%",
        )
        return bool(exact)

    async def _promote_proposal(self, proposal_id: str) -> Optional[str]:
        row = await fetchrow("SELECT * FROM ioo_node_proposals WHERE id = $1::uuid", proposal_id)
        if not row:
            return None
        node = await fetchrow(
            """
            INSERT INTO ioo_nodes
                (type, title, description, tags, domain, step_type, goal_category, difficulty_level)
            VALUES ('activity',$1,$2,$3,$4,$5,$6,5)
            RETURNING id
            """,
            row["title"], row["description"], row["tags"] or [], row["domain"],
            row["step_type"] or "hybrid", row["goal_category"],
        )
        await execute("UPDATE ioo_node_proposals SET status = 'approved' WHERE id = $1::uuid", proposal_id)
        try:
            await get_graph_agent().embed_all_nodes()
        except Exception:
            pass
        return str(node["id"]) if node else None

    async def run_daily(self, categories: Optional[List[str]] = None) -> Dict[str, Any]:
        # Daily enrichment should touch all three lived-action domains, with
        # rest/recovery treated as a continuously tested iVive aspect.
        categories = categories or self.pick_categories(4)
        proposed = promoted = skipped = 0
        for category in categories:
            results: List[Dict[str, str]] = []
            if category.startswith("Eviva"):
                for q in self.EVIVA_SOURCE_QUERIES:
                    results.extend(await self._search_web(q))
            for tmpl in self.QUERY_TEMPLATES:
                results.extend(await self._search_web(tmpl.format(goal=category)))
            steps = await self._extract_steps(category, results)
            source_url = next((r.get("url") for r in results if r.get("url")), None)
            for step in steps[:8]:
                title = (step.get("title") or "").strip()
                if len(title) < 8 or await self._similar_exists(title):
                    skipped += 1
                    continue
                confidence = float(step.get("confidence") or 0.5)
                status = "approved" if confidence >= 0.82 else "pending"
                row = await fetchrow(
                    """
                    INSERT INTO ioo_node_proposals
                        (title, description, goal_category, step_type, domain, tags, source_url, confidence, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    RETURNING id
                    """,
                    title,
                    step.get("description"),
                    category,
                    step.get("step_type") if step.get("step_type") in ("digital", "physical", "hybrid") else "hybrid",
                    step.get("domain") or self._infer_domain(category),
                    step.get("tags") or [category],
                    step.get("source_url") or source_url,
                    confidence,
                    status,
                )
                proposed += 1
                if status == "approved":
                    if await self._promote_proposal(str(row["id"])):
                        promoted += 1
        return {"ok": True, "categories": categories, "proposed": proposed, "promoted": promoted, "skipped": skipped}

    def _infer_domain(self, category: str) -> str:
        lowered = category.lower()
        if category.startswith("Eviva") or "career" in lowered or "community" in lowered:
            return "Eviva"
        if category.startswith("Aventi") or "adventure" in lowered or "dating" in lowered or "travel" in lowered:
            return "Aventi"
        if "rest" in lowered or "sleep" in lowered or "recovery" in lowered or "burnout" in lowered:
            return "iVive"
        return "iVive"


_enrichment_agent: Optional[IOOEnrichmentAgent] = None


def get_ioo_enrichment_agent(openai_client=None) -> IOOEnrichmentAgent:
    global _enrichment_agent
    if _enrichment_agent is None:
        _enrichment_agent = IOOEnrichmentAgent(openai_client)
    return _enrichment_agent
