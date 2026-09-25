from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

TRANSIENT_ERRORS = (httpx2.TransportError, MCPError, TimeoutError)
# Backoff before each retry; a gateway outage lasting longer than this aborts the run.
RETRY_DELAYS = (2.0, 5.0, 15.0, 30.0, 60.0)
ZERO = Decimal("0")
# Authorization-time payment events settle within this delay after order approval.
PAYMENT_SETTLEMENT = timedelta(days=1)
UNDELIVERED_STATUSES = {"shipped", "processing", "invoiced", "approved", "created"}


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return ZERO


def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {json.dumps(row, sort_keys=True): row for row in rows if isinstance(row, dict)}
    return list(unique.values())


@dataclass(frozen=True)
class CaseWindow:
    """Lifecycle window of the claimed order: rows outside it belong to other scenarios."""

    purchased_at: datetime
    approved_at: datetime | None
    opened_at: datetime
    estimated_at: datetime | None

    def contains(self, value: Any) -> bool:
        moment = parse_ts(value)
        return moment is not None and self.purchased_at <= moment <= self.opened_at

    def contains_payment(self, value: Any) -> bool:
        moment = parse_ts(value)
        start = self.approved_at or self.purchased_at
        upper = min(start + PAYMENT_SETTLEMENT, self.opened_at)
        return moment is not None and start <= moment <= upper

    def contains_limit(self, value: Any) -> bool:
        moment = parse_ts(value)
        upper = self.estimated_at or self.opened_at
        return moment is not None and self.purchased_at <= moment <= upper


OUT_OF_LIFECYCLE = "EXCLUDED_OUTSIDE_ORDER_LIFECYCLE"


def conflict(field_name: str, source: str, resolution: str = OUT_OF_LIFECYCLE) -> dict[str, Any]:
    """Rows of `source` contradicting the authoritative order row; the order row wins."""
    return {
        "field": field_name,
        "sources": ["get_order", source],
        "selected_source": "get_order",
        "resolution_code": resolution,
    }


def scoped_events(
    events: list[dict[str, Any]], order_id: str, accepts: Callable[[Any], bool]
) -> list[dict[str, Any]]:
    return [
        event
        for event in dedupe(events)
        if event.get("order_id") == order_id and accepts(event.get("event_at"))
    ]


@dataclass
class Finding:
    """Specialist result handed back to the coordinator."""

    agent: str
    facts: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    # Tools that answered "no records" (only allowed for optional tools).
    not_found: list[str] = field(default_factory=list)
    # Tools that failed after all retries: the case must not be finalized on guesses.
    errors: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


class Specialist:
    actor = "specialist"
    tools: tuple[str, ...] = ()
    # Tools whose error response means "no records" rather than a gateway failure.
    optional_tools: tuple[str, ...] = ()

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def fetch(
        self, finding: Finding, case_id: str, tool: str, **arguments: str
    ) -> Any | None:
        if tool not in self.tools:
            raise PermissionError(f"{self.actor} is not allowed to call {tool}")
        last_error = ""
        for attempt in range(1, len(RETRY_DELAYS) + 2):
            try:
                evidence = await self.gateway.call(tool, case_id=case_id, **arguments)
            except RuntimeError as exc:
                if tool in self.optional_tools:
                    finding.not_found.append(tool)
                    return None
                last_error = str(exc)
            except TRANSIENT_ERRORS as exc:
                last_error = type(exc).__name__
            else:
                finding.evidence[tool] = evidence["evidence_ref"]
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.actor,
                    tool_name=tool,
                    evidence_refs=[evidence["evidence_ref"]],
                    attributes={"domain": evidence["domain"], "attempt": attempt},
                )
                return evidence["data"]
            if attempt <= len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])
        finding.errors.append(f"{tool}: {last_error}")
        return None

    async def handle(self, case: dict[str, Any], context: dict[str, Any]) -> Finding:
        case_id = case["case_id"]
        self.trace.emit(
            case_id=case_id, event_type="task_assigned", actor="coordinator", target=self.actor
        )
        finding = Finding(self.actor)
        await self.run(case, context, finding)
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.actor,
            target="coordinator",
            decision_code="FAILED" if finding.errors else "OK",
            evidence_refs=sorted(set(finding.evidence.values())) or None,
            attributes={"not_found": ",".join(finding.not_found) or None},
        )
        return finding

    async def run(self, case: dict[str, Any], context: dict[str, Any], finding: Finding) -> None:
        raise NotImplementedError


