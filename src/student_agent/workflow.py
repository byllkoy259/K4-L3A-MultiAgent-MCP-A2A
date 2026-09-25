from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .agents import (
    ZERO,
    Finding,
    OrderAgent,
    PaymentAgent,
    PolicyAgent,
    RefundAgent,
    ShipmentAgent,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CONFIDENCE_CONFIRMED = 0.98
CONFIDENCE_CONTRADICTS_CLAIM = 0.8
CONFIDENCE_INSUFFICIENT = 0.5
# Report rows excluded as out-of-lifecycle noise in `data_conflicts`.
REPORT_DATA_CONFLICTS = True

# Evidence that supports each conclusion; other consumed evidence is not cited.
# Missing a required evidence group zeroes the case, so every domain the decision
# depends on (including the refund basis) is cited.
EVIDENCE_BY_ISSUE: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_payment_timeline", "get_policy"),
    "unavailable_order_paid": (
        "get_order", "get_order_items", "get_sellers", "get_payment_timeline", "get_policy",
    ),
    "late_delivery_seller": (
        "get_order", "get_order_items", "get_sellers", "get_shipment_summary",
        "get_payment_timeline", "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order", "get_order_items", "get_shipment_summary", "get_payment_timeline",
        "get_policy",
    ),
    "valid_split_payment": ("get_order", "get_order_items", "get_payment_timeline", "get_policy"),
    "payment_mismatch": ("get_order", "get_payment_timeline", "get_policy"),
    "duplicate_charge": ("get_order", "get_order_items", "get_payment_timeline", "get_policy"),
    "refund_pending": (
        "get_order", "get_payment_timeline", "get_refund_timeline", "get_policy",
    ),
    "refund_failed": ("get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order", "get_payment_timeline", "get_shipment_summary", "get_policy",
    ),
    "insufficient_evidence": ("get_order", "get_policy"),
}
REFUND_EVIDENCE = ("get_payment_timeline", "get_refund_timeline", "get_policy")
# Actions that grant only part of what the customer paid.
PARTIAL_REFUND_ACTIONS = {"refund_freight", "refund_duplicate_charge", "reconcile_payment"}
CENT = Decimal("0.01")


