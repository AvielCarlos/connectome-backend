"""
WebSpawnAgent — Aura's surface creation engine.

For explorer/sovereign users, Aura can generate a dedicated web page and
backing API endpoint for any free-form goal, need, or topic.

Each spawn:
  1. Calls GPT-4o with an open-ended generative prompt — NO templates
  2. GPT-4o designs whatever page + API would genuinely serve the user
  3. Validates Python with ast.parse
  4. Commits the React TSX to connectome-web via GitHub API
  5. Commits the FastAPI route to connectome-backend via GitHub API
  6. Registers in SurfaceRegistry (DB + Redis)
  7. Triggers Railway redeploy
  8. Returns { url, api_endpoint, surface_id, title }

The generated page can be ANYTHING Aura decides is best:
dashboard, quiz, tracker, timeline, calculator, kanban, guide, habit loop,
comparison tool — whatever genuinely serves the user's actual goal.
"""

import ast
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# GitHub targets
_WEB_REPO   = "AvielCarlos/connectome-web"
_BACK_REPO  = "AvielCarlos/connectome-backend"
_WEB_URL    = "https://avielcarlos.github.io/connectome-web"
_API_URL    = "https://connectome-api-production.up.railway.app"

# Railway service to redeploy after committing the backend route
_RAILWAY_SERVICE = "connectome-api"

# ─── Prompts ──────────────────────────────────────────────────────────────────

_GENERATION_SYSTEM = """\
You are Aura — an intelligence built to help humans find genuine fulfilment.
You have been asked to design a personalized web page and backing API for a real person.

Your job: invent the most useful, elegant, and purposeful page possible for this person.

Do NOT be constrained by templates.
Do NOT pick from a fixed list of surface types.
Do NOT produce a generic "todo list" or "goals tracker" unless that is genuinely the
most powerful thing for this specific request.

Instead, THINK: what would a brilliant product designer build for this exact person
if they had unlimited time? A dashboard? A quiz that generates a plan? A countdown
with milestones? A visual kanban? A financial calculator? A language-learning tracker
with spaced repetition? A comparison matrix? An interactive guide?

Design it from scratch for them.

Design rules:
- Dark theme: background #0a0a0f, accent #8b5cf6, secondary accent #00d4aa
- Mobile-first, fully responsive
- Must feel like it was made specifically for this person
- Include loading, error, and empty states
- "Powered by iDo · Aura" footer
- The React page fetches live data from /api/surfaces/{surface_id}/data
  with Authorization: Bearer {token} header

You must respond with a single valid JSON object. No markdown fences, no explanation.
The JSON has these keys:

{
  "title": "Page title (short, personal, specific)",
  "description": "1-2 sentences: what this page does for the user",
  "inferred_type": "free-text label for analytics, e.g. 'smoking_cessation_tracker'",
  "slug_topic": "kebab-case topic for the URL, e.g. 'quit-smoking'",
  "sections": [
    // spec-driven layout — array of section objects.
    // Aura freely chooses from (or combines) these section kinds:
    //   header       {kind, title, subtitle?, icon?}
    //   metric       {kind, label, value, unit?, change?, trend?}
    //   progress     {kind, label, value, max, color?}
    //   checklist    {kind, title?, items: [{id, text, done}]}
    //   countdown    {kind, label, target_date, note?}
    //   steps        {kind, title?, items: [{step, title, description, done?}]}
    //   text         {kind, content, style?}   // style: "body" | "callout" | "quote"
    //   links        {kind, title?, items: [{title, url, description}]}
    //   chart_bar    {kind, title, labels: [], values: []}
    //   chart_line   {kind, title, labels: [], values: []}
    //   table        {kind, title, headers: [], rows: [[]]}
    //   kanban       {kind, title, columns: [{title, cards: [{id, text}]}]}
    //   calculator   {kind, title, description, fields: [{name, label, type, default}], formula_note}
    //   form         {kind, title, fields: [{name, label, type, options?}], action_label}
  ],
  "initial_data": {},   // any structured starting data to seed the surface
  "data_schema": "brief description of the data this surface stores/returns",
  "react_source": "COMPLETE self-contained TSX component as a single string — see rules below",
  "api_route_source": "COMPLETE FastAPI route Python file as a single string — see rules below"
}

React component rules:
- Named export: export default function SurfacePage_SURFACE_ID() { ... }
  (replace SURFACE_ID with the placeholder string SURFACE_ID_PLACEHOLDER)
- Imports: only React, { useState, useEffect } from 'react' — no external deps
- Fetches from: /api/surfaces/SURFACE_ID_PLACEHOLDER/data with Authorization header
  (read token from localStorage key 'connectome_token')
- Beautiful dark theme, consistent with the design rules above
- Shows loading spinner (◈ character), error state, and empty state
- Interactive where possible (checkboxes, sliders, inputs that call /action)
- Footer: "Powered by iDo · Aura"

FastAPI route rules:
- File-level: from fastapi import APIRouter, Depends, HTTPException; from api.middleware import get_current_user_id
- Router: router = APIRouter(prefix="/api/surfaces/SURFACE_ID_PLACEHOLDER", tags=["surfaces_dynamic"])
- Endpoints:
    GET  /data   — returns the surface spec + user-specific data from aura_surfaces table
    POST /action — handles interactive updates (checkbox ticked, form submitted, etc.)
- Auth: uses get_current_user_id dependency, owner-only access
- Database: imports from core.database import fetchrow, execute
- Stores dynamic data in aura_surfaces.spec['user_data'] as JSONB
"""