class OrderAgent(Specialist):
    actor = "order-agent"
    tools = ("get_order", "get_order_items", "get_sellers")

    async def run(self, case: dict[str, Any], context: dict[str, Any], finding: Finding) -> None:
        case_id = case["case_id"]
        order_id = context["order_id"]
        finding.facts["order"] = None
        order = await self.fetch(finding, case_id, "get_order", order_id=order_id)
        if not isinstance(order, dict) or order.get("order_id") != order_id:
            return
        purchased = parse_ts(order.get("order_purchase_timestamp"))
        opened = parse_ts(case.get("opened_at"))
        if purchased is None or opened is None:
            return
        window = CaseWindow(
            purchased_at=purchased,
            approved_at=parse_ts(order.get("order_approved_at")),
            opened_at=opened,
            estimated_at=parse_ts(order.get("order_estimated_delivery_date")),
        )
        finding.facts.update(order=order, window=window)

        items = await self.fetch(finding, case_id, "get_order_items", order_id=order_id)
        raw_items = items if isinstance(items, list) else []
        rows = [
            row
            for row in dedupe(raw_items)
            if row.get("order_id") == order_id
            and window.contains_limit(row.get("shipping_limit_date"))
        ]
        if len(rows) < len(raw_items):
            finding.conflicts.append(conflict("order_items.shipping_limit_date", "get_order_items"))
        freight_by_seller: dict[str, Decimal] = {}
        for row in rows:
            seller = row.get("seller_id")
            freight_by_seller[seller] = freight_by_seller.get(seller, ZERO) + money(
                row.get("freight_value")
            )
        finding.facts.update(
            items=rows,
            item_ids=sorted({row["order_item_id"] for row in rows if row.get("order_item_id")}),
            seller_ids=sorted({row["seller_id"] for row in rows if row.get("seller_id")}),
            items_total=sum(
                (money(r.get("price")) + money(r.get("freight_value")) for r in rows), ZERO
            ),
            freight_total=sum(freight_by_seller.values(), ZERO),
            freight_by_seller=freight_by_seller,
        )
        sellers = await self.fetch(finding, case_id, "get_sellers", order_id=order_id)
        finding.facts["seller_records"] = [
            row
            for row in (sellers if isinstance(sellers, list) else [])
            if isinstance(row, dict) and row.get("seller_id") in freight_by_seller
        ]


class PaymentAgent(Specialist):
    actor = "payment-agent"
    tools = ("get_payment_timeline",)

    async def run(self, case: dict[str, Any], context: dict[str, Any], finding: Finding) -> None:
        window: CaseWindow = context["window"]
        data = await self.fetch(
            finding, case["case_id"], "get_payment_timeline", order_id=context["order_id"]
        )
        events = data.get("events", []) if isinstance(data, dict) else []
        scoped = scoped_events(events, context["order_id"], window.contains_payment)
        captures = [
            money(e.get("amount_brl"))
            for e in scoped
            if e.get("event_type") == "captured" and e.get("status") == "confirmed"
        ]
        mismatches = [
            money(e.get("amount_brl"))
            for e in scoped
            if e.get("event_type") == "reconciliation_mismatch"
        ]
        finding.facts.update(
            available=data is not None,
            captures=captures,
            captured_total=sum(captures, ZERO),
            mismatches=mismatches,
            excluded_events=len(events) - len(scoped),
        )
        if len(scoped) < len(events):
            finding.conflicts.append(conflict("payment_events.event_at", "get_payment_timeline"))


class RefundAgent(Specialist):
    actor = "refund-agent"
    tools = ("get_refund_timeline",)
    # The gateway answers with a tool error when an order has no refund records at all.
    # This call sits between two required calls, so an outage is still detected per case.
    optional_tools = ("get_refund_timeline",)

    async def run(self, case: dict[str, Any], context: dict[str, Any], finding: Finding) -> None:
        window: CaseWindow = context["window"]
        data = await self.fetch(
            finding, case["case_id"], "get_refund_timeline", order_id=context["order_id"]
        )
        events = data.get("events", []) if isinstance(data, dict) else []
        scoped = scoped_events(events, context["order_id"], window.contains)
        finding.facts.update(
            failed=[money(e.get("amount_brl")) for e in scoped if e.get("status") == "failed"],
            pending=[money(e.get("amount_brl")) for e in scoped if e.get("status") == "pending"],
            excluded_events=len(events) - len(scoped),
        )
        if len(scoped) < len(events):
            finding.conflicts.append(conflict("refund_events.event_at", "get_refund_timeline"))


