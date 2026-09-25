from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class EvidenceUnavailable(RuntimeError):
    """A tool every case needs answered with an error. In this case set these records
    always exist, so the error is treated as transient and the whole case is retried
    instead of being finalized without evidence (which the scorer hard-gates to 0)."""

    def __init__(self, case_id: str, failures: dict[str, str]) -> None:
        detail = "; ".join(f"{tool}: {message}" for tool, message in failures.items())
        super().__init__(f"{case_id}: required evidence unavailable ({detail})")
        self.case_id = case_id
        self.failures = failures


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool: str
    domain: str
    actor: str
    data: Any


class EvidenceLedger:
    """Every MCP evidence object obtained for ONE case.

    It is the only place refs can be cited from: a ref that was not returned by the
    gateway for this case cannot reach the output, so refs are never invented or reused
    across cases.
    """

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self._by_tool: dict[str, Evidence] = {}
        self.failures: dict[str, str] = {}

    def add(self, evidence: Evidence) -> None:
        self._by_tool[evidence.tool] = evidence

    def fail(self, tool: str, message: str) -> None:
        self.failures[tool] = message

    def ref(self, tool: str) -> str | None:
        evidence = self._by_tool.get(tool)
        return evidence.ref if evidence else None

    def refs(self, tools: tuple[str, ...] | list[str]) -> list[str]:
        """Refs for the given tools that were actually obtained, in a stable order."""
        return [ref for ref in (self.ref(tool) for tool in tools) if ref]

    def all_refs(self) -> set[str]:
        return {evidence.ref for evidence in self._by_tool.values()}
