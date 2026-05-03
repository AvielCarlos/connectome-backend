# Contributing to Connectome Backend

Connectome is the AI OS backend for Aura and the Ascension Technologies ecosystem. Contributions should make the system more useful, reliable, intelligent, or easier for other builders to extend.

## Local setup

```bash
git clone https://github.com/AvielCarlos/connectome-backend.git
cd connectome-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Useful checks before opening a PR:

```bash
python -m compileall api core ora main.py
```

If your change touches a route, include a short curl/example request in the PR description when possible.

## No-spam growth principle

Growth work should improve inbound/public/owned surfaces: onboarding, SEO pages, contribution paths, shareable product proof, analytics, and referral loops. Do not add cold DM/email scraping or bulk outreach mechanics. The current growth target is 10 new users/day through trustworthy channels.

## How to pick work

1. Browse open issues.
2. Prefer issues labelled `good first issue`, `agent-dev`, `backend`, `ml-graph`, `growth`, or `docs`.
3. Comment with your intended implementation before starting if the issue is large or ambiguous.
4. Keep PRs focused. One clear improvement beats one sprawling branch.

## Contribution categories

- **Agent development** — Aura agents, IOO execution, context reasoning, search, recommendation, orchestration.
- **Backend/API** — FastAPI routes, data models, auth, integrations, performance, reliability.
- **Graph/ML** — pgvector, embeddings, IOO ontology, prerequisite inference, ranking models.
- **DAO/contribution systems** — CP attribution, GitHub webhooks, rewards, leaderboards, governance primitives.
- **Growth engineering** — onboarding metrics, referral loops, contributor analytics.
- **Docs/devex** — setup, architecture guides, examples, issue quality, API docs.

## CP rewards

Contribution Points recognise meaningful work and help identify founding stewards of the ecosystem.

| Work type | Typical CP |
| --- | ---: |
| Docs polish, small bug, test coverage | 25–75 |
| Good first issue / small feature | 75–200 |
| Production feature or agent improvement | 200–600 |
| Major architecture, ML/graph, infra, security | 600–1,500+ |

CP is awarded after review based on shipped impact, quality, maintainability, and mission alignment. If you believe your work deserves a specific CP range, include it in the PR description.

## Pull request checklist

- [ ] The PR has a clear title and short summary.
- [ ] The change is scoped to one problem.
- [ ] Local checks ran, or the reason they could not run is stated.
- [ ] New config is documented in `.env.example` if needed.
- [ ] User-facing/API behaviour is documented.
- [ ] The PR mentions the related issue and expected CP category.

## Review principles

We value ambitious work, but the backend must remain understandable and operable. Prefer simple, observable systems. Add abstractions only when they make the next contributor faster.