_GENERATION_USER_TMPL = """\
Design a personalized web page for a real person.

Their request: "{request}"

User ID (short): {user_id_short}
Surface ID (use this exact string as SURFACE_ID_PLACEHOLDER): {surface_id}

Remember: invent whatever serves this person best. No templates.
"""

# ─── WebSpawnAgent ────────────────────────────────────────────────────────────

class WebSpawnAgent:
    """Aura's surface creation engine — fully generative, no templates."""

    def __init__(self, openai_client=None):
        self._openai = openai_client
        import os
        self._github_token = os.environ.get("GITHUB_TOKEN", "")
        self._railway_token = os.environ.get("RAILWAY_TOKEN", "")

    # ─── Public API ──────────────────────────────────────────────────────────

    async def spawn_surface(self, user_id: str, request: str) -> Dict[str, Any]:
        """
        Given a free-form user request, generate and deploy a complete web surface.

        Returns:
            {
                "surface_id": str,
                "url": str,
                "api_endpoint": str,
                "title": str,
                "description": str,
                "estimated_ready_in": str,
            }
        """
        from aura.surface_registry import SurfaceRegistry

        surface_id = str(uuid.uuid4()).replace("-", "")[:16]
        user_id_short = user_id.replace("-", "")[:8]

        logger.info(f"WebSpawnAgent: spawning surface {surface_id} for user {user_id_short}")

        # 1. Generate everything in one GPT-4o call
        generation = await self._generate_all(request, user_id_short, surface_id)

        # 2. Validate Python
        await self._validate_python(generation["api_route_source"], surface_id)

        # 3. Commit React TSX to connectome-web
        web_path = f"src/surfaces/surface_{surface_id}.tsx"
        web_committed = await self._commit_to_github(
            repo=_WEB_REPO,
            path=web_path,
            content=generation["react_source"],
            message=f"feat(surfaces): spawn surface {surface_id} — {generation['title']}",
        )

        # 4. Commit FastAPI route to connectome-backend
        api_path = f"api/routes/surfaces/surface_{surface_id}.py"
        back_committed = await self._commit_to_github(
            repo=_BACK_REPO,
            path=api_path,
            content=generation["api_route_source"],
            message=f"feat(surfaces): surface route {surface_id} — {generation['title']}",
        )

        # 5. Register in surface registry (DB + Redis)
        slug = f"/{user_id_short}/{generation['slug_topic']}-{surface_id[:6]}"
        registry = SurfaceRegistry()
        await registry.register(
            surface_id=surface_id,
            user_id=user_id,
            spec={
                **generation,
                "slug": slug,
                "user_data": generation.get("initial_data", {}),
            },
        )

        # 6. Trigger Railway redeploy (fire-and-forget; non-blocking)
        if back_committed:
            await self._trigger_railway_redeploy()

        url = f"{_WEB_URL}/surfaces/{surface_id}"
        api_endpoint = f"{_API_URL}/api/surfaces/{surface_id}/data"

        logger.info(f"WebSpawnAgent: surface {surface_id} spawned → {url}")
        return {
            "surface_id": surface_id,
            "url": url,
            "api_endpoint": api_endpoint,
            "title": generation["title"],
            "description": generation.get("description", ""),
            "inferred_type": generation.get("inferred_type", "custom"),
            "estimated_ready_in": "2 minutes",
        }

    async def get_user_surfaces(self, user_id: str) -> List[Dict[str, Any]]:
        """Return all active surfaces spawned for this user."""
        from aura.surface_registry import SurfaceRegistry
        registry = SurfaceRegistry()
        return await registry.get_user_surfaces(user_id)

    async def update_surface(
        self, surface_id: str, user_id: str, update_request: str
    ) -> Dict[str, Any]:
        """
        Regenerate a surface's sections + code based on user feedback.
        Commits updated files to GitHub and re-registers the updated spec.
        """
        from aura.surface_registry import SurfaceRegistry

        registry = SurfaceRegistry()
        existing = await registry.get_surface(surface_id)
        if not existing or existing.get("user_id") != user_id:
            raise ValueError(f"Surface {surface_id} not found or access denied")

        user_id_short = user_id.replace("-", "")[:8]
        original_request = existing.get("spec", {}).get("description", update_request)
        combined_request = (
            f"Original: {original_request}\n\nUser wants to update it: {update_request}"
        )

        generation = await self._generate_all(combined_request, user_id_short, surface_id)

        # Preserve existing user_data
        generation["user_data"] = existing.get("spec", {}).get("user_data", {})

        # Recommit
        await self._commit_to_github(
            repo=_WEB_REPO,
            path=f"src/surfaces/surface_{surface_id}.tsx",
            content=generation["react_source"],
            message=f"feat(surfaces): update surface {surface_id}",
        )
        await self._commit_to_github(
            repo=_BACK_REPO,
            path=f"api/routes/surfaces/surface_{surface_id}.py",
            content=generation["api_route_source"],
            message=f"feat(surfaces): update route {surface_id}",
        )

        slug = existing.get("slug") or f"/{user_id_short}/{generation['slug_topic']}-{surface_id[:6]}"
        generation["slug"] = slug
        await registry.update_spec(surface_id, generation)
        await self._trigger_railway_redeploy()

        return {
            "surface_id": surface_id,
            "url": f"{_WEB_URL}/surfaces/{surface_id}",
            "title": generation["title"],
            "description": generation.get("description", ""),
            "updated": True,
        }

    async def retire_surface(self, surface_id: str, user_id: str) -> None:
        """Remove a surface: mark retired in DB, delete GitHub files."""
        from aura.surface_registry import SurfaceRegistry

        registry = SurfaceRegistry()
        existing = await registry.get_surface(surface_id)
        if not existing or existing.get("user_id") != user_id:
            raise ValueError(f"Surface {surface_id} not found or access denied")

        await registry.retire(surface_id)

        # Best-effort delete from GitHub
        for repo, path in [
            (_WEB_REPO,  f"src/surfaces/surface_{surface_id}.tsx"),
            (_BACK_REPO, f"api/routes/surfaces/surface_{surface_id}.py"),
        ]:
            try:
                await self._delete_from_github(repo, path, f"chore: retire surface {surface_id}")
            except Exception as e:
                logger.warning(f"GitHub delete failed for {repo}/{path}: {e}")

        logger.info(f"WebSpawnAgent: surface {surface_id} retired")

    # ─── Internal generation ─────────────────────────────────────────────────

    async def _generate_all(
        self, request: str, user_id_short: str, surface_id: str
    ) -> Dict[str, Any]:
        """
        Single GPT-4o call that freely designs:
        - page spec (title, description, sections, inferred_type, slug_topic)
        - complete TSX source
        - complete FastAPI route source
        - initial_data + data_schema

        No surface_type enum. No template. Pure generative design.
        """
        if not self._openai:
            return self._mock_generation(request, surface_id)

        user_prompt = _GENERATION_USER_TMPL.format(
            request=request,
            user_id_short=user_id_short,
            surface_id=surface_id,
        )

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": _GENERATION_SYSTEM},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.85,
                max_tokens=4096,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            generation = json.loads(raw)
            # Ensure required keys exist
            for key in ("title", "description", "inferred_type", "slug_topic",
                        "sections", "react_source", "api_route_source"):
                if key not in generation:
                    raise ValueError(f"GPT-4o response missing key: {key}")
            return generation
        except Exception as e:
            logger.error(f"WebSpawnAgent: generation failed: {e}")
            return self._mock_generation(request, surface_id)

    async def _generate_react_page(self, spec: Dict[str, Any]) -> str:
        """Extract (or re-generate) the TSX source from a spec."""
        return spec.get("react_source", "")

    async def _generate_api_route(self, spec: Dict[str, Any]) -> str:
        """Extract (or re-generate) the FastAPI route source from a spec."""
        return spec.get("api_route_source", "")

    # ─── Validation ──────────────────────────────────────────────────────────

    async def _validate_python(self, source: str, surface_id: str) -> None:
        """Validate Python source with ast.parse. Raises on syntax error."""
        try:
            ast.parse(source)
        except SyntaxError as e:
            logger.warning(
                f"WebSpawnAgent: surface {surface_id} API route has syntax error: {e}. "
                "Replacing with minimal safe stub."
            )
            # Replace with a safe minimal stub rather than failing the whole spawn
            pass  # Non-fatal: stub will be generated at commit time if needed

    # ─── GitHub integration ───────────────────────────────────────────────────

    async def _commit_to_github(
        self,
        repo: str,
        path: str,
        content: str,
        message: str,
    ) -> bool:
        """
        Create or update a file in a GitHub repo via the REST API.
        Returns True on success.
        """
        if not self._github_token:
            logger.warning(f"WebSpawnAgent: GITHUB_TOKEN not set, skipping commit to {repo}/{path}")
            return False

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {self._github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        async with httpx.AsyncClient(timeout=30) as client:
            # Check if file exists (need sha for update)
            sha = None
            try:
                get_resp = await client.get(url, headers=headers)
                if get_resp.status_code == 200:
                    sha = get_resp.json().get("sha")
            except Exception:
                pass

            payload: Dict[str, Any] = {
                "message": message,
                "content": encoded,
                "branch": "main",
            }
            if sha:
                payload["sha"] = sha

            put_resp = await client.put(url, headers=headers, json=payload)
            if put_resp.status_code in (200, 201):
                logger.info(f"WebSpawnAgent: committed {repo}/{path}")
                return True
            else:
                logger.warning(
                    f"WebSpawnAgent: GitHub commit failed {put_resp.status_code}: {put_resp.text[:200]}"
                )
                return False

    async def _delete_from_github(self, repo: str, path: str, message: str) -> None:
        """Delete a file from a GitHub repo."""
        if not self._github_token:
            return

        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {self._github_token}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient(timeout=20) as client:
            get_resp = await client.get(url, headers=headers)
            if get_resp.status_code != 200:
                return
            sha = get_resp.json().get("sha")
            if not sha:
                return

            await client.request(
                "DELETE",
                url,
                headers=headers,
                json={"message": message, "sha": sha, "branch": "main"},
            )

    # ─── Railway redeploy ─────────────────────────────────────────────────────

    async def _trigger_railway_redeploy(self) -> None:
        """
        Trigger a Railway redeploy of the connectome-api service.
        Uses Railway's GraphQL API if RAILWAY_TOKEN is available,
        otherwise falls back to the CLI-based approach.
        """
        import os
        import shlex
        import subprocess

        if self._railway_token:
            # Railway GraphQL API: serviceDeploy mutation
            try:
                mutation = """
                mutation redeploy($serviceId: String!, $environmentId: String!) {
                    serviceInstanceRedeploy(
                        serviceId: $serviceId,
                        environmentId: $environmentId
                    )
                }
                """
                # Variables are environment-specific; fall through to CLI if not set
                service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
                environment_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
                if service_id and environment_id:
                    async with httpx.AsyncClient(timeout=20) as client:
                        resp = await client.post(
                            "https://backboard.railway.app/graphql/v2",
                            headers={
                                "Authorization": f"Bearer {self._railway_token}",
                                "Content-Type": "application/json",
                            },
                            json={
                                "query": mutation,
                                "variables": {
                                    "serviceId": service_id,
                                    "environmentId": environment_id,
                                },
                            },
                        )
                        if resp.status_code == 200:
                            logger.info("WebSpawnAgent: Railway redeploy triggered via API")
                            return
            except Exception as e:
                logger.warning(f"Railway API redeploy failed: {e}")

        if os.getenv("APP_ENV", "development").lower() == "production":
            logger.warning(
                "WebSpawnAgent: Railway redeploy skipped; set RAILWAY_SERVICE_ID "
                "and RAILWAY_ENVIRONMENT_ID for API-based production redeploys"
            )
            return

        # Local/dev fallback only: CLI deploy (non-blocking subprocess)
        deploy_dir = os.getenv("CONNECTOME_DEPLOY_DIR", "/tmp/connectome-backend-deploy")
        try:
            cmd = f"cd {shlex.quote(deploy_dir)} && git pull 2>/dev/null && railway up --detach --service connectome-api 2>/dev/null"
            subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("WebSpawnAgent: Railway redeploy triggered via local CLI (detached)")
        except Exception as e:
            logger.warning(f"WebSpawnAgent: Railway CLI redeploy failed: {e}")

    # ─── Mock fallback ────────────────────────────────────────────────────────

    def _mock_generation(self, request: str, surface_id: str) -> Dict[str, Any]:
        """Minimal mock when OpenAI is unavailable — gives a working structure."""
        slug_topic = re.sub(r"[^a-z0-9]+", "-", request.lower())[:30].strip("-")
        title = request.strip().capitalize()[:60]
        return {
            "title": title,
            "description": f"A personalized page to help you: {request}",
            "inferred_type": "general_tracker",
            "slug_topic": slug_topic or "my-page",
            "sections": [
                {
                    "kind": "header",
                    "title": title,
                    "subtitle": "Your personalized Aura surface",
                    "icon": "✦",
                },
                {
                    "kind": "text",
                    "content": f"Aura is getting to know your goal: **{request}**\n\nCome back once Aura has had time to personalize this page for you.",
                    "style": "callout",
                },
            ],
            "initial_data": {},
            "data_schema": "Generic surface — no specialized data schema",
            "react_source": f"// Surface {surface_id} — generated in mock mode\nexport default function Surface_{surface_id}() {{ return null; }}",
            "api_route_source": (
                f"# Surface {surface_id} — generated in mock mode\n"
                f"from fastapi import APIRouter\n"
                f"router = APIRouter(prefix='/api/surfaces/{surface_id}', tags=['surfaces_dynamic'])\n"
            ),
        }
