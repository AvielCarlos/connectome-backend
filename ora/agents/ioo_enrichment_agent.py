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
    CATEGORIES = [
        "health/fitness", "mental wellness", "relationships", "career",
        "finance", "creativity", "spirituality", "learning", "adventure", "community",
    ]

    QUERY_TEMPLATES = [
        "complete step-by-step guide to {goal}",
        "what do you need to achieve {goal}",
        "science-backed pathway to {goal}",
        "evidence based steps habits metrics for {goal}",
    ]

    def __init__(self, openai_client=None):
        self.openai = openai_client

    def pick_categories(self, count: int = 3) -> List[str]:
        # Deterministic daily rotation, with slight spread across the list.
        start = int(hashlib.sha256(str(date.today()).encode()).hexdigest(), 16) % len(self.CATEGORIES)
        return [self.CATEGORIES[(start + i) % len(self.CATEGORIES)] for i in range(count)]

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
            "health/fitness": [
                ("Schedule 3-4 movement sessions per week", "Create recurring training blocks and start with manageable intensity.", "physical", ["fitness", "habit"]),
                ("Track daily protein intake for 3 days", "Measure current protein intake before changing the diet.", "hybrid", ["nutrition", "tracking"]),
                ("Sleep 7-9 hours for recovery", "Protect sleep as the baseline for energy, mood, and adaptation.", "physical", ["sleep", "recovery"]),
            ],
            "mental wellness": [
                ("Complete a 5-minute daily mood check-in", "Track mood and triggers for one week.", "digital", ["mental wellness", "tracking"]),
                ("Try a guided breathing exercise", "Use a short breath practice to downshift the nervous system.", "digital", ["breathing", "calm"]),
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
Prefer clear prerequisites, habits, metrics, physical actions, and digital actions. Avoid vague motivation.

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
        categories = categories or self.pick_categories(3)
        proposed = promoted = skipped = 0
        for category in categories:
            results: List[Dict[str, str]] = []
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
                    step.get("domain") or "iVive",
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


_enrichment_agent: Optional[IOOEnrichmentAgent] = None


def get_ioo_enrichment_agent(openai_client=None) -> IOOEnrichmentAgent:
    global _enrichment_agent
    if _enrichment_agent is None:
        _enrichment_agent = IOOEnrichmentAgent(openai_client)
    return _enrichment_agent
