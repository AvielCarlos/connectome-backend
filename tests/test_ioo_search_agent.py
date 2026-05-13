import unittest
from unittest.mock import patch

from aura.agents.ioo_search_agent import build_search_agent_payload
from core.config import settings


class IOOSearchAgentTests(unittest.TestCase):
    def test_brave_search_live_results_are_structured_and_safe(self):
        original_key = settings.BRAVE_SEARCH_API_KEY
        settings.BRAVE_SEARCH_API_KEY = "test-key"

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "web": {
                        "results": [
                            {
                                "title": "Beginner meditation guide",
                                "url": "https://example.org/meditation",
                                "description": "A practical beginner guide.",
                            }
                        ]
                    }
                }

        try:
            with patch("aura.agents.ioo_search_agent.httpx.get", return_value=FakeResponse()) as mock_get:
                payload = build_search_agent_payload(
                    {"title": "Start meditating", "step_type": "digital", "tags": ["wellness"]},
                    {},
                )
        finally:
            settings.BRAVE_SEARCH_API_KEY = original_key

        mock_get.assert_called_once()
        live = [candidate for candidate in payload["candidates"] if candidate["id"] == "brave-web-result-1"]
        self.assertEqual(payload["integrations"]["brave_search"], "available")
        self.assertEqual(payload["mode"], "live_search")
        self.assertEqual(live[0]["source"]["name"], "Brave Search")
        self.assertEqual(live[0]["source"]["type"], "live_web_search_result")
        self.assertEqual(live[0]["next_action"]["requires_confirmation"], False)
        self.assertTrue(live[0]["metadata"]["external_actions_require_confirmation"])
        self.assertNotIn("test-key", str(payload))

    def test_brave_search_without_key_keeps_fallback_query_plan(self):
        original_key = settings.BRAVE_SEARCH_API_KEY
        settings.BRAVE_SEARCH_API_KEY = ""
        try:
            with patch("aura.agents.ioo_search_agent.httpx.get") as mock_get:
                payload = build_search_agent_payload(
                    {"title": "Start meditating", "step_type": "digital", "tags": ["wellness"]},
                    {},
                )
        finally:
            settings.BRAVE_SEARCH_API_KEY = original_key

        mock_get.assert_not_called()
        self.assertEqual(payload["integrations"]["brave_search"], "not_configured_or_no_results")
        self.assertEqual(payload["status"], "fallback_ready")
        self.assertTrue(payload["fallback"]["used"])


if __name__ == "__main__":
    unittest.main()
