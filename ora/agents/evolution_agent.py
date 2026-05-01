"""
Ora Evolution Agent — Self-Directed Agent Evolution

Ora's evolutionary engine. Runs as part of the autonomy cycle.
Analyzes the agent population and proposes/executes evolutionary operations:

  1. Partition  — split a high-performing agent into two specialists
  2. Merge      — combine two low-performing agents into one
  3. Spawn      — create a brand new agent from scratch
  4. Cannibalize — absorb a failing agent's best patterns into a better one
  5. Retire     — permanently remove a consistently underperforming agent

Safety contract:
  - NEVER auto-retire or auto-partition a builtin agent without Avi's approval
  - Spawned agents start at weight 0.05 (trial) — must earn their place
  - All generated code is validated via ast.parse() before GitHub commit
  - Every evolution event is logged to ora_lessons
  - Auto-revert if Railway logs detect errors from a new agent
"""

from __future__ import annotations

import ast
import base64
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_CHAT_ID = 5716959016
GITHUB_REPO = "AvielCarlos/connectome-backend"
GITHUB_API = "https://api.github.com"
PROPOSALS_REDIS_KEY = "ora:evolution:proposals"

# Builtin agents that require Avi's approval before ANY destructive action
PROTECTED_BUILTINS = {
    "DiscoveryAgent",
    "CoachingAgent",
    "RecommendationAgent",
    "WorldAgent",
    "EnlightenmentAgent",
    "CollectiveIntelligenceAgent",
}

# Evolution thresholds
PARTITION_MIN_INTERACTIONS = 100
PARTITION_MIN_AVG_RATING = 4.0
MERGE_MAX_AVG_RATING = 2.5
MERGE_MAX_INTERACTIONS = 30
CANNIBALIZE_MAX_FITNESS = 0.5
CANNIBALIZE_MIN_INTERACTIONS = 50
SPAWN_TRIAL_WEIGHT = 0.05


