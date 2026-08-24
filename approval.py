"""Draft-and-confirm gates for high-stakes tool execution."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

@dataclass
class ApprovalRequest:
    request_id: str
    action: str
    parameters: dict[str, Any]
    requester_id: str
    created_at: datetime
    expires_at: datetime
    approved: bool = False

class ApprovalGate:
    def __init__(self, ttl_seconds: int = 300): self._requests: dict[str, ApprovalRequest] = {}; self.ttl_seconds = ttl_seconds
    def draft(self, request_id: str, action: str, parameters: dict[str, Any], requester_id: str) -> ApprovalRequest:
        now = datetime.now(timezone.utc); req = ApprovalRequest(request_id, action, dict(parameters), requester_id, now, now + timedelta(seconds=self.ttl_seconds)); self._requests[request_id] = req; return req
    def approve(self, request_id: str, requester_id: str) -> bool:
        req = self._requests.get(request_id)
        if not req or req.requester_id != requester_id or datetime.now(timezone.utc) >= req.expires_at: return False
        req.approved = True; return True
    def consume(self, request_id: str) -> ApprovalRequest:
        req = self._requests.pop(request_id, None)
        if not req or not req.approved or datetime.now(timezone.utc) >= req.expires_at: raise PermissionError("approval required or expired")
        return req
