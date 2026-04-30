# IOO Execution Protocol

`POST /api/ioo/execute` turns a selected IOO node into a persisted execution run. The protocol now includes a production-shaped `search_agent` section: structured candidates with source, confidence, rationale, and next-action metadata.

The SearchAgent is side-effect free. It may prepare links, search surfaces, and prep paths, but it does not book, purchase, apply, or message anyone without explicit user confirmation.

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
    "safety": {
      "external_actions_require_confirmation": true
    }
  }
}
```
