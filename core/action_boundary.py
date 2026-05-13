"""Action-boundary evidence for approval-gated Aura actions.

This module is intentionally storage-agnostic. It gives approval-gated workers a
canonical evidence envelope they can persist, compare, and audit before any
sensitive external write executes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


class ActionType(str, Enum):
    READ = "read"
    DRAFT = "draft"
    SEND = "send"
    SCHEDULE = "schedule"
    PURCHASE = "purchase"
    DELETE = "delete"
    EXTERNAL_API_WRITE = "external_api_write"


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    SENSITIVE = "sensitive"
    SECRETS = "secrets"
    FINANCIAL = "financial"
    HEALTH = "health"


class ApprovalMode(str, Enum):
    """How an action boundary was authorized to execute."""

    USER_APPROVAL = "user_approval"
    POLICY_PREAPPROVED = "policy_preapproved"
    HUMAN_OPERATOR = "human_operator"


_SECRETISH_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}

_SECRETISH_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "key",
    "password",
    "secret",
    "session",
    "token",
}


class ActionBoundaryError(ValueError):
    """Raised when approval evidence no longer permits execution."""


class FinalActionResult(BaseModel):
    success: bool
    external_id: Optional[str] = None
    message: Optional[str] = None
    rollback_notes: Optional[str] = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalBinding(BaseModel):
    """Exact approval grant bound to one canonical action boundary."""

    tool: str
    args_hash: str
    target_resource: str
    action_type: ActionType
    resource_scope: str
    data_classification: DataClassification
    policy_version: str
    policy_rule_id: str
    approval_mode: ApprovalMode
    approver_ref: str
    expires_at: datetime
    evidence_hash: str

    @field_validator("args_hash", "evidence_hash")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("expected lowercase sha256 hex digest")
        return value

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at


class WorkerResumeToken(BaseModel):
    """Short-lived async worker capability bound to the approval evidence hash."""

    token_id: str = Field(default_factory=lambda: secrets.token_urlsafe(24))
    evidence_hash: str
    expires_at: datetime
    used_at: Optional[datetime] = None

    def consume(self, expected_evidence_hash: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self.used_at is not None:
            raise ActionBoundaryError("worker resume token has already been used")
        if now >= self.expires_at:
            raise ActionBoundaryError("worker resume token has expired")
        if not hmac.compare_digest(self.evidence_hash, expected_evidence_hash):
            raise ActionBoundaryError("worker resume token does not match action evidence")
        self.used_at = now


class ActionBoundaryEvidence(BaseModel):
    """Evidence envelope explaining exactly what an approved action may do."""

    user_intent: str
    action_type: ActionType
    target_resource: str
    resource_scope: str
    data_classification: DataClassification
    policy_version: str
    policy_rule_id: str = "unspecified"
    approval_mode: ApprovalMode = ApprovalMode.USER_APPROVAL
    cost_or_risk: Optional[str] = None
    rollback_notes: Optional[str] = None
    tool: str
    tool_args: Dict[str, Any] = Field(default_factory=dict)
    approval_binding: Optional[ApprovalBinding] = None
    worker_resume_token: Optional[WorkerResumeToken] = None
    final_result: Optional[FinalActionResult] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator(
        "user_intent",
        "target_resource",
        "resource_scope",
        "policy_version",
        "policy_rule_id",
        "tool",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @property
    def args_hash(self) -> str:
        return _sha256(_canonical_json(self.tool_args))

    @property
    def evidence_hash(self) -> str:
        return _sha256(_canonical_json(self._boundary_payload()))

    def _boundary_payload(self) -> Dict[str, Any]:
        """Fields that must not drift after approval."""

        return {
            "user_intent": self.user_intent,
            "action_type": self.action_type.value,
            "target_resource": self.target_resource,
            "resource_scope": self.resource_scope,
            "data_classification": self.data_classification.value,
            "policy_version": self.policy_version,
            "policy_rule_id": self.policy_rule_id,
            "approval_mode": self.approval_mode.value,
            "tool": self.tool,
            "args_hash": self.args_hash,
        }

    def bind_approval(
        self,
        *,
        approver_ref: str,
        expires_at: datetime,
        resume_ttl: Optional[timedelta] = None,
    ) -> "ActionBoundaryEvidence":
        """Attach an approval binding and optional short-lived worker token."""

        binding = ApprovalBinding(
            tool=self.tool,
            args_hash=self.args_hash,
            target_resource=self.target_resource,
            action_type=self.action_type,
            resource_scope=self.resource_scope,
            data_classification=self.data_classification,
            policy_version=self.policy_version,
            policy_rule_id=self.policy_rule_id,
            approval_mode=self.approval_mode,
            approver_ref=approver_ref,
            expires_at=expires_at,
            evidence_hash=self.evidence_hash,
        )
        token = None
        if resume_ttl is not None:
            token = WorkerResumeToken(
                evidence_hash=self.evidence_hash,
                expires_at=min(expires_at, datetime.now(timezone.utc) + resume_ttl),
            )
        return self.model_copy(update={"approval_binding": binding, "worker_resume_token": token})

    def assert_execution_allowed(self, now: Optional[datetime] = None) -> None:
        """Reject execution unless the current boundary exactly matches approval."""

        if self.approval_binding is None:
            raise ActionBoundaryError("missing approval binding")
        binding = self.approval_binding
        if binding.is_expired(now):
            raise ActionBoundaryError("approval binding has expired")
        checks = {
            "tool": self.tool,
            "args_hash": self.args_hash,
            "target_resource": self.target_resource,
            "action_type": self.action_type,
            "resource_scope": self.resource_scope,
            "data_classification": self.data_classification,
            "policy_version": self.policy_version,
            "policy_rule_id": self.policy_rule_id,
            "approval_mode": self.approval_mode,
            "evidence_hash": self.evidence_hash,
        }
        for field, current in checks.items():
            approved = getattr(binding, field)
            if isinstance(current, Enum):
                current = current.value
            if isinstance(approved, Enum):
                approved = approved.value
            if not hmac.compare_digest(str(current), str(approved)):
                raise ActionBoundaryError(f"approval drift detected for {field}")

    def record_result(self, result: FinalActionResult) -> "ActionBoundaryEvidence":
        """Return a copy with immutable final-result evidence attached."""

        return self.model_copy(update={"final_result": result})

    def to_audit_log(self) -> Dict[str, Any]:
        """Safe audit payload: enough to explain, without storing raw secrets."""

        return {
            **self._boundary_payload(),
            "created_at": self.created_at.isoformat(),
            "approval_expires_at": self.approval_binding.expires_at.isoformat()
            if self.approval_binding
            else None,
            "approver_ref": self.approval_binding.approver_ref if self.approval_binding else None,
            "cost_or_risk": self.cost_or_risk,
            "rollback_notes": self.rollback_notes,
            "tool_args_redacted": _redact(self.tool_args),
            "worker_resume_token": {
                "token_id_hash": _sha256(self.worker_resume_token.token_id)
                if self.worker_resume_token
                else None,
                "expires_at": self.worker_resume_token.expires_at.isoformat()
                if self.worker_resume_token
                else None,
                "used": self.worker_resume_token.used_at is not None
                if self.worker_resume_token
                else None,
            },
            "final_result": self.final_result.model_dump(mode="json") if self.final_result else None,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if _is_secretish_key(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_secretish_key(key: str) -> bool:
    """Detect common secret-bearing key names before audit logging.

    Approval evidence may include provider-native argument dictionaries. Those
    APIs often use camelCase, kebab-case, or nested names like access_token,
    refreshToken, set-cookie, and clientSecret. Treating only exact key matches
    as sensitive can leak credentials into otherwise safe audit logs.
    """

    normalized_chars = []
    previous = ""
    for char in key:
        if char.isupper() and previous and (previous.islower() or previous.isdigit()):
            normalized_chars.append(" ")
        normalized_chars.append(char.lower() if char.isalnum() else " ")
        previous = char
    normalized = "".join(normalized_chars)
    compact = normalized.replace(" ", "")
    if compact in _SECRETISH_KEYS:
        return True

    parts = set(normalized.split())
    return bool(parts.intersection(_SECRETISH_KEY_PARTS))
