from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Contracts


class TraceWriter:
    """Append observable workflow events. Never put prompts or chain-of-thought here."""

    def __init__(self, path: Path, contracts: Contracts) -> None:
        self.path = path
        self.contracts = contracts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pending: list[str] | None = None

    def begin_case(self) -> None:
        """Hold events in memory until the case either commits or is rolled back."""
        self._pending = []

    def commit_case(self) -> None:
        lines, self._pending = self._pending or [], None
        if lines:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.writelines(lines)

    def rollback_case(self) -> None:
        """Drop events of a failed attempt so a retried case has one clean lifecycle."""
        self._pending = None

    def emit(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "day09-trace-event-v1",
            "event_id": f"evt_{secrets.token_urlsafe(18)}",
            "case_id": case_id,
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "actor": actor,
        }
        optional = {
            "target": target,
            "decision_code": decision_code,
            "tool_name": tool_name,
            "evidence_refs": evidence_refs,
            "attributes": attributes,
        }
        event.update({key: value for key, value in optional.items() if value is not None})
        self.contracts.validate_trace(event, "trace event")
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        if self._pending is not None:
            self._pending.append(line)
        else:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        return event
