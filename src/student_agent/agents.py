"""Specialist agents. Each owns one evidence domain and may call only its own tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .analysis import (
    CaseFacts,
    CaseWindow,
    Diagnosis,
    InvalidEvidence,
    OrderFacts,
    PaymentFacts,
    ShipmentFacts,
    diagnose,
    order_facts,
    order_window,
    payment_facts,
    shipment_facts,
)
from .evidence import Evidence, EvidenceLedger
from .mcp_gateway import EvidenceGateway, ToolCallError
from .trace import TraceWriter


class ToolPermissionError(PermissionError):
    pass


class Specialist:
    name = "specialist"
    tools: frozenset[str] = frozenset()

    def __init__(
        self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter, ledger: EvidenceLedger
    ) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.ledger = ledger

    async def fetch(self, tool: str, **arguments: str) -> Evidence | None:
        """Call one owned tool. Returns None when the call fails; the failure is recorded
        in the ledger so the coordinator can tell a transient outage from a normal
        absence -- missing evidence is reported, never filled in."""
        if tool not in self.tools:
            raise ToolPermissionError(f"{self.name} may not call {tool}")
        try:
            response = await self.gateway.call(tool, case_id=self.case_id, **arguments)
        except ToolCallError as exc:
            self.ledger.fail(tool, str(exc)[-160:])
            return None
        except TimeoutError:
            self.ledger.fail(tool, "timed out")
            return None
        evidence = Evidence(
            ref=response["evidence_ref"], tool=tool, domain=response["domain"],
            actor=self.name, data=response["data"],
        )
        self.ledger.add(evidence)
        self.trace.emit(
            case_id=self.case_id, event_type="tool_result_consumed", actor=self.name,
            tool_name=tool, evidence_refs=[evidence.ref],
            attributes={"domain": evidence.domain, "warnings": len(response.get("warnings", []))},
        )
        return evidence


@dataclass(frozen=True)
class OrderReport:
    facts: OrderFacts | None
    window: CaseWindow | None
    seller_ids: tuple[str, ...]


class OrderAgent(Specialist):
    name = "order-agent"
    tools = frozenset({"get_order", "get_order_items", "get_sellers"})

    async def investigate(self, order_id: str, opened_at: datetime) -> OrderReport:
        order, items, sellers = await asyncio.gather(
            self.fetch("get_order", order_id=order_id),
            self.fetch("get_order_items", order_id=order_id),
            self.fetch("get_sellers", order_id=order_id),
        )
        if order is None or items is None:
            return OrderReport(None, None, ())
        try:
            window = order_window(order.data, opened_at)
            facts = order_facts(order.data, items.data, window)
        except (InvalidEvidence, KeyError, TypeError):
            return OrderReport(None, None, ())
        if facts.order_id != order_id:
            return OrderReport(None, None, ())
        known = {str(s.get("seller_id")) for s in sellers.data} if sellers else set()
        seller_ids = tuple(dict.fromkeys(
            i.seller_id for i in facts.items if not sellers or i.seller_id in known
        ))
        return OrderReport(facts, window, seller_ids)


class PaymentAgent(Specialist):
    name = "payment-agent"
    tools = frozenset({"get_payment_timeline", "get_refund_timeline"})

    async def investigate(self, order_id: str, window: CaseWindow) -> PaymentFacts | None:
        # No refund record is a normal state (nothing was refunded), not missing evidence.
        timeline, refunds = await asyncio.gather(
            self.fetch("get_payment_timeline", order_id=order_id),
            self.fetch("get_refund_timeline", order_id=order_id),
        )
        if timeline is None:
            return None
        try:
            return payment_facts(timeline.data, refunds.data if refunds else None, window)
        except (InvalidEvidence, KeyError, TypeError):
            return None


class ShipmentAgent(Specialist):
    name = "shipment-agent"
    tools = frozenset({"get_shipment_summary"})

    async def investigate(self, order_id: str, window: CaseWindow) -> ShipmentFacts | None:
        summary = await self.fetch("get_shipment_summary", order_id=order_id)
        if summary is None:
            return None
        try:
            return shipment_facts(summary.data, window)
        except (InvalidEvidence, KeyError, TypeError):
            return None


# --------------------------------------------------------------------- policy


@dataclass(frozen=True)
class Resolution:
    diagnosis: Diagnosis
    case_status: str
    action: str
    refund: Decimal
    parties: tuple[tuple[str, str | None], ...]


INSUFFICIENT = ("needs_investigation", "escalate_manual_review")


class PolicyAgent(Specialist):
    """Loads the machine-readable policy and turns a diagnosis into a resolution."""

    name = "policy-agent"
    tools = frozenset({"get_policy"})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rules: dict[str, Any] | None = None

    async def load(self, policy_version: str) -> bool:
        policy = await self.fetch("get_policy", policy_version=policy_version)
        data = policy.data if policy else None
        if not isinstance(data, dict) or data.get("policy_version") != policy_version:
            return False
        rules = data.get("rules")
        self.rules = rules if isinstance(rules, dict) else None
        return self.rules is not None

    def decide(
        self, claimed_topic: str, facts: CaseFacts, seller_ids: tuple[str, ...]
    ) -> Resolution:
        diagnosis = diagnose(claimed_topic, facts)
        rule = (self.rules or {}).get(diagnosis.primary_issue)
        if rule is None:
            if diagnosis.primary_issue != "insufficient_evidence":
                diagnosis = Diagnosis(
                    "insufficient_evidence", False, min(diagnosis.confidence, 0.6),
                    "NO_POLICY_RULE" if self.rules else "POLICY_UNAVAILABLE", diagnosis.signals,
                )
            status, action = INSUFFICIENT
            return Resolution(diagnosis, status, action, Decimal(0), (("unknown", None),))
        refund = Decimal(str(rule["refund_brl"]))
        if facts.payment is not None:
            refund = min(refund, facts.payment.captured_total)  # never refund more than paid
        return Resolution(
            diagnosis=diagnosis,
            case_status=str(rule["case_status"]),
            action=str(rule["recommended_action"]),
            refund=refund,
            parties=tuple(self._party(p, facts, seller_ids) for p in rule["responsible_parties"]),
        )

    @staticmethod
    def _party(
        party: dict[str, Any], facts: CaseFacts, seller_ids: tuple[str, ...]
    ) -> tuple[str, str | None]:
        """Policy rules name a party type; the concrete seller comes from this case's evidence."""
        party_type = str(party["party_type"])
        if party_type != "seller":
            return party_type, None
        late = facts.shipment.late_sellers if facts.shipment else ()
        seller = next(iter(late or seller_ids), None)
        return ("seller", seller) if seller else ("unknown", None)
