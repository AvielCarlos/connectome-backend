# Connectome Backend — AI OS for Human Flourishing

Connectome is the nervous system of the Ascension Technologies ecosystem: an AI OS that turns goals, context, places, routines, reflections, and community signals into coordinated action for human flourishing.

This backend is where Aura's brain, agent runtime, IOO Execution Protocol, contribution tracking, CP rewards, and graph intelligence come together.

## Why builders should care

Most AI products are chat boxes. Connectome is infrastructure for an agentic life OS:

- **Agent developers** can build specialized Aura agents that reason over goals, context, venues, routines, feedback, and DAO signals.
- **Backend and infra engineers** can harden a real FastAPI + PostgreSQL/pgvector + Redis system already deployed toward production.
- **Graph and ML engineers** can improve the IOO ontology, embeddings, prerequisite inference, and recommendation loops.
- **Product-minded developers** can ship features that immediately become part of the Path Feed/Aura daily experience.
- **Open-source contributors** earn visible Contribution Points (CP), leaderboard recognition, and a path toward founding steward status in the Ascension DAO.

## Architecture overview

```text
Connectome Backend
├── FastAPI API surface          api/routes/*
├── Aura agent brain              ora/agents/*
├── IOO Execution Protocol       api/routes/ioo*.py + graph/embedding logic
├── DAO + contribution layer     api/routes/dao*.py, github_webhook.py, leaderboard.py
├── Persistence                  PostgreSQL, pgvector, Redis
└── External integrations        GitHub OAuth/webhooks, Google, Stripe, Places, OpenAI/Anthropic
```

Key domains:

- **Aura** — the brain: executive agents, coaching, context, discovery, growth, contribution recruitment.
- **Connectome** — the AI OS / nervous system: APIs, memory, graph, execution protocol, surfaces.
- **Path Feed** — the daily app experience where users interact with Aura.
- **Ascension Technologies** — DAO, governance, Contribution Points, ownership/economic coordination layer.

## Try / share / contribute

We are currently aiming for **10 new users per day** through no-spam inbound growth: useful public assets, clear GitHub issues, SEO/owned pages, and product proof — not cold DMs or blasts.

Useful first actions:

- Try the app: https://avielcarlos.github.io/connectome-web/
- Open the Path Feed and give feedback on confusing or useful cards.
- Share one concrete critique or screenshot with context.
- Builders: pick one narrow issue and comment with your approach before opening a PR.

## Contribute and earn CP

Connectome uses Contribution Points (CP) to recognise meaningful work. CP can support reputation, leaderboard placement, steward invitations, governance weight, and future ecosystem upside as the DAO matures.

Typical CP ranges:

| Contribution | CP range |
| --- | ---: |
| Small docs, bug triage, test fixes | 25–75 CP |
| Good first issue / contained feature | 75–200 CP |
| Agent, backend, or frontend feature | 200–600 CP |
| ML/graph architecture, production infra, major systems | 600–1,500+ CP |

Final CP is based on shipped impact, review quality, maintainability, and whether the work advances the mission.

## Good first areas

- Add or improve API documentation for existing routes.
- Create tests around contribution, DAO rewards, IOO execution, or feedback flows.
- Improve GitHub webhook ingestion and CP attribution.
- Add object storage support for feedback screenshots.
- Build live SearchAgent and UXSelectionAgent foundations for IOO execution.
- Improve pgvector node embeddings and prerequisite inference.
- Instrument developer onboarding analytics.

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

For local development, PostgreSQL and Redis are recommended. Some integrations fall back to mocks or disabled modes when keys are missing.

## Start contributing

1. Read [`CONTRIBUTING.md`](CONTRIBUTING.md).
2. Read [`docs/DEVELOPER_MISSION.md`](docs/DEVELOPER_MISSION.md).
3. Pick an issue labelled `good first issue`, `agent-dev`, `backend`, `ml-graph`, `frontend`, or `growth`.
4. Comment with your intended approach.
5. Open a focused PR and include the CP category you believe applies.

We are looking for builders who want to make AI useful in real human lives — not just impressive in demos.
