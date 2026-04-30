# IOO Execution Protocol

`POST /api/ioo/execute` turns a selected IOO node into a persisted execution run. The protocol includes:

- `search_agent`: production-shaped candidate discovery with source, confidence, rationale, and next-action metadata.
- `ux_selection_agent`: deterministic ranking over candidate actions, returning scored options, rationale, tradeoffs, and a recommended next action.

Both agents are side-effect free. They may prepare links, search surfaces, prep paths, rankings, and recommendations, but they do not book, purchase, apply, or message anyone without explicit user confirmation.

## UXSelectionAgent demo

Run a standalone example input/output without the API or database:

```bash
python3 examples/ux_selection_demo.py
```

The demo uses the typed `UXSelectionInput` contract and prints ranked options with scores, human-readable rationale, tradeoffs, and the recommended next action. The same ranking function is called from the IOO execution flow after `SearchAgent` prepares candidate actions.

## Example request

```http
POST /api/ioo/execute
Content-Type: application/json
Authorization: Bearer <token>

{
  "node_id": "2f984c1e-5d44-41e4-84e0-1eb89fbf1a75",
  "intent": "do_now"
}
```

## Example response excerpt

```json
{
  "run_id": "9d277fa8-c25b-4a0d-a9aa-f2ef8af9fa9a",
  "status": "plan_ready",
  "protocol": {
    "node_id": "2f984c1e-5d44-41e4-84e0-1eb89fbf1a75",
    "intent": "do_now",
    "execution_agents": [
      {
        "role": "SearchAgent",
        "status": "fallback_ready",
        "candidate_count": 3,
        "fallback_used": true
      },
      {
        "role": "UXSelectionAgent",
        "status": "ranked",
        "ranked_option_count": 3,
        "recommended_option_id": "local-map-search"
      }
    ],
    "search_agent": {
      "role": "SearchAgent",
      "status": "fallback_ready",
      "mode": "query_plan",
      "summary": "Prepared reversible discovery candidates; no booking, purchase, message, or application was performed.",
      "fallback": {
        "used": true,
        "reason": "Live web/Places/Aventi/Eviva provider clients are not wired into this runtime yet; returning provider-ready query links and prep paths instead.",
        "user_safe": true
      },
      "candidates": [
        {
          "id": "local-map-search",
          "title": "Local options for Join a local community initiative",
          "candidate_type": "local_discovery",
          "source": {
            "name": "Google Maps search",
            "type": "maps_query",
            "url": "https://www.google.com/maps/search/?api=1&query=Join+a+local+community+initiative+Eviva+Vancouver%2C+Canada",
            "status": "query_ready"
          },
          "confidence": 0.66,
          "rationale": "Physical or hybrid IOO nodes need nearby, reviewable options before the user commits.",
          "next_action": {
            "label": "Open map results and shortlist 2-3 realistic options",
            "action_type": "open_link",
            "requires_confirmation": false,
            "url": "https://www.google.com/maps/search/?api=1&query=Join+a+local+community+initiative+Eviva+Vancouver%2C+Canada"
          },
          "metadata": {
            "location_used": "Vancouver, Canada",
            "query": "Join a local community initiative Eviva Vancouver, Canada"
          }
        }
      ]
    },
    "ux_selection_agent": {
      "role": "UXSelectionAgent",
      "status": "ranked",
      "objective": "Choose the best execution path for 'Join a local community initiative'",
      "summary": "Ranked 3 option(s) for fit, constraints, friction, fulfilment, and source confidence.",
      "ranked_options": [
        {
          "rank": 1,
          "id": "local-map-search",
          "title": "Local options for Join a local community initiative",
          "score": 72.4,
          "rationale": "Physical or hybrid IOO nodes need nearby, reviewable options before the user commits. Ranked highly for source confidence.",
          "tradeoffs": ["Still needs user confirmation before any irreversible action."],
          "next_action": {
            "label": "Open map results and shortlist 2-3 realistic options",
            "action_type": "open_link",
            "requires_confirmation": false,
            "url": "https://www.google.com/maps/search/?api=1&query=Join+a+local+community+initiative+Eviva+Vancouver%2C+Canada",
            "candidate_id": "local-map-search"
          }
        }
      ],
      "recommended_next_action": {
        "label": "Recommended: Open map results and shortlist 2-3 realistic options",
        "action_type": "open_link",
        "requires_confirmation": false,
        "candidate_id": "local-map-search",
        "ranked_option_id": "local-map-search",
        "score": 72.4
      }
    },
    "safety": {
      "external_actions_require_confirmation": true
    }
  }
}
```
