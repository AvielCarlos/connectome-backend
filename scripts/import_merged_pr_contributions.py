#!/usr/bin/env python3
"""Import AvielCarlos GitHub work as approved DAO CP/XP contributions.

Tracks both:
- merged PRs across connectome-backend and connectome-web;
- direct main-branch commits not associated with those PR merge commits.

Idempotency:
- PRs keyed by github_pr_url;
- direct commits keyed by source_id / external_link;
- CP ledger, CP transactions, and XP rows checked before insert.

Run with DATABASE_URL set to production Postgres.
Optional env:
  GITHUB_TOKEN       higher GitHub API limits
  GITHUB_OWNER       default AvielCarlos
  GITHUB_REPOS       default connectome-backend,connectome-web
  IMPORT_SINCE       default 2026-04-29T00:00:00Z
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import Json

OWNER = os.environ.get("GITHUB_OWNER", "AvielCarlos")
REPOS = [r.strip() for r in os.environ.get("GITHUB_REPOS", "connectome-backend,connectome-web").split(",") if r.strip()]
IMPORT_SINCE = os.environ.get("IMPORT_SINCE", "2026-04-29T00:00:00Z")
AUTHOR_LOGIN = os.environ.get("GITHUB_AUTHOR_LOGIN", OWNER)
AUTHOR_EMAILS = {"carlosandromeda8@gmail.com", "avielcarlos@users.noreply.github.com", "avielcarlos@Aviels-MacBook-Pro.local"}


def github_get(path: str, params: dict[str, Any] | None = None) -> Any:
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(f"https://api.github.com{path}{query}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_all_pages(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = github_get(path, {**params, "per_page": 100, "page": page})
        if not batch:
            return out
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def cp_for(title: str, source_kind: str) -> int:
    t = title.lower()
    if source_kind == "commit" and (t.startswith("[aura] identity pack backup") or "backup —" in t):
        return 0
    if t.startswith("docs") or t.startswith("chore") or "document" in t:
        return 300
    if t.startswith("fix") or " fix " in t or "preserve" in t or "badge" in t or "auth" in t:
        return 400
    if any(k in t for k in ["foundation", "graph", "intelligence", "council", "aios", "neural"]):
        return 1000
    if any(k in t for k in ["agent", "protocol", "oauth", "contribution system", "backup freshness"]):
        return 900
    if any(k in t for k in ["onboarding", "feedback", "shell", "social", "reward", "xp", "cp"]):
        return 800
    if any(k in t for k in ["security", "secret", "guard", "sustainability", "payment"]):
        return 700
    return 700


def ltv_monthly_rate(contribution_type: str, title: str, cp: int) -> int:
    """Lifetime-value matrix for imported GitHub work.

    Base recurring value comes from contribution type, then strategic work gets
    a higher monthly rate so durable architecture/agent/graph contributions keep
    accruing CP while they continue serving the product.
    """
    base = {
        "code": 30,
        "agent": 50,
        "design": 20,
        "doc": 10,
        "research": 10,
        "feedback": 5,
        "community": 15,
    }.get(contribution_type, 10)
    t = title.lower()
    strategic_multiplier = 1.0
    if any(k in t for k in ["graph", "neural", "intelligence", "aios", "council", "foundation"]):
        strategic_multiplier = 2.0
    elif any(k in t for k in ["agent", "protocol", "backup", "survival", "model evolution"]):
        strategic_multiplier = 1.7
    elif any(k in t for k in ["feedback", "onboarding", "social", "reward", "contribution"]):
        strategic_multiplier = 1.4
    cp_multiplier = max(1.0, min(2.0, cp / 700.0))
    return max(5, int(round(base * strategic_multiplier * cp_multiplier)))


def fetch_merged_pr_contributions() -> tuple[list[dict[str, Any]], set[str]]:
    contributions: list[dict[str, Any]] = []
    merge_shas: set[str] = set()
    for repo in REPOS:
        pulls = fetch_all_pages(f"/repos/{OWNER}/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc"})
        for pr in pulls:
            if not pr.get("merged_at"):
                continue
            title = pr.get("title") or f"Merged PR #{pr['number']}"
            cp = cp_for(title, "pr")
            if pr.get("merge_commit_sha"):
                merge_shas.add(pr["merge_commit_sha"])
            contributions.append({
                "kind": "github_pr",
                "repo": repo,
                "number": pr["number"],
                "title": title,
                "description": f"Merged GitHub PR #{pr['number']} in {repo}: {title}",
                "url": pr.get("html_url"),
                "source_id": f"github_pr:{repo}:{pr['number']}",
                "cp": cp,
                "merged_at": pr.get("merged_at"),
            })
    return contributions, merge_shas


def fetch_direct_commit_contributions(merge_shas: set[str]) -> list[dict[str, Any]]:
    contributions: list[dict[str, Any]] = []
    for repo in REPOS:
        commits = fetch_all_pages(f"/repos/{OWNER}/{repo}/commits", {"sha": "main", "since": IMPORT_SINCE})
        for item in commits:
            sha = item.get("sha", "")
            if not sha or sha in merge_shas:
                continue
            commit = item.get("commit") or {}
            message = (commit.get("message") or "").splitlines()[0]
            if not message or message.startswith("[Aura] Identity pack backup"):
                continue
            author = item.get("author") or {}
            commit_author = commit.get("author") or {}
            login = author.get("login")
            email = (commit_author.get("email") or "").lower()
            if login != AUTHOR_LOGIN and email not in AUTHOR_EMAILS:
                continue
            cp = cp_for(message, "commit")
            if cp <= 0:
                continue
            contributions.append({
                "kind": "github_commit",
                "repo": repo,
                "number": sha[:8],
                "title": message,
                "description": f"Direct main-branch commit in {repo}: {message}",
                "url": item.get("html_url"),
                "source_id": f"github_commit:{repo}:{sha}",
                "cp": cp,
                "merged_at": commit_author.get("date"),
            })
    return contributions


def tier_for_cp(total_cp: int) -> str:
    if total_cp >= 3000:
        return "founding_steward"
    if total_cp >= 1000:
        return "core_contributor"
    if total_cp >= 500:
        return "contributor"
    if total_cp >= 100:
        return "builder"
    return "observer"


def ledger_ref(item: dict[str, Any]) -> str:
    """Short stable reference for ledger tables with legacy varchar limits."""
    if item["kind"] == "github_pr":
        return f"ghpr:{item['repo']}:{item['number']}"
    return f"ghcommit:{item['repo']}:{item['number']}"


def one(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()


def ensure_schema(cur):
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_avatar_url TEXT")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_connected BOOLEAN DEFAULT FALSE")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_github ON users(github_username)")
    cur.execute("ALTER TABLE contributors ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_contributors_user_id ON contributors(user_id)")
    cur.execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)")
    cur.execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS external_link TEXT")
    cur.execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual'")
    cur.execute("ALTER TABLE contributions ADD COLUMN IF NOT EXISTS source_id TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contributions_source_id ON contributions(source_id) WHERE source_id IS NOT NULL")


def get_user_and_contributor(cur):
    user = one(cur, """
        SELECT id, email, created_at
        FROM users
        WHERE email = %s
        ORDER BY created_at ASC
        LIMIT 1
    """, ("carlosandromeda8@gmail.com",))
    if not user:
        user = one(cur, """
            SELECT id, email, created_at
            FROM users
            ORDER BY created_at ASC
            LIMIT 1
        """)
    if not user:
        raise RuntimeError("No users found to assign contributions to")

    user_id, user_email, _ = user
    cur.execute("""
        UPDATE users
        SET github_username = %s,
            github_avatar_url = %s,
            github_connected = TRUE
        WHERE id = %s
    """, (OWNER, f"https://github.com/{OWNER}.png?size=120", user_id))

    cur.execute("""
        INSERT INTO contributors (github_username, display_name, email, avatar_url, user_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (github_username) DO UPDATE SET
            display_name = COALESCE(EXCLUDED.display_name, contributors.display_name),
            email = COALESCE(EXCLUDED.email, contributors.email),
            avatar_url = COALESCE(EXCLUDED.avatar_url, contributors.avatar_url),
            user_id = COALESCE(EXCLUDED.user_id, contributors.user_id)
        RETURNING id
    """, (OWNER, "Aviel Carlos", user_email, f"https://github.com/{OWNER}.png?size=120", user_id))
    contributor_id = cur.fetchone()[0]
    return user_id, user_email, contributor_id


def import_contribution(cur, contributor_id, user_id, item: dict[str, Any]) -> tuple[str, Any | None]:
    url = item["url"]
    source_id = ledger_ref(item)
    existing = one(cur, """
        SELECT id FROM contributions
        WHERE source_id = %s OR github_pr_url = %s OR external_link = %s
        LIMIT 1
    """, (source_id, url, url))
    contribution_type = "code" if item["kind"] in {"github_pr", "github_commit"} else "custom"
    monthly_rate = ltv_monthly_rate(contribution_type, item["title"], int(item["cp"]))
    if existing:
        cur.execute("""
            UPDATE contributions
            SET ltv_monthly_rate = GREATEST(COALESCE(ltv_monthly_rate, 0), %s),
                is_ltv_active = TRUE,
                impact_data = COALESCE(impact_data, '{}'::jsonb) || %s::jsonb
            WHERE id = %s
        """, (monthly_rate, Json({"ltv_monthly_rate": monthly_rate, "ltv_matrix": "base contribution type × strategic durability × CP scope"}), existing[0]))
        return "skipped_existing", existing[0]

    cur.execute("""
        INSERT INTO contributions (
            contributor_id, user_id, contribution_type, title, description,
            github_pr_url, external_link, source, source_id, status,
            base_cp, multiplier, final_cp, aura_evaluation, impact_data,
            ltv_monthly_rate, is_ltv_active, ltv_last_evaluated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'github_import', %s, 'approved', %s, 1.0, %s, %s, %s, %s, TRUE, NOW())
        RETURNING id
    """, (
        contributor_id,
        user_id,
        contribution_type,
        item["title"],
        item["description"],
        url if item["kind"] == "github_pr" else None,
        url,
        source_id,
        item["cp"],
        item["cp"],
        f"Approved {item['kind'].replace('_', ' ')} imported into DAO contribution tracking with the lifetime-value matrix applied.",
        Json({
            "source": item["kind"],
            "repo": item["repo"],
            "identifier": item["number"],
            "cp_awarded": item["cp"],
            "url": url,
            "ltv_monthly_rate": monthly_rate,
            "ltv_matrix": "base contribution type × strategic durability × CP scope",
        }),
        monthly_rate,
    ))
    return "inserted", cur.fetchone()[0]


def insert_ledgers(cur, contributor_id, user_id, contribution_id, item: dict[str, Any]) -> dict[str, int]:
    cp = int(item["cp"])
    reason = f"Approved {item['kind'].replace('_', ' ')}: {item['repo']}#{item['number']} — {item['title']}"
    out = {"cp_ledger": 0, "cp_transactions": 0, "xp_log": 0}
    if not one(cur, "SELECT 1 FROM cp_ledger WHERE contribution_id = %s LIMIT 1", (contribution_id,)):
        cur.execute("INSERT INTO cp_ledger (contributor_id, contribution_id, cp_amount, reason) VALUES (%s, %s, %s, %s)", (contributor_id, contribution_id, cp, reason))
        out["cp_ledger"] = 1
    if not one(cur, "SELECT 1 FROM cp_transactions WHERE reference_id = %s LIMIT 1", (ledger_ref(item),)):
        cur.execute("INSERT INTO cp_transactions (user_id, amount, reason, reference_id) VALUES (%s, %s, %s, %s)", (user_id, cp, reason, ledger_ref(item)))
        out["cp_transactions"] = 1
    short_ref = ledger_ref(item)
    if not one(cur, "SELECT 1 FROM xp_log WHERE ref_id = %s LIMIT 1", (short_ref,)):
        cur.execute(
            "INSERT INTO xp_log (user_id, amount, reason, ref_id) VALUES (%s, %s, %s, %s)",
            (user_id, cp, reason[:80], short_ref),
        )
        out["xp_log"] = 1
    return out


def recalc_totals(cur, contributor_id, user_id):
    cur.execute("SELECT COALESCE(SUM(cp_amount), 0) FROM cp_ledger WHERE contributor_id = %s", (contributor_id,))
    contributor_total_cp = int(cur.fetchone()[0] or 0)
    cur.execute("UPDATE contributors SET total_cp = %s, tier = %s WHERE id = %s", (contributor_total_cp, tier_for_cp(contributor_total_cp), contributor_id))

    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM cp_transactions WHERE user_id = %s", (user_id,))
    cp_balance = int(cur.fetchone()[0] or 0)
    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM cp_transactions WHERE user_id = %s AND amount > 0", (user_id,))
    total_cp_earned = int(cur.fetchone()[0] or 0)
    cur.execute("""
        INSERT INTO user_cp_balance (user_id, cp_balance, total_cp_earned, last_updated)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            cp_balance = EXCLUDED.cp_balance,
            total_cp_earned = EXCLUDED.total_cp_earned,
            last_updated = NOW()
    """, (user_id, cp_balance, total_cp_earned))
    cur.execute("UPDATE users SET dao_cp = %s WHERE id = %s", (total_cp_earned, user_id))
    cur.execute("SELECT COALESCE(SUM(amount), 0) FROM xp_log WHERE user_id = %s", (user_id,))
    total_xp = int(cur.fetchone()[0] or 0)
    return contributor_total_cp, total_cp_earned, total_xp


def main():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    prs, merge_shas = fetch_merged_pr_contributions()
    commits = fetch_direct_commit_contributions(merge_shas)
    items = sorted(prs + commits, key=lambda i: (i.get("merged_at") or "", i["repo"], str(i["number"])))

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        ensure_schema(cur)
        user_id, user_email, contributor_id = get_user_and_contributor(cur)

        stats = {
            "requested_contributions": len(items),
            "requested_prs": len(prs),
            "requested_direct_commits": len(commits),
            "requested_cp_total": sum(int(i["cp"]) for i in items),
            "inserted_contributions": 0,
            "skipped_existing": 0,
            "cp_ledger_rows_inserted": 0,
            "cp_transactions_inserted": 0,
            "xp_log_rows_inserted": 0,
        }

        for item in items:
            status, contribution_id = import_contribution(cur, contributor_id, user_id, item)
            if status == "inserted":
                stats["inserted_contributions"] += 1
            else:
                stats["skipped_existing"] += 1
            ledger = insert_ledgers(cur, contributor_id, user_id, contribution_id, item)
            stats["cp_ledger_rows_inserted"] += ledger["cp_ledger"]
            stats["cp_transactions_inserted"] += ledger["cp_transactions"]
            stats["xp_log_rows_inserted"] += ledger["xp_log"]

        contributor_total_cp, total_cp_earned, total_xp = recalc_totals(cur, contributor_id, user_id)
        conn.commit()
        stats.update({
            "user": {"id": str(user_id), "email": user_email, "github_username": OWNER},
            "contributor_id": str(contributor_id),
            "contributor_total_cp": contributor_total_cp,
            "user_total_cp_earned": total_cp_earned,
            "user_total_xp": total_xp,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        })
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