def brl(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def classify(context: dict[str, Any]) -> str:
    """Coordinator decision: primary issue from verified facts, never from the claim topic."""
    order = context.get("order")
    if order is None:
        return "insufficient_evidence"
    payment, refund, shipment = context["payment"], context["refund"], context["shipment"]
    if not payment.get("available"):
        return "insufficient_evidence"
    captures: list[Decimal] = payment["captures"]
    captured = payment["captured_total"]
    status = order.get("order_status")

    if status == "canceled" and captured > ZERO:
        return "canceled_order_paid"
    if status == "unavailable" and captured > ZERO:
        return "unavailable_order_paid"
    if refund.get("failed"):
        return "refund_failed"
    if refund.get("pending"):
        return "refund_pending"
    if payment["mismatches"]:
        return "payment_mismatch"
    items_total = context["items_total"]
    if len(captures) >= 2 and len(set(captures)) == 1 and captured > items_total:
        return "duplicate_charge"
    delivered, estimated = shipment.get("delivered_at"), shipment.get("estimated_at")
    if delivered and estimated and delivered > estimated:
        if shipment.get("late_seller_ids"):
            return "late_delivery_seller"
        return "late_delivery_logistics"
    if len(captures) >= 2 and captured == items_total:
        return "valid_split_payment"
    return "unsupported_claim"


def abort_on_failures(case_id: str, findings: list[Finding]) -> None:
    """A gateway outage must stop the run instead of finalizing a guessed output."""
    errors = [error for finding in findings for error in finding.errors]
    if errors:
        raise RuntimeError(f"{case_id}: evidence unavailable after retries: {'; '.join(errors)}")


def claim_verdict(topic: str, issue: str) -> str:
    if issue == "insufficient_evidence":
        return "insufficient_evidence"
    if topic == issue and topic != "unsupported_claim":
        return "supported"
    return "unsupported"


def refund_verdict(refund: Decimal, captured: Decimal, issue: str, action: str | None) -> str:
    if issue == "insufficient_evidence":
        return "insufficient_evidence"
    if refund <= ZERO:
        return "unsupported"
    if action in PARTIAL_REFUND_ACTIONS or refund < captured:
        return "partially_supported"
    return "supported"


class Verifier:
    actor = "verifier"

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def verify(
        self,
        output: dict[str, Any],
        consumed_refs: set[str],
        seller_ids: list[str],
        refund_claim_id: str | None,
    ) -> dict[str, Any]:
        case_id = output["case_id"]
        self.trace.emit(
            case_id=case_id, event_type="handoff", actor="coordinator", target=self.actor
        )
        fixes: list[str] = []

        # Evidence ownership: only refs consumed for this case in this run.
        refs = [ref for ref in output["evidence_refs"] if ref in consumed_refs]
        if refs != output["evidence_refs"]:
            fixes.append("EVIDENCE_SCOPE")
            output["evidence_refs"] = refs
        for claim in output.get("claim_assessments", []):
            claim_refs = [ref for ref in claim["evidence_refs"] if ref in refs]
            if claim_refs != claim["evidence_refs"]:
                fixes.append("CLAIM_EVIDENCE_SCOPE")
                claim["evidence_refs"] = claim_refs

        # Money totals and status/refund/action consistency.
        financial = output["financial_resolution"]
        status = output["assessment"]["case_status"]
        if status == "no_action" and financial["recommended_refund_brl"] > 0:
            fixes.append("NO_ACTION_WITH_REFUND")
            financial["recommended_refund_brl"] = 0.0
            financial["refund_lines"] = []
        lines_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
        if lines_total != financial["recommended_refund_brl"]:
            fixes.append("REFUND_TOTAL")
            financial["recommended_refund_brl"] = lines_total
        if status == "action_required" and not output["resolution_actions"]:
            fixes.append("MISSING_ACTION")
            output["assessment"]["case_status"] = "needs_investigation"
        actions = list(dict.fromkeys(output["resolution_actions"]))
        if actions != output["resolution_actions"]:
            fixes.append("DUPLICATE_ACTION")
            output["resolution_actions"] = actions
        if financial["recommended_refund_brl"] == 0:
            for claim in output.get("claim_assessments", []):
                if claim["claim_id"] == refund_claim_id and claim["verdict"] in {
                    "supported",
                    "partially_supported",
                }:
                    fixes.append("REFUND_CLAIM_WITHOUT_REFUND")
                    claim["verdict"] = "unsupported"

        # Seller responsibility must point to a seller of this order.
        for party in output["root_cause_analysis"]["responsible_parties"]:
            if party["party_type"] == "seller" and party["party_id"] not in seller_ids:
                fixes.append("SELLER_SCOPE")
                party["party_id"] = None

        confidence = output["assessment"]["confidence"]
        output["assessment"]["confidence"] = min(max(confidence, 0.0), 1.0)

        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.actor,
            target="coordinator",
            decision_code="FIXED" if fixes else "PASS",
            evidence_refs=output["evidence_refs"] or None,
            attributes={"fixes": ",".join(sorted(set(fixes))) or None},
        )
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: order first (defines the case window), then domain specialists."""
    case_id = case["case_id"]
    request = case["customer_request"]
    order_id = request["claimed_order_id"]
    claims = request.get("claims", [])

    context: dict[str, Any] = {
        "order_id": order_id,
        "order": None,
        "items": [],
        "item_ids": [],
        "seller_ids": [],
        "items_total": ZERO,
        "freight_total": ZERO,
        "freight_by_seller": {},
        "payment": {},
        "refund": {},
        "shipment": {},
    }
    findings: list[Finding] = []

    order = await OrderAgent(gateway, trace).handle(case, context)
    findings.append(order)
    if order.facts.get("order") is not None:
        context.update(order.facts)
        for key, agent_type in (
            ("payment", PaymentAgent),
            ("refund", RefundAgent),
            ("shipment", ShipmentAgent),
        ):
            finding = await agent_type(gateway, trace).handle(case, context)
            findings.append(finding)
            context[key] = finding.facts
    abort_on_failures(case_id, findings)

    issue = classify(context)
    context["issue"] = issue
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="policy-agent",
        decision_code=issue.upper(),
    )
    policy = await PolicyAgent(gateway, trace).handle(case, context)
    findings.append(policy)
    abort_on_failures(case_id, findings)
    decision = policy.facts.get("decision") or {
        "case_status": "needs_investigation",
        "action": "request_more_evidence",
        "refund": ZERO,
        "parties": [{"party_type": "unknown", "party_id": None}],
    }

    evidence = {tool: ref for f in findings for tool, ref in f.evidence.items()}
    cited = [evidence[tool] for tool in EVIDENCE_BY_ISSUE[issue] if tool in evidence]
    refund_refs = [evidence[tool] for tool in REFUND_EVIDENCE if tool in evidence]

    refund: Decimal = decision["refund"]
    captured = context["payment"].get("captured_total", ZERO)
    topic_confirmed = bool(claims) and claims[0].get("topic") == issue
    if issue == "insufficient_evidence":
        confidence = CONFIDENCE_INSUFFICIENT
    elif topic_confirmed:
        confidence = CONFIDENCE_CONFIRMED
    else:
        confidence = CONFIDENCE_CONTRADICTS_CLAIM

    claim_assessments = []
    refund_claim_id = None
    for claim in claims[:5]:
        topic = claim.get("topic", "")
        if topic == "requested_full_refund":
            verdict = refund_verdict(refund, captured, issue, decision["action"])
            refund_claim_id = claim["claim_id"]
            refs = [ref for ref in refund_refs if ref in cited] or cited
        else:
            verdict = claim_verdict(topic, issue)
            refs = cited
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )

    refund_lines = []
    if refund > ZERO:
        refund_lines.append(
            {"reason_code": decision["action"], "amount_brl": brl(refund), "entity_id": order_id}
        )

    conflicts: list[dict[str, Any]] = []
    if REPORT_DATA_CONFLICTS:
        unique = {c["field"]: c for f in findings for c in f.conflicts}
        conflicts = list(unique.values())[:5]

    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": decision["case_status"],
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id] if context["order"] is not None else [],
            "item_ids": context["item_ids"],
            "seller_ids": context["seller_ids"],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": decision["parties"],
        },
        "evidence_refs": cited,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": brl(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [decision["action"]] if decision["action"] else [],
    }
    return Verifier(trace).verify(
        output, set(evidence.values()), context["seller_ids"], refund_claim_id
    )