class ShipmentAgent(Specialist):
    actor = "shipment-agent"
    tools = ("get_shipment_summary",)

    async def run(self, case: dict[str, Any], context: dict[str, Any], finding: Finding) -> None:
        window: CaseWindow = context["window"]
        order: dict[str, Any] = context["order"]
        data = await self.fetch(
            finding, case["case_id"], "get_shipment_summary", order_id=context["order_id"]
        )
        summary = data if isinstance(data, dict) else {}
        limits = [
            (row.get("seller_id"), parse_ts(row.get("shipping_limit_at")))
            for row in dedupe(summary.get("shipping_limits", []))
            if window.contains_limit(row.get("shipping_limit_at"))
        ]
        if not limits:
            limits = [
                (item.get("seller_id"), parse_ts(item.get("shipping_limit_date")))
                for item in context["items"]
            ]
        # Timestamps from the authoritative order row win over shipment events.
        carrier_at = parse_ts(
            summary.get("delivered_carrier_at") or order.get("order_delivered_carrier_date")
        )
        delivered_at = parse_ts(
            summary.get("delivered_customer_at") or order.get("order_delivered_customer_date")
        )
        estimated_at = parse_ts(
            summary.get("estimated_delivery_at") or order.get("order_estimated_delivery_date")
        )
        # Still in transit when the case was opened after the promised date: late as well.
        if (
            delivered_at is None
            and estimated_at is not None
            and order.get("order_status") in UNDELIVERED_STATUSES
            and window.opened_at > estimated_at
        ):
            effective_delivery = window.opened_at
        else:
            effective_delivery = delivered_at
        late_sellers = sorted(
            {
                seller
                for seller, limit in limits
                if seller and limit and carrier_at and carrier_at > limit
            }
        )
        # Delivery events must match the order row's delivery timestamp to be believed.
        stray_events = [
            event
            for event in dedupe(summary.get("events", []))
            if parse_ts(event.get("event_at")) != delivered_at
        ]
        if stray_events:
            finding.conflicts.append(
                conflict(
                    "shipment_events.event_at",
                    "get_shipment_summary",
                    "ORDER_DELIVERY_TIMESTAMP_AUTHORITATIVE",
                )
            )
        finding.facts.update(
            available=data is not None,
            carrier_at=carrier_at,
            delivered_at=effective_delivery,
            estimated_at=estimated_at,
            late_seller_ids=late_sellers,
        )


class PolicyAgent(Specialist):
    actor = "policy-agent"
    tools = ("get_policy",)

    async def run(self, case: dict[str, Any], context: dict[str, Any], finding: Finding) -> None:
        finding.facts["decision"] = None
        data = await self.fetch(
            finding, case["case_id"], "get_policy", policy_version=case["policy_version"]
        )
        rules = data.get("rules") if isinstance(data, dict) else None
        rule = rules.get(context["issue"]) if isinstance(rules, dict) else None
        if not isinstance(rule, dict):
            return
        issue = context["issue"]
        refund = self.refund_amount(issue, context)
        policy_refund = money(rule.get("refund_brl", 0))
        finding.facts["decision"] = {
            "case_status": rule.get("case_status", "needs_investigation"),
            "action": rule.get("recommended_action"),
            "refund": refund,
            "parties": self.parties(rule, self.responsible_sellers(issue, context)),
        }
        self.trace.emit(
            case_id=case["case_id"],
            event_type="policy_decided",
            actor=self.actor,
            decision_code=str(rule.get("recommended_action") or "NO_RULE").upper(),
            evidence_refs=[finding.evidence["get_policy"]],
            attributes={
                "issue": issue,
                "case_status": rule.get("case_status"),
                "refund_brl": float(refund),
                "policy_refund_matches": refund == policy_refund,
            },
        )

    @staticmethod
    def responsible_sellers(issue: str, context: dict[str, Any]) -> list[str]:
        if issue == "late_delivery_seller":
            return context["shipment"].get("late_seller_ids", [])
        return context["seller_ids"]

    @staticmethod
    def parties(rule: dict[str, Any], sellers: list[str]) -> list[dict[str, Any]]:
        parties: list[dict[str, Any]] = []
        for party in rule.get("responsible_parties", []):
            party_type = party.get("party_type", "unknown")
            # Policy party IDs are samples from other orders; bind sellers to this order.
            if party_type == "seller" and sellers:
                parties.extend({"party_type": "seller", "party_id": s} for s in sellers)
            elif party_type == "seller":
                parties.append({"party_type": "seller", "party_id": None})
            else:
                parties.append({"party_type": party_type, "party_id": None})
        return parties[:5]

    @staticmethod
    def refund_amount(issue: str, context: dict[str, Any]) -> Decimal:
        payment = context["payment"]
        refund = context["refund"]
        captures: list[Decimal] = payment.get("captures", [])
        captured = payment.get("captured_total", ZERO)
        if issue in {"canceled_order_paid", "unavailable_order_paid"}:
            return captured
        if issue == "refund_failed":
            return sum(refund.get("failed", []), ZERO)
        if issue == "payment_mismatch":
            return sum(payment.get("mismatches", []), ZERO)
        if issue == "duplicate_charge":
            return captured - sum(set(captures), ZERO)
        if issue == "late_delivery_seller":
            late = context["shipment"].get("late_seller_ids", [])
            freight = sum((context["freight_by_seller"].get(s, ZERO) for s in late), ZERO)
            return min(freight, captured)
        if issue == "late_delivery_logistics":
            return min(context["freight_total"], captured)
        return ZERO
