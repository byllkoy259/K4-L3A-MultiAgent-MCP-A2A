from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    def add(self, evidence: Evidence) -> None:
        self._by_tool[evidence.tool] = evidence

    def ref(self, tool: str) -> str | None:
        evidence = self._by_tool.get(tool)
        return evidence.ref if evidence else None

    def refs(self, tools: tuple[str, ...] | list[str]) -> list[str]:
        """Refs for the given tools that were actually obtained, in a stable order."""
        return [ref for ref in (self.ref(tool) for tool in tools) if ref]

    def all_refs(self) -> set[str]:
        return {evidence.ref for evidence in self._by_tool.values()}
