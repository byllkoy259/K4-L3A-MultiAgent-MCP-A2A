"""Verifier: deterministic invariants every output must satisfy before it is finalized."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from .evidence import EvidenceLedger

MONEY_TOLERANCE = Decimal("0.01")
CHECKS = (
    "SCHEMA", "CASE_ID", "ENTITY_SCOPE", "EVIDENCE_OWNERSHIP", "CLAIM_LINKAGE",
    "MONEY_TOTAL", "REFUND_CAP", "STATUS_REFUND", "STATUS_ACTION", "DUPLICATE_ACTIONS",
    "SELLER_RESPONSIBILITY", "CONFIDENCE_BOUNDS",
)


def verify(
    output: dict[str, Any],
    *,
    case_id: str,
    order_id: str,
    ledger: EvidenceLedger,
    validate_schema: Callable[[dict[str, Any]], None],
    paid_total: Decimal | None,
) -> list[str]:
    """Return the names of failed checks (empty list = pass)."""
    try:
        validate_schema(output)
    except ValueError:
        return ["SCHEMA"]
    failures: list[str] = []
    if output["case_id"] != case_id:
        failures.append("CASE_ID")

    entities = output["affected_entities"]
    if entities["order_ids"] != [order_id]:
        failures.append("ENTITY_SCOPE")

    refs = set(output["evidence_refs"])
    if not refs or not refs <= ledger.all_refs():
        failures.append("EVIDENCE_OWNERSHIP")
    if any(not set(c["evidence_refs"]) <= refs for c in output.get("claim_assessments", [])):
        failures.append("CLAIM_LINKAGE")

    money = output["financial_resolution"]
    refund = Decimal(str(money["recommended_refund_brl"]))
    lines = sum((Decimal(str(line["amount_brl"])) for line in money["refund_lines"]), Decimal(0))
    if abs(lines - refund) > MONEY_TOLERANCE:
        failures.append("MONEY_TOTAL")
    if paid_total is not None and refund > paid_total + MONEY_TOLERANCE:
        failures.append("REFUND_CAP")

    status = output["assessment"]["case_status"]
    actions = output["resolution_actions"]
    # Money only moves when action is required; every status still records an action
    # (e.g. document_no_action), so the case is never left without a next step.
    if status != "action_required" and (refund > 0 or money["refund_lines"]):
        failures.append("STATUS_REFUND")
    if not actions:
        failures.append("STATUS_ACTION")
    if len(actions) != len(set(actions)):
        failures.append("DUPLICATE_ACTIONS")

    sellers = set(entities["seller_ids"])
    if any(
        party["party_type"] == "seller" and party["party_id"] not in sellers
        for party in output["root_cause_analysis"]["responsible_parties"]
    ):
        failures.append("SELLER_RESPONSIBILITY")

    confidences = [output["assessment"]["confidence"]] + [
        c["confidence"] for c in output.get("claim_assessments", [])
    ]
    if any(not 0 <= value <= 1 for value in confidences):
        failures.append("CONFIDENCE_BOUNDS")
    return failures
