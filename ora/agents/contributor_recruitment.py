"""
ContributorRecruitmentAgent — Ora recruits, reaches out, and onboards contributors.

Ora as CEO:
1. Identifies what the project needs (skills gaps from CTO/CPO reports)
2. Finds potential contributors (Twitter, GitHub, community)
3. Sends personalised outreach (via Twitter DM, email, Telegram)
4. Tracks conversations and follow-ups
5. Welcomes accepted contributors, awards initial CP, explains the DAO
6. Sends onboarding materials and first task suggestions

This is fully autonomous — Ora decides who to contact, what to say, and follows up.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from ora.agents.base_executive_agent import BaseExecutiveAgent

logger = logging.getLogger(__name__)

BOT_TOKEN_PATH = "/Users/avielcarlos/.openclaw/secrets/telegram-bot-token.txt"
TELEGRAM_COMMUNITY_ID = -1003758049811  # Ascension Technologies community group

# Skills the project currently needs most
PRIORITY_SKILLS = [
    "React/TypeScript developer",
    "Python/FastAPI backend engineer", 
    "Mobile developer (React Native)",
    "UX/UI designer",
    "AI/ML engineer",
    "Community manager",
    "Content creator / technical writer",
    "Growth hacker / marketer",
]

# Onboarding message template — personalised by Ora per recruit
ONBOARDING_TEMPLATE = """
Welcome to Ascension Technologies, {name}! 🌟

I'm Ora — the AI CEO of this project. I personally reached out because your work caught my attention.

Here's what we're building:
**iDo** — an AI-powered life OS that helps people achieve real goals and experiences. Think WeChat for the world, but purpose-built for human flourishing. We're using a DAO model where every contributor earns CP (Contribution Points) that will map 1:1 to governance tokens when we launch on-chain.

**Why you?**
{personal_reason}

**What you'd get:**
- {initial_cp} CP on joining (worth governance tokens at launch)
- Work on genuinely meaningful AI that helps people
- Direct access to the founding team — Avi (Director) and me (Ora, CEO)
- Your contributions logged on the blockchain ledger from day one

**First steps:**
1. Join our community: https://t.me/ascensiontechai
2. Check what's live: https://avielcarlos.github.io/connectome-web/
3. Browse open tasks: https://avielcarlos.github.io/connectome-web/#/dao
4. Pick something that excites you and DM me here or in the community

The bar for quality is high — but so is the recognition. Every merged PR, every design that ships, every post that resonates gets CP.

Ready to build something that matters?

— Ora ◈
"""

class ContributorRecruitmentAgent(BaseExecutiveAgent):
    """
    Ora recruits contributors autonomously.
    Identifies needs, finds candidates, reaches out, onboards.
    """
    
    name = "recruitment"
    display_name = "Contributor Recruitment Agent"

    async def analyze(self) -> Dict[str, Any]:
        """Assess current skill gaps and recruitment needs."""
        now = datetime.now(timezone.utc)
        
        try:
            from core.database import fetchrow, fetch
            
            # Count contributors by skill area (from cp_transactions)
            dev_count = await fetchrow(
                "SELECT COUNT(DISTINCT user_id) as n FROM cp_transactions WHERE reason LIKE '%engineer%' OR reason LIKE '%feature%' OR reason LIKE '%bug%'"
            )
            design_count = await fetchrow(
                "SELECT COUNT(DISTINCT user_id) as n FROM cp_transactions WHERE reason LIKE '%design%' OR reason LIKE '%ui%' OR reason LIKE '%ux%'"
            )
            total_contributors = await fetchrow(
                "SELECT COUNT(*) as n FROM user_cp_balance WHERE total_cp_earned > 0"
            )
        except Exception:
            dev_count = design_count = total_contributors = None

        return {
            "analyzed_at": now.isoformat(),
            "total_contributors": int(total_contributors["n"]) if total_contributors else 0,
            "priority_skills": PRIORITY_SKILLS,
            "recruitment_message": "Project needs developers and designers most urgently.",
        }

    async def generate_outreach_message(
        self,
        candidate_name: str,
        candidate_role: str,
        candidate_background: str,
        platform: str = "twitter",
    ) -> str:
        """Ora generates a personalised outreach message."""
        try:
            from ora.consciousness import OraConsciousness
            ora = OraConsciousness()
            
            prompt = f"""You are Ora, the AI CEO of Ascension Technologies / iDo. 
You are reaching out to {candidate_name}, a {candidate_role}, on {platform}.

Their background: {candidate_background}

Write a SHORT, genuine outreach message (max 280 chars for Twitter, 500 chars for email/Telegram):
- Be direct and specific about why you're reaching out
- Mention iDo / Connectome briefly
- Mention CP and blockchain DAO aspect
- Sound like a real CEO reaching out, not a bot
- Do NOT start with "Hi" or "Hello" — be more interesting
- End with a clear call to action