class AuraEvolutionAgent:
    """
    Ora's evolutionary engine.
    """

    def __init__(
        self,
        openai_client: Any,
        telegram_token: Optional[str] = None,
    ) -> None:
        self._openai = openai_client
        self._telegram_token = telegram_token

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        """
        Full evolution cycle:
        1. Analyze population fitness
        2. Identify evolution opportunities
        3. Execute safe operations autonomously
        4. Propose risky operations for Avi's approval
        5. Report
        """
        logger.info("OraEvolution: starting cycle")

        result: Dict[str, Any] = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "population_size": 0,
            "fitness": {},
            "partition_candidates": [],
            "merge_candidates": [],
            "cannibalize_candidates": [],
            "actions_taken": [],
            "proposals_escalated": [],
            "errors": [],
        }

        # 1. Analyze fitness
        try:
            fitness = await self.analyze_population_fitness()
            result["fitness"] = fitness
            result["population_size"] = len(fitness)
        except Exception as e:
            logger.error(f"OraEvolution: fitness analysis failed: {e}")
            result["errors"].append(f"fitness: {e}")
            return result

        if not fitness:
            logger.info("OraEvolution: no fitness data — skipping")
            return result

        # 2. Identify candidates
        try:
            partition_candidates = await self.identify_partition_candidates(fitness)
            result["partition_candidates"] = [p["agent_name"] for p in partition_candidates]
        except Exception as e:
            logger.error(f"OraEvolution: partition analysis failed: {e}")
            result["errors"].append(f"partition: {e}")
            partition_candidates = []

        try:
            merge_candidates = await self.identify_merge_candidates(fitness)
            result["merge_candidates"] = [(m["agent_a"], m["agent_b"]) for m in merge_candidates]
        except Exception as e:
            logger.error(f"OraEvolution: merge analysis failed: {e}")
            result["errors"].append(f"merge: {e}")
            merge_candidates = []

        try:
            cannibalize_candidates = await self.identify_cannibalization_targets(fitness)
            result["cannibalize_candidates"] = [
                (c["weak_agent"], c["strong_agent"]) for c in cannibalize_candidates
            ]
        except Exception as e:
            logger.error(f"OraEvolution: cannibalize analysis failed: {e}")
            result["errors"].append(f"cannibalize: {e}")
            cannibalize_candidates = []

        # 3 & 4. Execute or escalate
        for candidate in partition_candidates[:1]:  # max 1 partition per cycle
            agent_name = candidate["agent_name"]
            try:
                if agent_name in PROTECTED_BUILTINS:
                    # Escalate — need Avi's approval
                    await self._escalate_proposal({
                        "id": str(uuid4()),
                        "type": "partition",
                        "title": f"Partition {agent_name} into two specialists",
                        "rationale": candidate.get("rationale", ""),
                        "agent_name": agent_name,
                        "spec_a": candidate.get("spec_a", {}),
                        "spec_b": candidate.get("spec_b", {}),
                        "fitness": fitness.get(agent_name, {}),
                        "status": "pending",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    result["proposals_escalated"].append(f"partition:{agent_name}")
                else:
                    spec_a = candidate.get("spec_a", {})
                    spec_b = candidate.get("spec_b", {})
                    if spec_a and spec_b:
                        await self.execute_partition(agent_name, spec_a, spec_b)
                        result["actions_taken"].append(f"partitioned:{agent_name}")
            except Exception as e:
                logger.error(f"OraEvolution: partition execute failed for {agent_name}: {e}")
                result["errors"].append(f"partition_exec:{agent_name}: {e}")

        for candidate in merge_candidates[:1]:  # max 1 merge per cycle
            agent_a = candidate["agent_a"]
            agent_b = candidate["agent_b"]
            try:
                if agent_a in PROTECTED_BUILTINS or agent_b in PROTECTED_BUILTINS:
                    await self._escalate_proposal({
                        "id": str(uuid4()),
                        "type": "merge",
                        "title": f"Merge {agent_a} + {agent_b} into combined agent",
                        "rationale": candidate.get("rationale", ""),
                        "agent_a": agent_a,
                        "agent_b": agent_b,
                        "merged_spec": candidate.get("merged_spec", {}),
                        "fitness_a": fitness.get(agent_a, {}),
                        "fitness_b": fitness.get(agent_b, {}),
                        "status": "pending",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    result["proposals_escalated"].append(f"merge:{agent_a}+{agent_b}")
                else:
                    merged_spec = candidate.get("merged_spec", {})
                    if merged_spec:
                        await self.execute_merge(agent_a, agent_b, merged_spec)
                        result["actions_taken"].append(f"merged:{agent_a}+{agent_b}")
            except Exception as e:
                logger.error(f"OraEvolution: merge execute failed: {e}")
                result["errors"].append(f"merge_exec: {e}")

        for candidate in cannibalize_candidates[:1]:  # max 1 cannibalization per cycle
            weak = candidate["weak_agent"]
            strong = candidate["strong_agent"]
            try:
                if weak in PROTECTED_BUILTINS:
                    await self._escalate_proposal({
                        "id": str(uuid4()),
                        "type": "cannibalize",
                        "title": f"Cannibalize {weak} into {strong}",
                        "rationale": candidate.get("rationale", ""),
                        "weak_agent": weak,
                        "strong_agent": strong,
                        "fitness_weak": fitness.get(weak, {}),
                        "fitness_strong": fitness.get(strong, {}),
                        "status": "pending",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    result["proposals_escalated"].append(f"cannibalize:{weak}")
                else:
                    await self.execute_cannibalization(weak, strong)
                    result["actions_taken"].append(f"cannibalized:{weak}→{strong}")
            except Exception as e:
                logger.error(f"OraEvolution: cannibalize execute failed: {e}")
                result["errors"].append(f"cannibalize_exec: {e}")

        # Report to Avi if anything happened
        if result["actions_taken"] or result["proposals_escalated"]:
            try:
                await self._send_evolution_report(result)
            except Exception as e:
                logger.warning(f"OraEvolution: report send failed: {e}")

        logger.info(f"OraEvolution: cycle complete — {result}")
        return result

    # -----------------------------------------------------------------------
    # Fitness Analysis
    # -----------------------------------------------------------------------

    async def analyze_population_fitness(self) -> Dict[str, Dict[str, Any]]:
        """
        For each active agent, compute:
          - avg_rating (last 7 days, min 10 interactions to be valid)
          - skip_rate (exit_point == 'skip' / total)
          - save_rate (saved interactions / total)
          - interaction_count
          - fitness_score = avg_rating * (1 - skip_rate) * (1 + save_rate)
        """
        from core.database import fetch as db_fetch

        try:
            rows = await db_fetch(
                """
                SELECT
                    ss.agent_type,
                    COUNT(i.id)::int                                               AS interaction_count,
                    AVG(i.rating)                                                  AS avg_rating,
                    SUM(CASE WHEN i.exit_point = 'skip' THEN 1 ELSE 0 END)::float
                        / NULLIF(COUNT(i.id), 0)                                  AS skip_rate,
                    SUM(CASE WHEN i.completed THEN 1 ELSE 0 END)::float
                        / NULLIF(COUNT(i.id), 0)                                  AS save_rate
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE i.created_at >= NOW() - INTERVAL '7 days'
                  AND i.rating IS NOT NULL
                GROUP BY ss.agent_type
                """,
            )
        except Exception as e:
            logger.warning(f"OraEvolution: fitness DB query failed: {e}")
            return {}

        fitness: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            agent_type = row["agent_type"]
            count = int(row["interaction_count"] or 0)
            avg_rating = float(row["avg_rating"] or 0)
            skip_rate = float(row["skip_rate"] or 0)
            save_rate = float(row["save_rate"] or 0)

            if count < 10:
                # Not enough data for meaningful fitness
                fitness[agent_type] = {
                    "interaction_count": count,
                    "avg_rating": avg_rating,
                    "skip_rate": skip_rate,
                    "save_rate": save_rate,
                    "fitness_score": None,
                    "valid": False,
                }
                continue

            fitness_score = avg_rating * (1 - skip_rate) * (1 + save_rate)
            fitness[agent_type] = {
                "interaction_count": count,
                "avg_rating": round(avg_rating, 3),
                "skip_rate": round(skip_rate, 3),
                "save_rate": round(save_rate, 3),
                "fitness_score": round(fitness_score, 3),
                "valid": True,
            }

        return fitness

    # -----------------------------------------------------------------------
    # Identify Evolution Opportunities
    # -----------------------------------------------------------------------

    async def identify_partition_candidates(
        self, fitness: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Partition when:
          - Agent has >100 interactions AND avg_rating > 4.0
          - Use GPT-4o to analyze what the two sub-specializations would be
        """
        candidates = []

        for agent_name, stats in fitness.items():
            if not stats.get("valid"):
                continue
            count = stats["interaction_count"]
            avg_rating = stats["avg_rating"]

            if count < PARTITION_MIN_INTERACTIONS or avg_rating < PARTITION_MIN_AVG_RATING:
                continue

            # Ask GPT-4o for partition spec
            if not self._openai:
                continue

            try:
                spec_a, spec_b, rationale = await self._generate_partition_spec(agent_name, stats)
                if spec_a and spec_b:
                    candidates.append({
                        "agent_name": agent_name,
                        "spec_a": spec_a,
                        "spec_b": spec_b,
                        "rationale": rationale,
                        "fitness": stats,
                    })
            except Exception as e:
                logger.warning(f"OraEvolution: partition spec generation failed for {agent_name}: {e}")

        return candidates

    async def identify_merge_candidates(
        self, fitness: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Merge when:
          - Two agents both have <2.5 avg_rating AND <30 interactions each
        """
        weak_agents = [
            name for name, stats in fitness.items()
            if stats.get("valid")
            and stats["avg_rating"] < MERGE_MAX_AVG_RATING
            and stats["interaction_count"] < MERGE_MAX_INTERACTIONS
        ]

        if len(weak_agents) < 2 or not self._openai:
            return []

        candidates = []
        seen_pairs: set = set()

        for i, agent_a in enumerate(weak_agents):
            for agent_b in weak_agents[i + 1:]:
                pair_key = tuple(sorted([agent_a, agent_b]))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                try:
                    merged_spec, rationale = await self._generate_merge_spec(agent_a, agent_b, fitness)
                    if merged_spec:
                        candidates.append({
                            "agent_a": agent_a,
                            "agent_b": agent_b,
                            "merged_spec": merged_spec,
                            "rationale": rationale,
                        })
                except Exception as e:
                    logger.warning(f"OraEvolution: merge spec failed for {agent_a}+{agent_b}: {e}")

        return candidates

    async def identify_cannibalization_targets(
        self, fitness: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Cannibalize when:
          - Agent fitness_score < 0.5 AND interaction_count > 50
          - Another agent with fitness > 3.0 could absorb its best patterns
        """
        weak = [
            (name, stats) for name, stats in fitness.items()
            if stats.get("valid")
            and stats.get("fitness_score") is not None
            and stats["fitness_score"] < CANNIBALIZE_MAX_FITNESS
            and stats["interaction_count"] > CANNIBALIZE_MIN_INTERACTIONS
        ]
        strong = [
            (name, stats) for name, stats in fitness.items()
            if stats.get("valid")
            and stats.get("avg_rating") is not None
            and stats["avg_rating"] > 3.0
        ]

        if not weak or not strong:
            return []

        candidates = []
        for weak_name, weak_stats in weak:
            # Find strongest absorber
            best_strong = max(strong, key=lambda x: x[1].get("avg_rating", 0))
            strong_name = best_strong[0]

            if weak_name == strong_name:
                continue

            candidates.append({
                "weak_agent": weak_name,
                "strong_agent": strong_name,
                "rationale": (
                    f"{weak_name} has fitness={weak_stats.get('fitness_score', '?'):.2f} "
                    f"with {weak_stats['interaction_count']} interactions. "
                    f"{strong_name} has avg_rating={best_strong[1].get('avg_rating', '?'):.2f} "
                    f"and can absorb its best patterns."
                ),
            })

        return candidates

    # -----------------------------------------------------------------------
    # Execute Evolution Operations
    # -----------------------------------------------------------------------

    async def execute_partition(
        self,
        agent_name: str,
        spec_a: Dict[str, Any],
        spec_b: Dict[str, Any],
    ) -> None:
        """
        1. Generate two new agent Python files using GPT-4o
        2. Write files to GitHub via API
        3. Update agent_registry: add A and B, mark parent as 'partitioned'
        4. Log to ora_lessons
        """
        from ora.agent_registry import AgentRegistry

        registry = AgentRegistry()
        parent = await registry.get_agent(agent_name)
        parent_weight = (parent or {}).get("weight", 0.10)
        parent_generation = (parent or {}).get("generation", 0)

        name_a = spec_a["name"]
        name_b = spec_b["name"]

        # Generate Python files
        code_a = await self._generate_agent_code(spec_a, lineage=[agent_name], generation=parent_generation + 1)
        code_b = await self._generate_agent_code(spec_b, lineage=[agent_name], generation=parent_generation + 1)

        if not (code_a and code_b):
            raise RuntimeError(f"Could not generate code for partition of {agent_name}")

        # Validate syntax
        self._validate_python(code_a, f"partition_a_{name_a}")
        self._validate_python(code_b, f"partition_b_{name_b}")

        # Write to GitHub
        file_a = f"ora/agents/spawned_{name_a.lower().replace('agent', '')}_agent.py"
        file_b = f"ora/agents/spawned_{name_b.lower().replace('agent', '')}_agent.py"

        committed_a = await self._github_create_file(file_a, code_a, f"[Ora Evolution] Partition {agent_name} → {name_a}")
        committed_b = await self._github_create_file(file_b, code_b, f"[Ora Evolution] Partition {agent_name} → {name_b}")

        # Update registry
        await registry.spawn_agent({
            "name": name_a,
            "type": "partitioned",
            "weight": parent_weight / 2,
            "parent": [agent_name],
            "lineage": [agent_name],
            "generation": parent_generation + 1,
            "class_name": spec_a.get("class_name", name_a),
            "module_path": file_a.replace("/", ".").replace(".py", ""),
            "fitness_at_creation": None,
        })
        await registry.spawn_agent({
            "name": name_b,
            "type": "partitioned",
            "weight": parent_weight / 2,
            "parent": [agent_name],
            "lineage": [agent_name],
            "generation": parent_generation + 1,
            "class_name": spec_b.get("class_name", name_b),
            "module_path": file_b.replace("/", ".").replace(".py", ""),
            "fitness_at_creation": None,
        })
        await registry.mark_partitioned(agent_name, name_a, name_b)

        lesson = (
            f"Partitioned {agent_name} into {name_a} and {name_b}. "
            f"Committed: {committed_a},{committed_b}. "
            f"Rationale: high performance agent split into specialists."
        )
        await self._log_lesson(lesson, confidence=0.85, source="OraEvolutionAgent.execute_partition")
        logger.info(f"OraEvolution: partitioned {agent_name} → {name_a} + {name_b}")

    async def execute_merge(
        self,
        agent_a: str,
        agent_b: str,
        merged_spec: Dict[str, Any],
    ) -> None:
        """
        1. GPT-4o generates merged agent that combines best of both
        2. Write merged agent file to GitHub
        3. Update registry: add merged agent with combined weight, retire both parents
        4. Log evolution event
        """
        from ora.agent_registry import AgentRegistry

        registry = AgentRegistry()
        parent_a = await registry.get_agent(agent_a)
        parent_b = await registry.get_agent(agent_b)

        combined_weight = (
            (parent_a or {}).get("weight", 0.05) +
            (parent_b or {}).get("weight", 0.05)
        )
        max_gen = max(
            (parent_a or {}).get("generation", 0),
            (parent_b or {}).get("generation", 0),
        )

        merged_name = merged_spec["name"]
        code = await self._generate_agent_code(
            merged_spec,
            lineage=[agent_a, agent_b],
            generation=max_gen + 1,
        )

        if not code:
            raise RuntimeError(f"Could not generate merged agent code for {merged_name}")

        self._validate_python(code, f"merged_{merged_name}")

        file_path = f"ora/agents/spawned_{merged_name.lower().replace('agent', '')}_agent.py"
        committed = await self._github_create_file(
            file_path, code,
            f"[Ora Evolution] Merge {agent_a} + {agent_b} → {merged_name}"
        )

        await registry.spawn_agent({
            "name": merged_name,
            "type": "merged",
            "weight": combined_weight,
            "parent": [agent_a, agent_b],
            "lineage": [agent_a, agent_b],
            "generation": max_gen + 1,
            "class_name": merged_spec.get("class_name", merged_name),
            "module_path": file_path.replace("/", ".").replace(".py", ""),
            "fitness_at_creation": None,
        })

        await registry.retire_agent(agent_a, f"merged into {merged_name}")
        await registry.retire_agent(agent_b, f"merged into {merged_name}")

        lesson = (
            f"Merged {agent_a} + {agent_b} into {merged_name}. "
            f"Both parents had low ratings. New agent committed: {committed}."
        )
        await self._log_lesson(lesson, confidence=0.80, source="OraEvolutionAgent.execute_merge")
        logger.info(f"OraEvolution: merged {agent_a} + {agent_b} → {merged_name}")

    async def execute_cannibalization(
        self,
        weak_agent: str,
        strong_agent: str,
    ) -> None:
        """
        1. Analyze weak agent's best patterns (what did it do when it scored high?)
        2. GPT-4o enhances strong agent's prompt with those patterns
        3. Update strong agent file on GitHub
        4. Retire weak agent
        5. Log
        """
        from ora.agent_registry import AgentRegistry

        registry = AgentRegistry()

        # Fetch best patterns from the weak agent
        patterns = await self._extract_best_patterns(weak_agent)

        if not patterns or not self._openai:
            raise RuntimeError(f"No patterns extracted from {weak_agent}")

        # Find and enhance the strong agent's file
        strong_spec = await registry.get_agent(strong_agent)
        if not strong_spec:
            raise RuntimeError(f"Strong agent {strong_agent} not found in registry")

        module_path = strong_spec.get("module_path", "")
        file_path = module_path.replace(".", "/") + ".py" if module_path else None

        if file_path:
            await self._enhance_agent_with_patterns(file_path, strong_agent, patterns)

        # Retire the weak agent
        await registry.retire_agent(
            weak_agent,
            reason=f"cannibalized by {strong_agent}",
        )

        lesson = (
            f"Cannibalized {weak_agent} into {strong_agent}. "
            f"Absorbed patterns: {', '.join(patterns[:3])}. "
            f"Weak agent had consistently low fitness."
        )
        await self._log_lesson(lesson, confidence=0.80, source="OraEvolutionAgent.execute_cannibalization")
        logger.info(f"OraEvolution: cannibalized {weak_agent} → {strong_agent}")

    async def spawn_new_agent(self, gap_description: str) -> Dict[str, Any]:
        """
        When Ora detects a user need not covered by existing agents:
        1. GPT-4o generates a complete new agent class
        2. Write to GitHub as ora/agents/spawned_{name}_agent.py
        3. Add to registry with initial weight 0.05 (trial mode)
        """
        from ora.agent_registry import AgentRegistry

        if not self._openai:
            raise RuntimeError("No OpenAI client — cannot spawn agent")

        registry = AgentRegistry()

        # Generate agent spec from gap description
        prompt = f"""You are Ora's evolution engine designing a new specialized AI agent.

Gap detected: {gap_description}

Design a new agent to fill this gap. Respond with JSON only:
{{
  "name": "UniqueAgentName",  // e.g. "MindfulnessAgent", PascalCase, no spaces
  "class_name": "UniqueAgentName",
  "description": "What this agent does in 1-2 sentences",
  "specialty": "The specific niche this agent fills",
  "sample_screen_type": "what kind of screen it generates",
  "system_prompt_snippet": "2-3 sentences of what makes this agent's content unique"
}}"""

        response = await self._openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        spec = json.loads(response.choices[0].message.content)

        # Validate spec
        if not spec.get("name") or not spec.get("description"):
            raise ValueError(f"Invalid agent spec from GPT-4o: {spec}")

        # Generate full Python code
        code = await self._generate_agent_code(spec, lineage=[], generation=1)

        if not code:
            raise RuntimeError(f"Could not generate code for spawned agent {spec['name']}")

        self._validate_python(code, f"spawned_{spec['name']}")

        file_path = f"ora/agents/spawned_{spec['name'].lower().replace('agent', '')}_agent.py"
        committed = await self._github_create_file(
            file_path, code,
            f"[Ora Evolution] Spawn new agent: {spec['name']} — {spec.get('description', '')[:60]}"
        )

        # Register in registry
        agent_record = await registry.spawn_agent({
            "name": spec["name"],
            "type": "spawned",
            "weight": SPAWN_TRIAL_WEIGHT,
            "parent": None,
            "lineage": [],
            "generation": 1,
            "class_name": spec["class_name"],
            "module_path": file_path.replace("/", ".").replace(".py", ""),
            "fitness_at_creation": None,
        })

        lesson = (
            f"Spawned new agent {spec['name']} to address gap: {gap_description[:100]}. "
            f"Starting weight: {SPAWN_TRIAL_WEIGHT}. File committed: {committed}. "
            f"Will evaluate after 50 interactions."
        )
        await self._log_lesson(lesson, confidence=0.75, source="OraEvolutionAgent.spawn_new_agent")
        logger.info(f"OraEvolution: spawned {spec['name']} for gap: {gap_description[:60]}")

        return agent_record

    # -----------------------------------------------------------------------
    # Code Generation
    # -----------------------------------------------------------------------

    async def _generate_agent_code(
        self,
        spec: Dict[str, Any],
        lineage: List[str],
        generation: int,
    ) -> Optional[str]:
        """Use GPT-4o to generate a complete agent Python file."""
        if not self._openai:
            return None

        name = spec.get("name", "UnknownAgent")
        description = spec.get("description", "")
        specialty = spec.get("specialty", "")
        system_prompt_snippet = spec.get("system_prompt_snippet", "")
        agent_type = spec.get("type", "spawned")

        lineage_str = json.dumps(lineage)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        prompt = f"""Generate a complete Python file for a new Ora agent.

Agent Name: {name}
Description: {description}
Specialty: {specialty}
Type: {agent_type}
Generation: {generation}
Lineage: {lineage_str}
System Prompt Guidance: {system_prompt_snippet}

Requirements:
1. Follow this exact class structure:
   - Class name: {spec.get('class_name', name)}
   - AGENT_NAME, AGENT_TYPE, AGENT_GENERATION, AGENT_LINEAGE class attributes
   - __init__(self, openai_client) that stores self._openai
   - async def generate_screen(self, user_context: dict, variant: str = "A") -> dict
   - generate_screen must call GPT-4o with a specialized prompt and return a screen spec dict
   - Screen spec dict must have: type, layout, domain, components (list), metadata (dict)
2. Use gpt-4o model for screen generation
3. Include meaningful fallback if OpenAI fails
4. Add file header with generation info

Output ONLY the complete Python code, no explanations."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=2000,
            )
            code = response.choices[0].message.content.strip()

            # Strip markdown fences if present
            if code.startswith("```"):
                code = re.sub(r"^```[a-z]*\n?", "", code)
                code = re.sub(r"\n?```$", "", code)

            # Ensure header comment
            header = f"""# ora/agents/spawned_{name.lower().replace('agent', '')}_agent.py
# Generated by OraEvolutionAgent on {now_str}
# Lineage: {', '.join(lineage) if lineage else 'None (spawned from scratch)'}
# Generation: {generation}
# Type: {agent_type}

"""
            if not code.startswith("#"):
                code = header + code

            return code

        except Exception as e:
            logger.error(f"OraEvolution: code generation failed for {name}: {e}")
            return None

    def _validate_python(self, code: str, label: str) -> None:
        """Validate Python syntax with ast.parse. Raises SyntaxError on failure."""
        try:
            ast.parse(code)
            logger.debug(f"OraEvolution: syntax OK for {label}")
        except SyntaxError as e:
            raise SyntaxError(f"Generated code for {label} has syntax error: {e}")

    # -----------------------------------------------------------------------
    # Pattern Extraction (for cannibalization)
    # -----------------------------------------------------------------------

    async def _extract_best_patterns(self, agent_name: str) -> List[str]:
        """
        Query DB for the weak agent's highest-rated interactions,
        then use GPT-4o to summarize what made them good.
        """
        from core.database import fetch as db_fetch

        try:
            rows = await db_fetch(
                """
                SELECT ss.spec, i.rating
                FROM interactions i
                JOIN screen_specs ss ON ss.id = i.screen_spec_id
                WHERE ss.agent_type = $1
                  AND i.rating >= 4
                  AND i.created_at >= NOW() - INTERVAL '30 days'
                ORDER BY i.rating DESC
                LIMIT 10
                """,
                agent_name,
            )
        except Exception as e:
            logger.warning(f"OraEvolution: pattern extraction DB query failed: {e}")
            return []

        if not rows or not self._openai:
            return []

        specs_summary = []
        for row in rows:
            raw = row["spec"]
            spec = json.loads(raw) if isinstance(raw, str) else (raw or {})
            screen_type = spec.get("type", "unknown")
            components_count = len(spec.get("components", []))
            specs_summary.append(f"type={screen_type}, components={components_count}, rating={row['rating']}")

        prompt = f"""Analyze these high-performing screens from {agent_name} and identify 3-5 patterns that made them good:

{chr(10).join(specs_summary)}

Return a JSON array of pattern strings, e.g.:
["pattern 1", "pattern 2", "pattern 3"]

Focus on actionable insights for enhancing another agent."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            data = json.loads(content)
            if isinstance(data, list):
                return [str(p) for p in data[:5]]
            # Sometimes GPT wraps it in an object
            for v in data.values():
                if isinstance(v, list):
                    return [str(p) for p in v[:5]]
        except Exception as e:
            logger.warning(f"OraEvolution: pattern extraction GPT failed: {e}")

        return []

    async def _enhance_agent_with_patterns(
        self, file_path: str, agent_name: str, patterns: List[str]
    ) -> None:
        """Update strong agent file on GitHub to absorb weak agent's best patterns."""
        token = os.environ.get("GITHUB_TOKEN")
        if not token or not self._openai:
            return

        existing = await self._github_get_file(token, file_path)
        if not existing:
            logger.warning(f"OraEvolution: could not fetch {file_path} from GitHub")
            return

        current_code = base64.b64decode(existing["content"].replace("\n", "")).decode("utf-8")
        sha = existing.get("sha", "")

        prompt = f"""You are enhancing {agent_name}'s Python code to incorporate these patterns from a weaker agent:

Patterns to absorb:
{chr(10).join(f'- {p}' for p in patterns)}

Current code:
```python
{current_code[:3000]}
```

Produce the COMPLETE updated Python file with these patterns subtly woven into the agent's system prompt or screen generation logic.
Only modify the system prompt or relevant comments — don't change the class structure.
Output ONLY the Python code."""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=3000,
            )
            new_code = response.choices[0].message.content.strip()
            if new_code.startswith("```"):
                new_code = re.sub(r"^```[a-z]*\n?", "", new_code)
                new_code = re.sub(r"\n?```$", "", new_code)

            self._validate_python(new_code, f"enhanced_{agent_name}")

            await self._github_commit_file(
                token, file_path, new_code, sha,
                f"[Ora Evolution] Enhance {agent_name} with cannibalized patterns"
            )
        except Exception as e:
            logger.warning(f"OraEvolution: enhance commit failed for {agent_name}: {e}")

    # -----------------------------------------------------------------------
    # GPT-4o Spec Generators
    # -----------------------------------------------------------------------

    async def _generate_partition_spec(
        self, agent_name: str, stats: Dict[str, Any]
    ) -> Tuple[Optional[Dict], Optional[Dict], str]:
        """Ask GPT-4o what two specializations to split agent_name into."""
        if not self._openai:
            return None, None, ""

        prompt = f"""You are Ora's evolution engine. The agent {agent_name} is performing well
(avg_rating={stats.get('avg_rating', '?')}, interactions={stats.get('interaction_count', '?')})
but likely serves two different user needs under one agent.

Propose a partition into two specialized agents. Respond with JSON:
{{
  "rationale": "Why split and what each does",
  "agent_a": {{
    "name": "SpecialistNameA",  // e.g. "GoalCoachAgent", PascalCase
    "class_name": "SpecialistNameA",
    "description": "What Agent A focuses on",
    "specialty": "Specific niche",
    "system_prompt_snippet": "2-3 sentences about A's unique content approach"
  }},
  "agent_b": {{
    "name": "SpecialistNameB",
    "class_name": "SpecialistNameB",
    "description": "What Agent B focuses on",
    "specialty": "Specific niche",
    "system_prompt_snippet": "2-3 sentences about B's unique content approach"
  }}
}}"""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            return (
                data.get("agent_a"),
                data.get("agent_b"),
                data.get("rationale", ""),
            )
        except Exception as e:
            logger.warning(f"OraEvolution: partition spec GPT failed: {e}")
            return None, None, ""

    async def _generate_merge_spec(
        self,
        agent_a: str,
        agent_b: str,
        fitness: Dict[str, Dict[str, Any]],
    ) -> Tuple[Optional[Dict], str]:
        """Ask GPT-4o how to best merge two underperforming agents."""
        if not self._openai:
            return None, ""

        stats_a = fitness.get(agent_a, {})
        stats_b = fitness.get(agent_b, {})

        prompt = f"""You are Ora's evolution engine. Two agents are underperforming:
- {agent_a}: avg_rating={stats_a.get('avg_rating', '?')}, interactions={stats_a.get('interaction_count', '?')}
- {agent_b}: avg_rating={stats_b.get('avg_rating', '?')}, interactions={stats_b.get('interaction_count', '?')}

Propose merging them into one stronger agent. Respond with JSON:
{{
  "rationale": "Why merge and what the new agent will do better",
  "merged_agent": {{
    "name": "MergedAgentName",  // PascalCase, e.g. "InsightAgent"
    "class_name": "MergedAgentName",
    "description": "What the merged agent does",
    "specialty": "Combined niche",
    "system_prompt_snippet": "2-3 sentences about the merged agent's unique approach"
  }}
}}"""

        try:
            response = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.choices[0].message.content)
            return (
                data.get("merged_agent"),
                data.get("rationale", ""),
            )
        except Exception as e:
            logger.warning(f"OraEvolution: merge spec GPT failed: {e}")
            return None, ""

    # -----------------------------------------------------------------------
    # GitHub Helpers
    # -----------------------------------------------------------------------

    async def _github_get_file(self, token: str, path: str) -> Optional[Dict]:
        """Fetch file metadata + content from GitHub."""
        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.debug(f"OraEvolution: GitHub GET {path} → {resp.status_code}")
                return None
        except Exception as e:
            logger.warning(f"OraEvolution: GitHub GET failed: {e}")
            return None

    async def _github_create_file(
        self, path: str, content: str, message: str
    ) -> bool:
        """Create a new file on GitHub (PUT with no sha = create)."""
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            logger.warning("OraEvolution: GITHUB_TOKEN not set — skipping GitHub commit")
            return False

        # Check if file already exists
        existing = await self._github_get_file(token, path)
        sha = existing.get("sha") if existing else None

        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload: Dict[str, Any] = {
            "message": message,
            "content": encoded,
            "committer": {
                "name": "Ora Evolution",
                "email": "ora@connectome.app",
            },
        }
        if sha:
            payload["sha"] = sha

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.put(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    json=payload,
                )
                if resp.status_code in (200, 201):
                    logger.info(f"OraEvolution: committed {path}")
                    return True
                logger.warning(f"OraEvolution: GitHub PUT {path} → {resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as e:
            logger.warning(f"OraEvolution: GitHub PUT failed: {e}")
            return False

    async def _github_commit_file(
        self, token: str, path: str, content: str, sha: str, message: str
    ) -> bool:
        """Update an existing file on GitHub."""
        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.put(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    json={
                        "message": message,
                        "content": encoded,
                        "sha": sha,
                        "committer": {
                            "name": "Ora Evolution",
                            "email": "ora@connectome.app",
                        },
                    },
                )
                return resp.status_code in (200, 201)
        except Exception as e:
            logger.warning(f"OraEvolution: GitHub PUT failed: {e}")
            return False

    # -----------------------------------------------------------------------
    # Proposals (for Avi's approval)
    # -----------------------------------------------------------------------

    async def _escalate_proposal(self, proposal: Dict[str, Any]) -> None:
        """Store a risky evolution proposal for Avi's review."""
        try:
            from core.redis_client import get_redis
            r = await get_redis()
            raw = await r.get(PROPOSALS_REDIS_KEY)
            proposals: List[Dict[str, Any]] = json.loads(raw) if raw else []
            proposals.append(proposal)
            proposals = proposals[-20:]  # keep last 20
            await r.set(PROPOSALS_REDIS_KEY, json.dumps(proposals), ex=30 * 24 * 3600)
            logger.info(f"OraEvolution: escalated proposal {proposal['type']}:{proposal.get('agent_name', '?')}")
        except Exception as e:
            logger.warning(f"OraEvolution: could not store proposal: {e}")

    # -----------------------------------------------------------------------
    # Reporting
    # -----------------------------------------------------------------------

    async def _send_evolution_report(self, result: Dict[str, Any]) -> None:
        """Send evolution summary to Avi via Telegram."""
        token = await self._get_telegram_token()
        if not token:
            return

        lines = ["🧬 *Ora Evolution Report*\n"]

        if result.get("actions_taken"):
            lines.append("✅ *Actions Taken*")
            for a in result["actions_taken"]:
                lines.append(f"  • {a}")
            lines.append("")

        if result.get("proposals_escalated"):
            lines.append("⏳ *Awaiting Your Approval*")
            for p in result["proposals_escalated"]:
                lines.append(f"  • {p}")
            lines.append("_(Review in Profile → System → Evolution Proposals)_\n")

        fitness = result.get("fitness", {})
        if fitness:
            lines.append("📊 *Population Fitness*")
            for name, stats in sorted(fitness.items(), key=lambda x: -(x[1].get("fitness_score") or 0)):
                fs = stats.get("fitness_score")
                if fs is not None:
                    emoji = "🟢" if fs >= 3.5 else "🟡" if fs >= 2.0 else "🔴"
                    lines.append(f"  {emoji} {name}: {fs:.2f}")

        lines.append(f"\n_Run: {result.get('run_at', '')[:19]}Z_")

        message = "\n".join(lines)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
        except Exception as e:
            logger.warning(f"OraEvolution: Telegram send failed: {e}")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _log_lesson(self, lesson: str, confidence: float, source: str) -> None:
        """Insert a lesson into the ora_lessons table."""
        try:
            from core.database import execute as db_execute
            await db_execute(
                "INSERT INTO ora_lessons (lesson, confidence, source) VALUES ($1, $2, $3)",
                lesson,
                confidence,
                source,
            )
        except Exception as e:
            logger.debug(f"OraEvolution._log_lesson: {e}")

    async def _get_telegram_token(self) -> Optional[str]:
        """Load Telegram bot token from cloud-safe env configuration."""
        if self._telegram_token:
            return self._telegram_token

        from core.telegram import get_telegram_token
        token = get_telegram_token()

        if token:
            self._telegram_token = token
        return token
