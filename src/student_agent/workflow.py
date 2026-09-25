"""Coordinator: routes one case through the specialists, the policy agent and the verifier.

    case_received
      -> order-agent  (+ policy-agent in parallel)      order facts fix the case window
      -> payment-agent + shipment-agent (in parallel)    read only in-window records
      -> policy-agent decides                            policy_decided
      -> verifier checks the draft                       verification_completed
    case_finalized
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from .a2a import A2AChannel
from .agents import OrderAgent, PaymentAgent, PolicyAgent, Resolution, ShipmentAgent
from .analysis import CaseFacts, Diagnosis, OrderFacts, PaymentFacts, ShipmentFacts, parse_time
from .evidence import EvidenceLedger, EvidenceUnavailable
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verifier import CHECKS, verify

COORDINATOR = "coordinator"
VERIFIER = "verifier"
REFUND_REQUEST_TOPIC = "requested_full_refund"
ORDER_TOOLS = ("get_order", "get_order_items", "get_sellers")
PAYMENT_TOOLS = ("get_payment_timeline", "get_refund_timeline")
# Every case has these records; only the refund timeline is legitimately absent.
REQUIRED_TOOLS = (*ORDER_TOOLS, "get_payment_timeline", "get_shipment_summary", "get_policy")

# Evidence each conclusion rests on; only these refs are cited (precision over volume).
CITED_TOOLS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_order_items", "get_payment_timeline"),
    "unavailable_order_paid": (
        "get_order", "get_order_items", "get_sellers", "get_payment_timeline"),
    "late_delivery_seller": (
        "get_order", "get_order_items", "get_sellers", "get_shipment_summary"),
    "late_delivery_logistics": ("get_order", "get_order_items", "get_shipment_summary"),
    "valid_split_payment": ("get_order", "get_order_items", "get_payment_timeline"),
    "payment_mismatch": ("get_order", "get_order_items", "get_payment_timeline"),
    "duplicate_charge": ("get_order", "get_order_items", "get_payment_timeline"),
    "refund_pending": (
        "get_order", "get_order_items", "get_payment_timeline", "get_refund_timeline"),
    "refund_failed": (
        "get_order", "get_order_items", "get_payment_timeline", "get_refund_timeline"),
    "unsupported_claim": (
        "get_order", "get_order_items", "get_payment_timeline", "get_shipment_summary"),
    "insufficient_evidence": ("get_order", "get_order_items"),
}

# How far the policy's action goes towards the customer's "full refund" request.
REFUND_REQUEST_VERDICT = {
    "issue_refund": "supported",
    "retry_refund": "supported",
    "refund_freight": "partially_supported",
    "refund_duplicate_charge": "partially_supported",
    "reconcile_payment": "partially_supported",
    "monitor_refund": "insufficient_evidence",
    "document_no_action": "unsupported",
}

CONFLICT_PENALTY = 0.1


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    return await Coordinator(case, gateway, trace).run()


class Coordinator:
    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter):
        self.case = case
        self.case_id: str = case["case_id"]
        self.trace = trace
        self.ledger = EvidenceLedger(self.case_id)
        self.channel = A2AChannel(self.case_id, trace)
        agent_args = (self.case_id, gateway, trace, self.ledger)
        self.order_agent = OrderAgent(*agent_args)
        self.payment_agent = PaymentAgent(*agent_args)
        self.shipment_agent = ShipmentAgent(*agent_args)
        self.policy_agent = PolicyAgent(*agent_args)

    async def run(self) -> dict[str, Any]:
        request = self.case["customer_request"]
        order_id = str(request["claimed_order_id"])
        claims = list(request.get("claims", []))
        topic = next(
            (c["topic"] for c in claims if c["topic"] != REFUND_REQUEST_TOPIC), "unsupported_claim"
        )
        opened_at = parse_time(self.case["opened_at"])
        policy_version = str(self.case["policy_version"])

        # 1. Order facts define the case window; the policy can load meanwhile.
        self.channel.assign(COORDINATOR, self.order_agent.name, "COLLECT_ORDER_FACTS",
                            attributes={"claim_topic": topic})
        self.channel.assign(COORDINATOR, self.policy_agent.name, "LOAD_POLICY",
                            attributes={"policy_version": policy_version})
        report, policy_loaded = await asyncio.gather(
            self.order_agent.investigate(order_id, opened_at),
            self.policy_agent.load(policy_version),
        )
        order = report.facts
        self.channel.handoff(
            self.order_agent.name, COORDINATOR,
            "ORDER_FACTS_READY" if order else "ORDER_EVIDENCE_MISSING",
            evidence_refs=self.ledger.refs(ORDER_TOOLS),
            attributes=_order_attributes(order),
        )
        self.channel.handoff(
            self.policy_agent.name, COORDINATOR,
            "POLICY_LOADED" if policy_loaded else "POLICY_UNAVAILABLE",
            evidence_refs=self.ledger.refs(["get_policy"]),
        )

        # 2. Payment and shipment specialists only need the window, so they run together.
        payment = shipment = None
        if report.window is not None:
            window_attributes = {"window_start": report.window.start.isoformat(),
                                 "window_end": report.window.end.isoformat()}
            for agent, code in ((self.payment_agent, "COLLECT_PAYMENT_FACTS"),
                                (self.shipment_agent, "COLLECT_SHIPMENT_FACTS")):
                self.channel.assign(COORDINATOR, agent.name, code, attributes=window_attributes)
            payment, shipment = await asyncio.gather(
                self.payment_agent.investigate(order_id, report.window),
                self.shipment_agent.investigate(order_id, report.window),
            )
            self.channel.handoff(
                self.payment_agent.name, COORDINATOR,
                "PAYMENT_FACTS_READY" if payment else "PAYMENT_EVIDENCE_MISSING",
                evidence_refs=self.ledger.refs(PAYMENT_TOOLS),
                attributes=_payment_attributes(payment),
            )
            self.channel.handoff(
                self.shipment_agent.name, COORDINATOR,
                "SHIPMENT_FACTS_READY" if shipment else "SHIPMENT_EVIDENCE_MISSING",
                evidence_refs=self.ledger.refs(["get_shipment_summary"]),
                attributes=_shipment_attributes(shipment),
            )
        # A required tool that errored is an outage, not an answer: retry the whole case
        # rather than finalize one with no evidence (a guaranteed hard gate).
        failed = {t: m for t, m in self.ledger.failures.items() if t in REQUIRED_TOOLS}
        if failed:
            raise EvidenceUnavailable(self.case_id, failed)
        missing = tuple(
            name for name, value in (("order", order), ("payment", payment),
                                     ("shipment", shipment)) if value is None
        )
        facts = CaseFacts(order=order, payment=payment, shipment=shipment, missing=missing)
        conflicts = _data_conflicts(order, shipment)

        # 3. Policy agent turns the facts into a decision.
        self.channel.handoff(
            COORDINATOR, self.policy_agent.name, "FACTS_FOR_DECISION",
            evidence_refs=sorted(self.ledger.all_refs() - set(self.ledger.refs(["get_policy"]))),
            attributes={"claim_topic": topic, "missing": ",".join(missing) or None,
                        "conflicts": len(conflicts)},
        )
        resolution = self.policy_agent.decide(topic, facts, report.seller_ids)
        output = self._build_output(order_id, claims, order, report.seller_ids, resolution,
                                    conflicts)
        self.trace.emit(
            case_id=self.case_id, event_type="policy_decided", actor=self.policy_agent.name,
            decision_code=resolution.diagnosis.primary_issue,
            evidence_refs=self.ledger.refs(["get_policy"]) or None,
            attributes={
                "rule_basis": resolution.diagnosis.decision_code,
                "case_status": resolution.case_status,
                "action": resolution.action,
                "refund_brl": float(resolution.refund),
                "confidence": output["assessment"]["confidence"],
            },
        )
        self.channel.handoff(self.policy_agent.name, VERIFIER, "DRAFT_FOR_VERIFICATION",
                             evidence_refs=output["evidence_refs"])

        # 4. Verifier gates the output; a failed draft is replaced by a safe escalation.
        paid = facts.payment.captured_total if facts.payment else None
        failures = self._verify(output, order_id, paid)
        if failures:
            fallback = Resolution(
                Diagnosis("insufficient_evidence", False, 0.5, "VERIFICATION_FAILED",
                          resolution.diagnosis.signals),
                "needs_investigation", "escalate_manual_review", Decimal(0), (("unknown", None),),
            )
            output = self._build_output(order_id, claims, order, report.seller_ids, fallback,
                                        conflicts)
            failures = self._verify(output, order_id, paid)
        self.channel.handoff(VERIFIER, COORDINATOR, "VERIFIED" if not failures else "REJECTED",
                             evidence_refs=output["evidence_refs"])
        return output

    def _verify(self, output: dict[str, Any], order_id: str, paid: Decimal | None) -> list[str]:
        failures = verify(
            output, case_id=self.case_id, order_id=order_id, ledger=self.ledger,
            validate_schema=lambda value: self.trace.contracts.validate_output(value, "draft"),
            paid_total=paid,
        )
        self.trace.emit(
            case_id=self.case_id, event_type="verification_completed", actor=VERIFIER,
            decision_code="PASS" if not failures else "FAIL",
            evidence_refs=output["evidence_refs"] or None,
            attributes={"checks": len(CHECKS), "failed": ",".join(failures) or None},
        )
        return failures

    def _build_output(
        self,
        order_id: str,
        claims: list[dict[str, Any]],
        order: OrderFacts | None,
        seller_ids: tuple[str, ...],
        resolution: Resolution,
        conflicts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        diagnosis = resolution.diagnosis
        issue = diagnosis.primary_issue
        confidence = round(max(0.05, diagnosis.confidence - CONFLICT_PENALTY * bool(conflicts)), 2)
        policy_refs = self.ledger.refs(["get_policy"])
        issue_refs = self.ledger.refs(CITED_TOOLS[issue])
        evidence_refs = list(dict.fromkeys(issue_refs + policy_refs))

        refund = float(resolution.refund)
        refund_lines = []
        if refund > 0:
            freight_item = resolution.action == "refund_freight" and order and order.items
            refund_lines.append({
                "reason_code": issue.upper(),
                "amount_brl": refund,
                "entity_id": order.items[0].item_id if freight_item else order_id,
            })

        payment_refs = [r for r in self.ledger.refs(PAYMENT_TOOLS) if r in evidence_refs]
        claim_assessments = []
        for claim in claims[:5]:
            if claim["topic"] == REFUND_REQUEST_TOPIC:
                verdict = REFUND_REQUEST_VERDICT.get(
                    resolution.action, "partially_supported" if refund > 0 else "unsupported"
                )
                refs = policy_refs + payment_refs
            elif issue == "insufficient_evidence":
                verdict, refs = "insufficient_evidence", issue_refs
            else:
                supported = diagnosis.claim_supported and claim["topic"] == issue
                verdict, refs = ("supported" if supported else "unsupported"), issue_refs
            claim_assessments.append({
                "claim_id": str(claim["claim_id"]),
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": list(dict.fromkeys(refs)) or evidence_refs[:1],
            })

        return {
            "schema_version": "day09-l3a-output-v2",
            "case_id": self.case_id,
            "assessment": {
                "primary_issue": issue,
                "case_status": resolution.case_status,
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": [order_id],
                "item_ids": list(dict.fromkeys(i.item_id for i in order.items)) if order else [],
                "seller_ids": list(seller_ids),
                "payment_references": [],
                "shipment_ids": [],
            },
            "claim_assessments": claim_assessments,
            "root_cause_analysis": {
                "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
                "responsible_parties": [
                    {"party_type": party_type, "party_id": party_id}
                    for party_type, party_id in resolution.parties
                ],
            },
            "evidence_refs": evidence_refs,
            "data_conflicts": conflicts,
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": refund,
                "refund_lines": refund_lines,
            },
            "resolution_actions": [resolution.action],
        }


def _data_conflicts(
    order: OrderFacts | None, shipment: ShipmentFacts | None
) -> list[dict[str, Any]]:
    """Fields both the order row and the shipment summary report must agree on.

    The selected source is the one the analysis actually relies on for that field:
    order status from the order row, delivery timing from the shipment summary.
    """
    if order is None or shipment is None:
        return []
    pairs = {
        "order_status": (order.status, shipment.status, "get_order"),
        "delivered_carrier_at": (order.carrier_at, shipment.carrier_at, "get_shipment_summary"),
        "delivered_customer_at": (
            order.delivered_at, shipment.delivered_at, "get_shipment_summary"),
        "estimated_delivery_at": (
            order.estimated_at, shipment.estimated_at, "get_shipment_summary"),
    }
    return [
        {
            "field": field,
            "sources": ["get_order", "get_shipment_summary"],
            "selected_source": selected,
            "resolution_code": "PREFER_DOMAIN_SYSTEM_OF_RECORD",
        }
        for field, (from_order, from_shipment, selected) in pairs.items()
        if from_order != from_shipment
    ]


def _order_attributes(order: OrderFacts | None) -> dict[str, Any]:
    if order is None:
        return {}
    return {"order_status": order.status, "items_in_window": len(order.items),
            "items_excluded": order.excluded_items}


def _payment_attributes(payment: PaymentFacts | None) -> dict[str, Any]:
    if payment is None:
        return {}
    return {"captures_in_window": len(payment.captures),
            "refund_events_in_window": len(payment.refunds),
            "open_mismatches": len(payment.open_mismatches),
            "events_excluded": payment.excluded_events}


def _shipment_attributes(shipment: ShipmentFacts | None) -> dict[str, Any]:
    if shipment is None:
        return {}
    return {"delivered_late": shipment.delivered_late,
            "late_sellers": len(shipment.late_sellers),
            "limits_excluded": shipment.excluded_limits}
