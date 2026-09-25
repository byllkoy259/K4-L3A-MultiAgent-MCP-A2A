from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import synthetic as s
from student_agent.agents import OrderAgent, ToolPermissionError
from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceLedger, EvidenceUnavailable
from student_agent.mcp_gateway import ToolCallError
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_EVENTS = {"case_received", "task_assigned", "handoff", "verification_completed",
                   "case_finalized"}


class FakeGateway:
    def __init__(self, case_id: str, data: dict[str, Any]) -> None:
        self.case_id = case_id
        self.data = data
        self.calls: list[str] = []

    async def call(self, tool: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        assert case_id == self.case_id
        self.calls.append(tool)
        if self.data.get(tool) is None:
            raise ToolCallError(f"MCP tool {tool} failed: not found")
        return s.envelope(case_id, tool, self.data[tool])


def world(order: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    data = {
        "get_order": order,
        "get_order_items": [s.item()],
        "get_sellers": [{"seller_id": s.SELLER, "seller_city": "sao_paulo"}],
        "get_payment_timeline": s.timeline([s.payment("79.00")],
                                           [s.event(s.APPROVED_AT, "79.00")]),
        "get_refund_timeline": None,
        "get_shipment_summary": s.shipment(order, ["2018-01-04T09:00:00-03:00"]),
        "get_policy": s.POLICY,
    }
    data.update(overrides)
    return data


def run(case_id: str, topic: str, data: dict[str, Any], tmp_path: Path):
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(case_id, data)
    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(s.case_input(case_id, topic), gateway, trace))
    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
    contracts.validate_output(output, case_id)
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    return output, events, gateway


def test_canceled_paid_order_is_refunded_with_traceable_evidence(tmp_path: Path) -> None:
    order = s.order(status="canceled", delivered=None)
    output, events, _ = run("CASE_001", "canceled_order_paid", world(order), tmp_path)

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["resolution_actions"] == ["issue_refund"]

    assert {e["event_type"] for e in events} >= REQUIRED_EVENTS
    assert events[0]["event_type"] == "case_received"
    assert events[-1]["event_type"] == "case_finalized"
    consumed = {ref for e in events if e["event_type"] == "tool_result_consumed"
                for ref in e["evidence_refs"]}
    assert set(output["evidence_refs"]) <= consumed
    assert len({e["actor"] for e in events}) >= 5
    verdicts = [e["decision_code"] for e in events if e["event_type"] == "verification_completed"]
    assert verdicts == ["PASS"]


def test_seller_responsibility_uses_this_cases_seller(tmp_path: Path) -> None:
    order = s.order(carrier="2018-01-06T09:00:00-03:00", delivered="2018-01-12T09:00:00-03:00")
    output, _, _ = run("CASE_002", "late_delivery_seller", world(order), tmp_path)

    parties = output["root_cause_analysis"]["responsible_parties"]
    assert parties == [{"party_type": "seller", "party_id": s.SELLER}]
    assert s.SELLER in output["affected_entities"]["seller_ids"]
    assert output["financial_resolution"]["refund_lines"][0]["entity_id"] == s.ITEM


def test_no_action_case_has_no_refund(tmp_path: Path) -> None:
    output, _, _ = run("CASE_003", "unsupported_claim", world(s.order()), tmp_path)

    assert output["assessment"]["case_status"] == "no_action"
    assert output["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []
    }


def test_gateway_error_on_required_tool_is_not_finalized(tmp_path: Path) -> None:
    data = world(s.order(), get_order=None)
    with pytest.raises(EvidenceUnavailable, match="get_order"):
        run("CASE_004", "canceled_order_paid", data, tmp_path)


def test_malformed_payment_evidence_becomes_insufficient_evidence(tmp_path: Path) -> None:
    data = world(s.order(status="canceled", delivered=None),
                 get_payment_timeline={"unexpected": "shape"})
    output, _, _ = run("CASE_006", "canceled_order_paid", data, tmp_path)

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["evidence_refs"]  # still cites what it did read, so it is scorable


def test_specialists_cannot_call_tools_they_do_not_own(tmp_path: Path) -> None:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    agent = OrderAgent("CASE_005", FakeGateway("CASE_005", {}),
                       TraceWriter(tmp_path / "t.jsonl", contracts), EvidenceLedger("CASE_005"))
    with pytest.raises(ToolPermissionError):
        asyncio.run(agent.fetch("get_policy", policy_version="EC_POLICY_V1"))
