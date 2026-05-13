from datetime import datetime, timedelta, timezone
import unittest

from core.action_boundary import (
    ActionBoundaryError,
    ActionBoundaryEvidence,
    ActionType,
    ApprovalMode,
    DataClassification,
    FinalActionResult,
)


class ActionBoundaryEvidenceTests(unittest.TestCase):
    def _evidence(self):
        return ActionBoundaryEvidence(
            user_intent="Send Avi the weekly Aura growth summary",
            action_type=ActionType.SEND,
            target_resource="telegram:user:avi",
            resource_scope="single message to Avi only",
            data_classification=DataClassification.PRIVATE,
            policy_version="approval-policy-v1",
            policy_rule_id="send.private-message.requires-approval",
            approval_mode=ApprovalMode.USER_APPROVAL,
            cost_or_risk="Private message is sent externally to one Telegram recipient.",
            rollback_notes="Delete the Telegram message if supported by the provider.",
            tool="message.send",
            tool_args={
                "target": "telegram:user:avi",
                "message": "summary",
                "metadata": {
                    "token": "do-not-log",
                    "access_token": "oauth-access-token",
                    "refreshToken": "oauth-refresh-token",
                    "clientSecret": "oauth-client-secret",
                    "headers": {"set-cookie": "session-cookie"},
                    "api_version": "2026-05-11",
                    "monkey": "banana",
                },
            },
        )

    def test_allows_execution_when_boundary_matches_approval(self):
        evidence = self._evidence().bind_approval(
            approver_ref="user-hash:avi",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            resume_ttl=timedelta(minutes=2),
        )

        evidence.assert_execution_allowed()
        evidence.worker_resume_token.consume(evidence.evidence_hash)

        self.assertIsNotNone(evidence.worker_resume_token.used_at)

    def test_rejects_approval_drift_when_args_change(self):
        evidence = self._evidence().bind_approval(
            approver_ref="user-hash:avi",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        drifted = evidence.model_copy(
            update={"tool_args": {**evidence.tool_args, "message": "changed after approval"}}
        )

        with self.assertRaisesRegex(ActionBoundaryError, "approval drift"):
            drifted.assert_execution_allowed()

    def test_rejects_approval_drift_when_policy_context_changes(self):
        evidence = self._evidence().bind_approval(
            approver_ref="user-hash:avi",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        drifted = evidence.model_copy(update={"policy_rule_id": "different-rule"})

        with self.assertRaisesRegex(ActionBoundaryError, "approval drift"):
            drifted.assert_execution_allowed()

    def test_rejects_expired_worker_resume_token(self):
        evidence = self._evidence().bind_approval(
            approver_ref="user-hash:avi",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            resume_ttl=timedelta(minutes=2),
        )

        with self.assertRaisesRegex(ActionBoundaryError, "expired"):
            evidence.worker_resume_token.consume(
                evidence.evidence_hash,
                now=datetime.now(timezone.utc) + timedelta(minutes=3),
            )

    def test_rejects_reused_worker_resume_token(self):
        evidence = self._evidence().bind_approval(
            approver_ref="user-hash:avi",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            resume_ttl=timedelta(minutes=2),
        )

        evidence.worker_resume_token.consume(evidence.evidence_hash)
        with self.assertRaisesRegex(ActionBoundaryError, "already been used"):
            evidence.worker_resume_token.consume(evidence.evidence_hash)

    def test_records_final_result_and_redacts_secrets_from_audit_log(self):
        evidence = self._evidence().bind_approval(
            approver_ref="user-hash:avi",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            resume_ttl=timedelta(minutes=2),
        )
        completed = evidence.record_result(
            FinalActionResult(success=True, external_id="msg_123", rollback_notes="delete msg_123")
        )
        audit = completed.to_audit_log()

        self.assertEqual(audit["final_result"]["external_id"], "msg_123")
        self.assertEqual(audit["policy_rule_id"], "send.private-message.requires-approval")
        self.assertEqual(audit["approval_mode"], "user_approval")
        self.assertEqual(
            audit["cost_or_risk"],
            "Private message is sent externally to one Telegram recipient.",
        )
        self.assertEqual(
            audit["rollback_notes"],
            "Delete the Telegram message if supported by the provider.",
        )
        self.assertEqual(audit["tool_args_redacted"]["metadata"]["token"], "[REDACTED]")
        self.assertEqual(audit["tool_args_redacted"]["metadata"]["access_token"], "[REDACTED]")
        self.assertEqual(audit["tool_args_redacted"]["metadata"]["refreshToken"], "[REDACTED]")
        self.assertEqual(audit["tool_args_redacted"]["metadata"]["clientSecret"], "[REDACTED]")
        self.assertEqual(
            audit["tool_args_redacted"]["metadata"]["headers"]["set-cookie"], "[REDACTED]"
        )
        self.assertEqual(audit["tool_args_redacted"]["metadata"]["api_version"], "2026-05-11")
        self.assertEqual(audit["tool_args_redacted"]["metadata"]["monkey"], "banana")
        self.assertNotIn("do-not-log", str(audit))
        self.assertNotIn("oauth-access-token", str(audit))
        self.assertNotIn("oauth-refresh-token", str(audit))
        self.assertNotIn("oauth-client-secret", str(audit))
        self.assertNotIn("session-cookie", str(audit))


if __name__ == "__main__":
    unittest.main()
