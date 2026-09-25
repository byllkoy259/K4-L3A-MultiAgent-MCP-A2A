"""Minimal agent-to-agent (A2A) messaging: every message is correlated by case_id and
traced as an observable event (task_assigned / handoff). Only codes and counts go into
the trace -- never prompts or reasoning text."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .trace import TraceWriter

# The workflow is a fixed DAG of ~12 messages per case; anything beyond this is a loop.
MAX_MESSAGES_PER_CASE = 40

Attributes = dict[str, str | int | float | bool | None]


class MessageLoopError(RuntimeError):
    pass


@dataclass(frozen=True)
class Message:
    case_id: str
    sequence: int
    kind: str  # "task" | "handoff"
    sender: str
    recipient: str
    code: str
    evidence_refs: tuple[str, ...] = ()
    body: Any = field(default=None, compare=False)


class A2AChannel:
    def __init__(self, case_id: str, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.trace = trace
        self.log: list[Message] = []

    def _record(self, message: Message) -> Message:
        if len(self.log) >= MAX_MESSAGES_PER_CASE:
            raise MessageLoopError(f"{self.case_id}: more than {MAX_MESSAGES_PER_CASE} messages")
        self.log.append(message)
        return message

    def assign(
        self, sender: str, recipient: str, code: str, *, body: Any = None,
        attributes: Attributes | None = None,
    ) -> Message:
        message = self._record(Message(
            self.case_id, len(self.log) + 1, "task", sender, recipient, code, body=body
        ))
        self.trace.emit(
            case_id=self.case_id, event_type="task_assigned", actor=sender, target=recipient,
            decision_code=code, attributes={"seq": message.sequence, **(attributes or {})},
        )
        return message

    def handoff(
        self, sender: str, recipient: str, code: str, *, evidence_refs: list[str] | None = None,
        body: Any = None, attributes: Attributes | None = None,
    ) -> Message:
        refs = tuple(dict.fromkeys(evidence_refs or ()))
        message = self._record(Message(
            self.case_id, len(self.log) + 1, "handoff", sender, recipient, code, refs, body
        ))
        self.trace.emit(
            case_id=self.case_id, event_type="handoff", actor=sender, target=recipient,
            decision_code=code, evidence_refs=list(refs) or None,
            attributes={"seq": message.sequence, **(attributes or {})},
        )
        return message
