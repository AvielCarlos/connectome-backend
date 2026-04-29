"""
Integral Synthesis Agent — Ora's four-quadrant knowledge synthesis layer.

Synthesises Connectome/Ora knowledge from the public website, web/app surfaces,
Drive strategy documents, and the agent memory bus. It detects meaningful
conflicts and escalates only high-severity conflicts to Avi.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from core.database import fetch
from ora.agents.agent_memory import AgentInsight, agent_memory_bus

logger = logging.getLogger(__name__)

AVI_TELEGRAM_CHAT_ID = 5716959016
ATDAO_URLS = [
    "https://atdao.org",
    "https://atdao.org/about",
    "https://atdao.org/manifesto",
]
CONNECTOME_WEB_REPO = "AvielCarlos/connectome-web"
RAW_WEB_CANDIDATES = [
    "README.md",
    "src/app/page.tsx",
    "src/app/layout.tsx",
    "src/pages/index.tsx",
    "app/page.tsx",
    "components/Hero.tsx",
    "src/components/Hero.tsx",
]
CONTEXT_PATH = Path(__file__).resolve().parents[1] / "data" / "integral_context.json"


@dataclass
class KnowledgeConflict:
    topic: str
    source_a: str
    version_a: str
    source_b: str
    version_b: str
    severity: str
    question_for_avi: str

    @property
    def id(self) -> str:
        seed = f"{self.topic}|{self.source_a}|{self.version_a}|{self.source_b}|{self.version_b}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["id"] = self.id
        return data


class IntegralSynthesisAgent:
    """Builds Ora's integral world-model across I / IT / WE / ITS quadrants."""

    name = "integral_synthesis"

    def __init__(self, openai_client: Any = None) -> None:
        self._openai = openai_client
        self._telegram_token: Optional[str] = None

    async def run(self) -> Dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        static_context = self._load_static_context()

        website_task = asyncio.create_task(self.fetch_website_content())
        web_repo_task = asyncio.create_task(self.fetch_connectome_web_content())
        app_task = asyncio.create_task(self.read_app_content())
        drive_task = asyncio.create_task(self.read_drive_docs())
        insights_task = asyncio.create_task(self.read_agent_insights())

        website, web_repo, app, drive_docs, insights = await asyncio.gather(
            website_task, web_repo_task, app_task, drive_task, insights_task
        )

        sources = {
            "static_context": static_context,
            "website": website,
            "connectome_web": web_repo,
            "app_content": app,
            "drive_docs": drive_docs,
            "agent_insights": [i.to_dict() for i in insights],
        }
        quadrants = self.synthesise_quadrants(sources)
        conflicts = self.detect_conflicts(sources)

        published_ids = await self.publish_synthesis(quadrants, sources, conflicts)
        escalated = []
        for conflict in conflicts:
            if conflict.severity.lower() == "high":
                sent = await self.escalate_conflict(conflict)
                escalated.append({"conflict_id": conflict.id, "sent": sent})

        return {
            "agent": self.name,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "source_counts": {
                "website": len(website),
                "connectome_web": len(web_repo),
                "app_content": sum(len(v) for v in app.values()),
                "drive_docs": len(drive_docs),
                "agent_insights": len(insights),
            },
            "quadrants": quadrants,
            "conflicts": [c.to_dict() for c in conflicts],
            "published_insight_ids": published_ids,
            "escalations": escalated,
        }

    async def fetch_website_content(self) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            for url in ATDAO_URLS:
                try:
                    resp = await client.get(url)
                    if resp.status_code >= 400:
                        continue
                    text = _clean_text(resp.text)
                    if text:
                        results.append({"source": url, "content": text[:8000]})
                except Exception as exc:
                    logger.debug("IntegralSynthesisAgent website fetch failed for %s: %s", url, exc)
        return results

    async def fetch_connectome_web_content(self) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            for branch in ("main", "master"):
                for path in RAW_WEB_CANDIDATES:
                    raw_url = f"https://raw.githubusercontent.com/{CONNECTOME_WEB_REPO}/{branch}/{path}"
                    try:
                        resp = await client.get(raw_url)
                        if resp.status_code == 404:
                            continue
                        if resp.status_code >= 400:
                            continue
                        text = _extract_human_text(resp.text)
                        if text:
                            results.append({"source": f"{CONNECTOME_WEB_REPO}/{path}", "content": text[:8000]})
                    except Exception as exc:
                        logger.debug("IntegralSynthesisAgent web repo fetch failed for %s: %s", path, exc)
                if results:
                    break
        return results

    async def read_app_content(self) -> Dict[str, List[Dict[str, Any]]]:
        data: Dict[str, List[Dict[str, Any]]] = {"screen_specs": [], "ora_surfaces": []}
        try:
            rows = await fetch(
                """
                SELECT id, agent_type, spec, global_rating, created_at
                FROM screen_specs
                ORDER BY created_at DESC
                LIMIT 75
                """
            )
            data["screen_specs"] = [_row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.debug("IntegralSynthesisAgent screen_specs read failed: %s", exc)
        try:
            rows = await fetch(
                """
                SELECT id, surface_type, title, slug, spec, status, created_at, updated_at
                FROM ora_surfaces
                ORDER BY updated_at DESC NULLS LAST, created_at DESC
                LIMIT 75
                """
            )
            data["ora_surfaces"] = [_row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.debug("IntegralSynthesisAgent ora_surfaces read failed: %s", exc)
        return data

    async def read_drive_docs(self, max_docs: int = 12) -> List[Dict[str, str]]:
        files = await _run_json_command([
            "gog", "drive", "list", "--account", "ora.intelligence.ai@gmail.com", "--client", "ora"
        ])
        docs: List[Dict[str, str]] = []
        if not isinstance(files, list):
            return docs

        strategy_re = re.compile(r"(strategy|vision|mission|ora|connectome|ascension|dao|product)", re.I)
        candidates = [f for f in files if strategy_re.search(str(f.get("name") or f.get("title") or ""))]
        if not candidates:
            candidates = files[:max_docs]

        for item in candidates[:max_docs]:
            doc_id = str(item.get("id") or item.get("fileId") or "")
            name = str(item.get("name") or item.get("title") or doc_id or "untitled")
            if not doc_id:
                continue
            content = await _run_text_command([
                "gog", "docs", "cat", doc_id, "--account", "ora.intelligence.ai@gmail.com", "--client", "ora"
            ])
            if content:
                docs.append({"source": name, "drive_id": doc_id, "content": content[:10000]})
        return docs

    async def read_agent_insights(self) -> List[AgentInsight]:
        return await agent_memory_bus.read_all_recent(hours=168)

    def synthesise_quadrants(self, sources: Dict[str, Any]) -> Dict[str, Any]:
        corpus = _source_corpus(sources)
        mission_terms = _snippets_for(corpus, ["mission", "human flourishing", "fulfilment", "singularity"])
        product_terms = _snippets_for(corpus, ["screen", "surface", "feature", "checkout", "subscription", "api"])
        community_terms = _snippets_for(corpus, ["community", "dao", "ascension", "steward", "culture"])
        architecture_terms = _snippets_for(corpus, ["architecture", "backend", "agent", "database", "revenue", "stripe"])
        return {
            "I": {
                "label": "individual interior",
                "focus": "intentions, values, personal meaning, goals",
                "synthesis": mission_terms[:8],
            },
            "IT": {
                "label": "individual exterior",
                "focus": "behaviour, metrics, feature engagement, observable progress",
                "synthesis": product_terms[:8],
            },
            "WE": {
                "label": "collective interior",
                "focus": "community culture, shared values, mission resonance",
                "synthesis": community_terms[:8],
            },
            "ITS": {
                "label": "collective exterior",
                "focus": "technical architecture, ecosystems, revenue systems",
                "synthesis": architecture_terms[:8],
            },
        }

    def detect_conflicts(self, sources: Dict[str, Any]) -> List[KnowledgeConflict]:
        claims_by_topic: Dict[str, List[Dict[str, str]]] = {}
        for source_name, source_items in _iter_source_items(sources):
            for topic, claim in _extract_claims(source_items).items():
                claims_by_topic.setdefault(topic, []).append({"source": source_name, "claim": claim})

        conflicts: List[KnowledgeConflict] = []
        for topic, claims in claims_by_topic.items():
            unique: List[Dict[str, str]] = []
            for claim in claims:
                if all(_similarity(claim["claim"], existing["claim"]) < 0.72 for existing in unique):
                    unique.append(claim)
            if len(unique) < 2:
                continue
            a, b = unique[0], unique[1]
            severity = _conflict_severity(topic, a["source"], b["source"], a["claim"], b["claim"])
            conflicts.append(KnowledgeConflict(
                topic=topic,
                source_a=a["source"],
                version_a=a["claim"][:600],
                source_b=b["source"],
                version_b=b["claim"][:600],
                severity=severity,
                question_for_avi=(
                    f"Which {topic} version produces better outcomes for users and the mission — "
                    "more efficient, effective, and aligned with human flourishing?"
                ),
            ))
        return conflicts[:20]

    async def publish_synthesis(
        self,
        quadrants: Dict[str, Any],
        sources: Dict[str, Any],
        conflicts: List[KnowledgeConflict],
    ) -> List[str]:
        ids: List[str] = []
        summary = {
            "quadrants": quadrants,
            "source_counts": {
                "website": len(sources.get("website") or []),
                "connectome_web": len(sources.get("connectome_web") or []),
                "drive_docs": len(sources.get("drive_docs") or []),
                "agent_insights": len(sources.get("agent_insights") or []),
            },
            "conflict_count": len(conflicts),
            "high_conflict_count": sum(1 for c in conflicts if c.severity == "high"),
        }
        insight_id = await agent_memory_bus.publish(AgentInsight(
            source_agent=self.name,
            domain="integral_knowledge",
            insight_type="four_quadrant_synthesis",
            content=json.dumps(summary, ensure_ascii=False),
            confidence=0.86,
            target_agents=[],
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        ))
        if insight_id:
            ids.append(insight_id)

        for conflict in conflicts:
            cid = await agent_memory_bus.publish(AgentInsight(
                source_agent=self.name,
                domain="integral_knowledge",
                insight_type="knowledge_conflict",
                content=json.dumps(conflict.to_dict(), ensure_ascii=False),
                confidence=0.7 if conflict.severity == "low" else 0.82,
                action_required=conflict.severity == "high",
                target_agents=["strategy", "cpo", "cto"] if conflict.severity != "low" else [],
                expires_at=datetime.now(timezone.utc) + timedelta(days=14),
            ))
            if cid:
                ids.append(cid)
        return ids

    async def escalate_conflict(self, conflict: KnowledgeConflict) -> bool:
        token = self._get_telegram_token()
        if not token:
            logger.warning("IntegralSynthesisAgent: no Telegram token; cannot escalate conflict %s", conflict.id)
            return False
        message = (
            "⚠️ Ora knowledge conflict\n\n"
            f"Topic: {conflict.topic}\n"
            "Truth principle: choose the version that produces the better experience, "
            "product, or service for real people.\n\n"
            f"Version A ({conflict.source_a}) implies in practice:\n{conflict.version_a[:500]}\n\n"
            f"Version B ({conflict.source_b}) implies in practice:\n{conflict.version_b[:500]}\n\n"
            "Question: Which of these produces better outcomes for users?\n"
            f"Conflict ID: {conflict.id}"
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": AVI_TELEGRAM_CHAT_ID, "text": message},
                )
                return resp.status_code < 400
        except Exception as exc:
            logger.warning("IntegralSynthesisAgent: Telegram escalation failed: %s", exc)
            return False

    def _get_telegram_token(self) -> str:
        if self._telegram_token is not None:
            return self._telegram_token
        token = os.environ.get("ORA_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
        if not token:
            for path in (
                "/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt",
                "/run/secrets/telegram-bot-token.txt",
            ):
                try:
                    token = Path(path).read_text().strip()
                    if token:
                        break
                except Exception:
                    pass
        self._telegram_token = token
        return token

    def _load_static_context(self) -> Dict[str, Any]:
        try:
            return json.loads(CONTEXT_PATH.read_text())
        except Exception as exc:
            logger.debug("IntegralSynthesisAgent static context unavailable: %s", exc)
            return {}


async def run_integral_synthesis(openai_client: Any = None) -> Dict[str, Any]:
    return await IntegralSynthesisAgent(openai_client=openai_client).run()


async def _run_json_command(cmd: List[str]) -> Any:
    text = await _run_text_command(cmd)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        # gog output formats vary; tolerate line-delimited JSON.
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        return rows or None


async def _run_text_command(cmd: List[str]) -> str:
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )
        if proc.returncode != 0:
            logger.debug("Command failed (%s): %s", " ".join(cmd), proc.stderr[:500])
            return ""
        return (proc.stdout or "").strip()
    except FileNotFoundError:
        logger.debug("Command unavailable: %s", cmd[0])
        return ""
    except Exception as exc:
        logger.debug("Command failed (%s): %s", " ".join(cmd), exc)
        return ""


def _clean_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return _normalise_ws(text)


def _extract_human_text(raw: str) -> str:
    raw = re.sub(r"import .*?;|export .*?;", " ", raw)
    quoted = re.findall(r"[\"'`]([^\"'`]{20,300})[\"'`]", raw)
    text = "\n".join(quoted) if quoted else raw
    return _normalise_ws(text)


def _normalise_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


def _source_corpus(sources: Dict[str, Any]) -> List[str]:
    corpus: List[str] = []
    for _name, item in _iter_source_items(sources):
        corpus.append(str(item))
    return corpus


def _iter_source_items(sources: Dict[str, Any]) -> Iterable[tuple[str, Any]]:
    for source_name, source_data in sources.items():
        if isinstance(source_data, list):
            for item in source_data:
                yield source_name, item
        elif isinstance(source_data, dict):
            if source_name == "app_content":
                for table, rows in source_data.items():
                    for row in rows:
                        yield table, row
            else:
                yield source_name, source_data
        elif source_data:
            yield source_name, source_data


def _snippets_for(corpus: List[str], needles: List[str]) -> List[str]:
    snippets: List[str] = []
    for text in corpus:
        lowered = text.lower()
        if not any(n.lower() in lowered for n in needles):
            continue
        clean = _normalise_ws(text)
        snippets.append(clean[:450])
    return snippets


def _extract_claims(item: Any) -> Dict[str, str]:
    text = _normalise_ws(json.dumps(item, ensure_ascii=False, default=str) if not isinstance(item, str) else item)
    claims: Dict[str, str] = {}
    topics = {
        "mission": ["mission", "human flourishing", "fulfilment", "singularity"],
        "product_name": ["connectome", "ido", "ora"],
        "ora_role": ["ora role", "heart of connectome", "ai at the heart", "multi-agent"],
        "revenue_model": ["subscription", "stripe", "revenue", "membership", "pricing"],
        "community_model": ["community", "dao", "steward", "ascension"],
    }
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for topic, needles in topics.items():
        matches = [s for s in sentences if any(n in s.lower() for n in needles)]
        if matches:
            claims[topic] = _normalise_ws(" ".join(matches[:3]))[:900]
    return claims


def _similarity(a: str, b: str) -> float:
    aw = set(re.findall(r"[a-z0-9]+", a.lower()))
    bw = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(1, len(aw | bw))


def _conflict_severity(topic: str, source_a: str, source_b: str, claim_a: str, claim_b: str) -> str:
    high_topics = {"mission", "product_name", "ora_role"}
    # Utility-based epistemology: severity comes from likely user/mission impact,
    # not from authority or document rank.
    if topic in high_topics:
        return "high"
    if topic in {"revenue_model", "community_model"}:
        return "medium"
    if _similarity(claim_a, claim_b) < 0.35:
        return "medium"
    return "low"