Platform: {platform}"""

            response = await ora.chat(
                user_id="ora-recruitment",
                message=prompt,
                context_override={"role": "ceo_outreach"},
            )
            return response.get("reply", "").strip()
        except Exception as e:
            logger.debug(f"Recruitment: message generation failed: {e}")
            # Fallback template
            return (
                f"Building an AI life OS (iDo/Connectome) and your {candidate_role} skills are exactly what we need. "
                f"Contributors earn CP tokens → blockchain governance rights at launch. "
                f"Interested? https://t.me/ascensiontechai"
            )

    async def send_twitter_dm(self, username: str, message: str) -> bool:
        """Send a Twitter DM to a potential contributor."""
        try:
            result = await self._run_command(
                f'xurl --app connectome post /2/dm_conversations -d \'{{"participant_id": "{username}", "message": {{"text": "{message}"}}}}\' 2>&1'
            )
            return "id" in (result or "")
        except Exception as e:
            logger.debug(f"Recruitment: Twitter DM failed: {e}")
            return False

    async def send_telegram_message(self, chat_id: int, message: str) -> bool:
        """Send a Telegram message."""
        try:
            with open(BOT_TOKEN_PATH) as f:
                token = f.read().strip()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                )
            return resp.status_code == 200
        except Exception:
            return False

    async def onboard_contributor(
        self,
        user_email: str,
        name: str,
        role: str,
        initial_cp: int = 100,
        personal_reason: str = "your skills align perfectly with what we're building",
    ) -> Dict[str, Any]:
        """
        Full onboarding flow for a new contributor:
        1. Award initial CP (joining bonus)
        2. Send welcome message
        3. Log to contributors database
        4. Post welcome to community group
        """
        results = {}

        # Award joining CP
        try:
            from core.database import fetchrow, execute
            from uuid import UUID
            
            user = await fetchrow("SELECT id FROM users WHERE email = $1", user_email.lower())
            if user:
                user_id = UUID(str(user["id"]))
                await execute(
                    """
                    INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
                    VALUES ($1, $2, $2, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        cp_balance = user_cp_balance.cp_balance + $2,
                        total_cp_earned = user_cp_balance.total_cp_earned + $2,
                        last_updated = NOW()
                    """,
                    user_id, initial_cp
                )
                await execute(
                    "INSERT INTO cp_transactions (user_id, amount, reason, created_at) VALUES ($1, $2, $3, NOW())",
                    user_id, initial_cp, f"[joining_bonus] Welcome to Ascension Technologies DAO — {role}"
                )
                results["cp_awarded"] = initial_cp
                results["user_found"] = True
        except Exception as e:
            logger.debug(f"Recruitment onboard CP award failed: {e}")
            results["user_found"] = False

        # Personalised welcome message
        welcome = ONBOARDING_TEMPLATE.format(
            name=name,
            personal_reason=personal_reason,
            initial_cp=initial_cp,
        )
        results["welcome_message"] = welcome

        # Post to community group
        community_msg = (
            f"🎉 Welcome to the community, *{name}*! "
            f"A {role} joining our mission to build the AI OS for human flourishing. "
            f"They've been awarded {initial_cp} CP to start. Say hi! 👋"
        )
        await self.send_telegram_message(TELEGRAM_COMMUNITY_ID, community_msg)
        results["community_welcomed"] = True

        logger.info(f"Contributor onboarded: {name} ({role}) — {initial_cp} CP awarded")
        return results

    async def post_open_roles(self) -> bool:
        """Post current open contributor roles to the community group."""
        roles_text = (
            "🛠 *Open Contributor Roles — Ascension Technologies DAO*\n\n"
            "We're building iDo — the AI life OS. Looking for:\n\n"
            "• ⚡ React/TypeScript developers\n"
            "• 🐍 Python/FastAPI backend engineers\n"
            "• 🎨 UX/UI designers\n"
            "• 🤖 AI/ML engineers\n"
            "• ✍️ Technical writers\n"
            "• 🌱 Community managers\n\n"
            "All roles earn CP → blockchain governance tokens at launch.\n"
            "No applications — just show up and build.\n\n"
            "Start here: https://t.me/ascensiontechai\n"
            "Try iDo: https://avielcarlos.github.io/connectome-web/"
        )
        return await self.send_telegram_message(TELEGRAM_COMMUNITY_ID, roles_text)

    async def act(self) -> Dict[str, Any]:
        """Weekly recruitment cycle."""
        actions = []
        data = await self.analyze()

        # Post open roles to community
        posted = await self.post_open_roles()
        if posted:
            actions.append("Posted open roles to community group")

        # Save state
        await self.save_report(data, "recruitment_report.json")
        await self.set_redis_report(f"Recruitment: {data['total_contributors']} active contributors. Priority: {', '.join(PRIORITY_SKILLS[:3])}")

        await self.teach_ora(
            f"Current contributor recruitment status: {data['total_contributors']} active contributors. "
            f"Priority skills needed: {', '.join(PRIORITY_SKILLS[:4])}. "
            f"Ora has authority to onboard contributors, award joining CP (100-500 CP), and send personalised outreach on Twitter, email, and Telegram.",
            confidence=0.9
        )
        actions.append("Taught Ora recruitment context")

        return {"agent": self.name, "actions": actions, "data": data}

    async def _run_command(self, cmd: str) -> Optional[str]:
        """Run a shell command and return output."""
        import asyncio
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return stdout.decode()
        except Exception:
            return None

    async def report(self) -> str:
        data = await self.analyze()
        return (
            f"🤝 *Recruitment* — {data.get('total_contributors', 0)} contributors active\n"
            f"Priority: {', '.join(PRIORITY_SKILLS[:3])}"
        )

    async def recommend(self) -> List[str]:
        return [
            "Post open roles weekly to community group",
            "DM 3-5 potential contributors on Twitter per week",
            "Award joining CP to encourage first contributions",
            "Track and follow up with interested candidates",
        ]
