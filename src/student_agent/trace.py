from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Contracts


class TraceWriter:
    """Append observable workflow events. Never put prompts or chain-of-thought here.

    Between ``begin()`` and ``commit()`` events are validated immediately but held in
    memory, so a case that fails part-way (e.g. the MCP connection drops) can be
    ``rollback()``-ed and re-run without leaving half a lifecycle in the trace file.
    """

    def __init__(self, path: Path, contracts: Contracts) -> None:
        self.path = path
        self.contracts = contracts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pending: list[str] | None = None

    def begin(self) -> None:
        self._pending = []

    def commit(self) -> None:
        pending, self._pending = self._pending or [], None
        self._write(pending)

    def rollback(self) -> None:
        self._pending = None

    def discard_cases(self, case_ids: set[str]) -> None:
        """Remove already-committed events of these cases so they can be re-run."""
        if not self.path.exists():
            return
        kept = [
            line for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line)["case_id"] not in case_ids
        ]
        self.path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")

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
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        if self._pending is not None:
            self._pending.append(line)
        else:
            self._write([line])
        return event

    def _write(self, lines: list[str]) -> None:
        if not lines:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))
