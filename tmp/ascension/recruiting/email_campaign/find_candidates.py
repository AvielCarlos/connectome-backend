#!/usr/bin/env python3
"""
Find new developer candidates from HackerNews 'Who wants to be hired' threads
and GitHub search — adds them to the pool for the email campaign.
"""
import json
import urllib.request
import re
from pathlib import Path
from datetime import datetime, timezone

CAMPAIGN_DIR = Path(__file__).parent
SENT_LOG = CAMPAIGN_DIR / "sent_log.json"
POOL_FILE = CAMPAIGN_DIR / "discovered_candidates.json"

def load_sent_emails():
    if SENT_LOG.exists():
        with open(SENT_LOG) as f:
            d = json.load(f)
        return {s["email"] for s in d.get("sent", []) if s.get("email")}
    return set()

def load_pool():
    if POOL_FILE.exists():
        with open(POOL_FILE) as f:
            return json.load(f)
    return {"candidates": [], "last_updated": None}

def save_pool(pool):
    pool["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(POOL_FILE, "w") as f:
        json.dump(pool, f, indent=2)

def search_hn_who_wants_to_be_hired():
    """Search HN 'Who wants to be hired' for AI/ML engineers"""
    candidates = []
    try:
        # Search for relevant HN posts
        url = "http://hn.algolia.com/api/v1/search?query=AI+engineer+Python+FastAPI+who+wants+to+be+hired&tags=comment&hitsPerPage=20"
        req = urllib.request.urlopen(url, timeout=10)
        data = json.loads(req.read())

        for hit in data.get("hits", []):
            text = hit.get("comment_text", "") or ""
            author = hit.get("author", "")
            # Look for email patterns
            emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', text)
            # Look for AI/ML keywords
            keywords = ["python", "fastapi", "langchain", "pytorch", "llm", "ai engineer", "ml engineer", "machine learning"]
            if any(kw in text.lower() for kw in keywords) and emails:
                for email in emails:
                    candidates.append({
                        "name": author or "Developer",
                        "email": email,
                        "source": "HackerNews who-wants-to-be-hired",
                        "context": "your HackerNews post about your AI engineering background",
                        "role_context": "AI/ML engineering and Python development",
                        "fit_description": "the Ora agent system and backend architecture",
                        "raw_text": text[:200],
                    })
    except Exception as e:
        print(f"HN search error: {e}")
    return candidates

def find_new_candidates():
    sent = load_sent_emails()
    pool = load_pool()
    existing_emails = {c["email"] for c in pool["candidates"] if c.get("email")}

    new_candidates = []

    # Search HN
    hn_candidates = search_hn_who_wants_to_be_hired()
    for c in hn_candidates:
        if c["email"] not in sent and c["email"] not in existing_emails:
            new_candidates.append(c)
            existing_emails.add(c["email"])

    if new_candidates:
        pool["candidates"].extend(new_candidates)
        save_pool(pool)
        print(f"Found {len(new_candidates)} new candidates. Pool size: {len(pool['candidates'])}")
    else:
        print("No new candidates found this run.")

    return new_candidates

if __name__ == "__main__":
    find_new_candidates()
