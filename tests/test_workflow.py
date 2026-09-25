"""Offline workflow tests with a fake MCP gateway and synthetic (non-competition) data."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent import agents
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
ORDER_ID = "0123456789abcdef0123456789abcdef"
CASE_ID = "TEST_CASE_001"
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}
OUTAGE = object()


def rule(status: str, action: str, refund: float, party: str) -> dict[str, Any]:
    return {
        "case_status": status,
        "recommended_action": action,
        "refund_brl": refund,
        "responsible_parties": [{"party_type": party, "party_id": "seller-sample"}],
    }


POLICY = {
    "currency": "BRL",
    "policy_version": "TEST_POLICY",
    "rules": {
        "canceled_order_paid": rule("action_required", "issue_refund", 0, "platform"),
        "unavailable_order_paid": rule("action_required", "issue_refund", 0, "seller"),
        "late_delivery_seller": rule("action_required", "refund_freight", 0, "seller"),
        "late_delivery_logistics": rule(
            "action_required", "refund_freight", 0, "logistics_provider"
        ),
        "valid_split_payment": rule("no_action", "document_no_action", 0, "customer"),
        "payment_mismatch": rule("action_required", "reconcile_payment", 0, "payment_provider"),
        "duplicate_charge": rule(
            "action_required", "refund_duplicate_charge", 0, "payment_provider"
        ),
        "refund_pending": rule("needs_investigation", "monitor_refund", 0, "payment_provider"),
        "refund_failed": rule("action_required", "retry_refund", 0, "payment_provider"),
        "unsupported_claim": rule("no_action", "document_no_action", 0, "customer"),
    },
}


def item(seller: str, limit: str, price: str = "79.00", freight: str = "10.00") -> dict:
    return {
        "order_id": ORDER_ID,
        "order_item_id": f"item-{seller}",
        "product_id": "product-1",
        "seller_id": seller,
        "shipping_limit_date": limit,
        "price": price,
        "freight_value": freight,
    }


def payment_event(at: str, amount: str, event_type: str = "captured") -> dict:
    status = "open" if event_type == "reconciliation_mismatch" else "confirmed"
    return {
        "order_id": ORDER_ID,
        "event_at": at,
        "event_type": event_type,
        "amount_brl": amount,
        "status": status,
    }


def base_responses() -> dict[str, Any]:
    """A healthy delivered order: nothing is wrong with it."""
    items = [item("seller-a", "2018-01-13T09:00:00-03:00")]
    return {
        "get_order": {
            "order_id": ORDER_ID,
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-01-10T09:00:00-03:00",
            "order_approved_at": "2018-01-10T10:00:00-03:00",
            "order_delivered_carrier_date": "2018-01-12T09:00:00-03:00",
            "order_delivered_customer_date": "2018-01-19T09:00:00-03:00",
            "order_estimated_delivery_date": "2018-01-20T09:00:00-03:00",
        },
        "get_order_items": items,
        "get_payment_timeline": {
            "order_id": ORDER_ID,
            "events": [payment_event("2018-01-10T10:00:00-03:00", "89.00")],
        },
        "get_refund_timeline": None,
        "get_shipment_summary": {
            "order_id": ORDER_ID,
            "delivered_carrier_at": "2018-01-12T09:00:00-03:00",
            "delivered_customer_at": "2018-01-19T09:00:00-03:00",
            "estimated_delivery_at": "2018-01-20T09:00:00-03:00",
            "shipping_limits": [
                {"order_item_id": i["order_item_id"], "seller_id": i["seller_id"],
                 "shipping_limit_at": i["shipping_limit_date"]}
                for i in items
            ],
            "events": [],
        },
        "get_sellers": [
            {"seller_id": seller, "seller_city": "sao_paulo", "seller_state": "SP"}
            for seller in ("seller-a", "seller-late", "seller-ok")
        ],
        "get_policy": POLICY,
    }


class FakeGateway:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append(tool_name)
        data = self.responses.get(tool_name)
        if data is None or data is OUTAGE:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_{len(self.calls):04d}_abcdefgh",
            "result_hash": "sha256:" + "0" * 64,
            "domain": DOMAINS[tool_name],
            "data": copy.deepcopy(data),
        }


def run_case(
    responses: dict[str, Any], topic: str, tmp_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    case = {
        "case_id": CASE_ID,
        "opened_at": "2018-01-25T09:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "synthetic",
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-a", "topic": topic},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "TEST_POLICY",
    }
    output = asyncio.run(solve_case(case, FakeGateway(responses), trace))
    contracts.validate_output(output, "output")
    events = [json.loads(line) for line in trace_path.read_text("utf-8").splitlines()]
    return output, events


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "RETRY_DELAYS", ())


def test_canceled_order_ignores_decoy_capture(tmp_path: Path) -> None:
    responses = base_responses()
    responses["get_order"]["order_status"] = "canceled"
    responses["get_order"]["order_delivered_customer_date"] = None
    responses["get_shipment_summary"]["delivered_customer_at"] = None
    # Decoy capture from another scenario: inside the case window, 9 days after approval.
    responses["get_payment_timeline"]["events"] = [
        payment_event("2018-01-10T10:00:00-03:00", "79.00"),
        payment_event("2018-01-19T10:00:00-03:00", "18.00"),
    ]
    output, events = run_case(responses, "canceled_order_paid", tmp_path)

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert [c["field"] for c in output["data_conflicts"]] == ["payment_events.event_at"]
    consumed = {ref for e in events if e["event_type"] == "tool_result_consumed"
                for ref in e["evidence_refs"]}
    assert set(output["evidence_refs"]) <= consumed
    required = {"task_assigned", "handoff", "policy_decided", "verification_completed"}
    assert required <= {e["event_type"] for e in events}


def test_late_seller_blames_only_the_late_seller(tmp_path: Path) -> None:
    responses = base_responses()
    items = [
        item("seller-late", "2018-01-11T09:00:00-03:00", freight="12.00"),
        item("seller-ok", "2018-01-13T09:00:00-03:00", freight="7.00"),
    ]
    responses["get_order_items"] = items
    responses["get_shipment_summary"]["shipping_limits"] = [
        {"order_item_id": i["order_item_id"], "seller_id": i["seller_id"],
         "shipping_limit_at": i["shipping_limit_date"]}
        for i in items
    ]
    responses["get_order"]["order_delivered_customer_date"] = "2018-01-22T09:00:00-03:00"
    responses["get_shipment_summary"]["delivered_customer_at"] = "2018-01-22T09:00:00-03:00"
    output, _ = run_case(responses, "late_delivery_seller", tmp_path)

    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": "seller-late"}
    ]
    assert output["financial_resolution"]["recommended_refund_brl"] == 12.0
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"
    cited_tools = {ref.split("_")[1] + "_" + ref.split("_")[2] for ref in output["evidence_refs"]}
    assert {"get_sellers", "get_shipment"} <= cited_tools


def test_logistics_delay_when_seller_handed_off_on_time(tmp_path: Path) -> None:
    responses = base_responses()
    responses["get_order"]["order_delivered_customer_date"] = "2018-01-22T09:00:00-03:00"
    responses["get_shipment_summary"]["delivered_customer_at"] = "2018-01-22T09:00:00-03:00"
    output, _ = run_case(responses, "late_delivery_logistics", tmp_path)

    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["root_cause_analysis"]["responsible_parties"][0]["party_type"] == (
        "logistics_provider"
    )
    assert output["financial_resolution"]["recommended_refund_brl"] == 10.0


def test_false_duplicate_claim_is_a_valid_split(tmp_path: Path) -> None:
    responses = base_responses()
    responses["get_payment_timeline"]["events"] = [
        payment_event("2018-01-10T10:00:00-03:00", "44.50"),
        payment_event("2018-01-10T11:00:00-03:00", "44.50"),
    ]
    output, _ = run_case(responses, "duplicate_charge", tmp_path)

    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["assessment"]["confidence"] < 0.9
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["financial_resolution"] == {
        "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []
    }


def test_duplicate_charge_refunds_the_extra_capture(tmp_path: Path) -> None:
    responses = base_responses()
    responses["get_payment_timeline"]["events"] = [
        payment_event("2018-01-10T10:00:00-03:00", "89.00"),
        payment_event("2018-01-10T11:00:00-03:00", "89.00"),
    ]
    output, _ = run_case(responses, "duplicate_charge", tmp_path)

    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    assert output["financial_resolution"]["recommended_refund_brl"] == 89.0
    assert output["claim_assessments"][0]["verdict"] == "supported"


def test_failed_refund_inside_window_is_retried(tmp_path: Path) -> None:
    responses = base_responses()
    responses["get_refund_timeline"] = {
        "order_id": ORDER_ID,
        "events": [
            {"order_id": ORDER_ID, "event_at": "2018-01-21T09:00:00-03:00",
             "event_type": "refund_requested", "amount_brl": "89.00", "status": "failed"},
            # Decoy from after the case was opened.
            {"order_id": ORDER_ID, "event_at": "2018-03-01T09:00:00-03:00",
             "event_type": "refund_requested", "amount_brl": "50.00", "status": "pending"},
        ],
    }
    output, _ = run_case(responses, "refund_failed", tmp_path)

    assert output["assessment"]["primary_issue"] == "refund_failed"
    assert output["resolution_actions"] == ["retry_refund"]
    assert output["financial_resolution"]["recommended_refund_brl"] == 89.0


def test_healthy_order_makes_the_claim_unsupported(tmp_path: Path) -> None:
    output, _ = run_case(base_responses(), "late_delivery_logistics", tmp_path)

    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["assessment"]["case_status"] == "no_action"
    assert [c["verdict"] for c in output["claim_assessments"]] == ["unsupported", "unsupported"]


def test_gateway_outage_aborts_instead_of_guessing(tmp_path: Path) -> None:
    responses = base_responses()
    responses["get_shipment_summary"] = OUTAGE
    with pytest.raises(RuntimeError, match="evidence unavailable"):
        run_case(responses, "late_delivery_seller", tmp_path)


def test_trace_of_a_failed_case_attempt_is_discarded(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    trace.begin_case()
    trace.emit(case_id=CASE_ID, event_type="case_received", actor="coordinator")
    trace.rollback_case()
    assert not (tmp_path / "trace.jsonl").exists()

    trace.begin_case()
    trace.emit(case_id=CASE_ID, event_type="case_received", actor="coordinator")
    trace.emit(case_id=CASE_ID, event_type="case_finalized", actor="coordinator")
    trace.commit_case()
    lines = (tmp_path / "trace.jsonl").read_text("utf-8").splitlines()
    assert [json.loads(line)["event_type"] for line in lines] == ["case_received", "case_finalized"]
