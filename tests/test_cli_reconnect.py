from __future__ import annotations

import asyncio
import json
import shutil
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2

from student_agent import cli

ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = [f"CASE_{i:03d}" for i in range(1, 101)]
DROP_AT = "CASE_008"


def minimal_output(case_id: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {"primary_issue": "insufficient_evidence",
                       "case_status": "needs_investigation", "confidence": 0.5},
        "affected_entities": {key: [] for key in (
            "order_ids", "item_ids", "seller_ids", "payment_references", "shipment_ids")},
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0,
                                 "refund_lines": []},
        "resolution_actions": ["escalate_manual_review"],
    }


def test_connection_drop_reruns_recent_cases_without_duplicate_events(
    tmp_path: Path, monkeypatch: Any
) -> None:
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    (tmp_path / ".env").write_text(
        "COMPETITION_API_URL=http://127.0.0.1:8081\n"
        "COMPETITION_TEAM_API_KEY=sk-team-test_key_0123456789\n"
        "MCP_ENDPOINT=http://127.0.0.1:8001/mcp\n"
    )
    (tmp_path / "case-set.json").write_text(json.dumps(
        {"case_set_version": "test-v1", "variant_id": "l3a", "case_ids": CASE_IDS}))
    (tmp_path / "inputs").mkdir()
    for case_id in CASE_IDS:
        (tmp_path / "inputs" / f"{case_id}.json").write_text(json.dumps({"case_id": case_id}))

    sessions: list[int] = []
    solved: list[str] = []

    class Gateway:
        async def list_tools(self) -> list[str]:
            return ["get_order"]

    @asynccontextmanager
    async def connect(*_: Any):
        sessions.append(len(sessions) + 1)
        yield Gateway()

    async def solve(case: dict[str, Any], gateway: Any, trace: Any) -> dict[str, Any]:
        if len(sessions) == 1 and case["case_id"] == DROP_AT:
            raise httpx2.RemoteProtocolError("Server disconnected")
        solved.append(case["case_id"])
        trace.emit(case_id=case["case_id"], event_type="task_assigned", actor="coordinator")
        return minimal_output(case["case_id"])

    monkeypatch.setattr(cli, "connect_gateway", connect)
    monkeypatch.setattr(cli, "solve_case", solve)
    asyncio.run(cli._run(tmp_path))

    assert sessions == [1, 2]
    rerun = [case_id for case_id, n in Counter(solved).items() if n == 2]
    assert rerun == [f"CASE_{i:03d}" for i in range(3, 8)]  # the 5 cases before the drop
    events = [json.loads(line)
              for line in (tmp_path / "traces" / "trace.jsonl").read_text().splitlines()]
    per_case = Counter((e["case_id"], e["event_type"]) for e in events)
    assert all(per_case[(case_id, "case_received")] == 1 for case_id in CASE_IDS)
    assert all(per_case[(case_id, "case_finalized")] == 1 for case_id in CASE_IDS)
    assert len(list((tmp_path / "outputs").glob("*.json"))) == 100
