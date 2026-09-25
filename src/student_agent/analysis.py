"""Pure analysis of MCP evidence payloads (no I/O), so every rule is unit-testable.

MCP rows for one order can mix the case's own records with records from other time
periods. Only rows timestamped inside the case window -- from the order purchase to
the moment the case was opened -- describe this complaint; everything else is ignored
and counted so the trace can show what was excluded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Any, TypeVar

T = TypeVar("T")
MONEY_TOLERANCE = Decimal("0.01")
MAX_SPLIT_PARTS = 8


class InvalidEvidence(ValueError):
    """An MCP payload did not have the shape a specialist relies on."""


def parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise InvalidEvidence(f"bad timestamp {value!r}") from exc


def parse_money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise InvalidEvidence(f"bad amount {value!r}") from exc


def _require(row: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(row, dict) or any(key not in row for key in keys):
        raise InvalidEvidence(f"expected an object with {keys}")
    return row


@dataclass(frozen=True)
class CaseWindow:
    start: datetime
    end: datetime

    def contains(self, moment: datetime | None) -> bool:
        return moment is not None and self.start <= moment <= self.end


def _one_per_key(
    rows: Iterable[T], *, key: Callable[[T], str], moment: Callable[[T], datetime]
) -> tuple[T, ...]:
    """One row per entity id. Conflicting versions of the same item resolve to the one
    closest to the order (earliest in-window timestamp)."""
    chosen: dict[str, T] = {}
    for row in rows:
        current = chosen.get(key(row))
        if current is None or moment(row) < moment(current):
            chosen[key(row)] = row
    return tuple(chosen.values())


# ---------------------------------------------------------------- order domain


@dataclass(frozen=True)
class Item:
    item_id: str
    seller_id: str
    price: Decimal
    freight: Decimal
    shipping_limit_at: datetime | None


@dataclass(frozen=True)
class OrderFacts:
    order_id: str
    status: str
    purchased_at: datetime
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    items: tuple[Item, ...]
    excluded_items: int

    @property
    def order_value(self) -> Decimal:
        return sum((item.price + item.freight for item in self.items), Decimal(0))


def order_window(order_data: Any, opened_at: datetime) -> CaseWindow:
    row = _require(order_data, "order_purchase_timestamp")
    start = parse_time(row["order_purchase_timestamp"])
    if start is None:
        raise InvalidEvidence("order has no purchase timestamp")
    return CaseWindow(start, opened_at)


def order_facts(order_data: Any, items_data: Any, window: CaseWindow) -> OrderFacts:
    row = _require(
        order_data, "order_id", "order_status", "order_delivered_carrier_date",
        "order_delivered_customer_date", "order_estimated_delivery_date",
    )
    if not isinstance(items_data, list):
        raise InvalidEvidence("order items must be a list")
    all_items = [
        Item(
            item_id=str(r["order_item_id"]),
            seller_id=str(r["seller_id"]),
            price=parse_money(r["price"]),
            freight=parse_money(r["freight_value"]),
            shipping_limit_at=parse_time(r["shipping_limit_date"]),
        )
        for r in (
            _require(r, "order_item_id", "seller_id", "price", "freight_value",
                     "shipping_limit_date")
            for r in items_data
        )
    ]
    items = _one_per_key(
        (i for i in all_items if window.contains(i.shipping_limit_at)),
        key=lambda i: i.item_id, moment=lambda i: i.shipping_limit_at,
    )
    return OrderFacts(
        order_id=str(row["order_id"]),
        status=str(row["order_status"]),
        purchased_at=window.start,
        carrier_at=parse_time(row["order_delivered_carrier_date"]),
        delivered_at=parse_time(row["order_delivered_customer_date"]),
        estimated_at=parse_time(row["order_estimated_delivery_date"]),
        items=items,
        excluded_items=len(all_items) - len(items),
    )


# -------------------------------------------------------------- payment domain


@dataclass(frozen=True)
class Capture:
    at: datetime
    amount: Decimal
    payment_type: str | None
    sequential: int | None


@dataclass(frozen=True)
class RefundEvent:
    at: datetime
    amount: Decimal
    status: str


@dataclass(frozen=True)
class PaymentFacts:
    captures: tuple[Capture, ...]
    open_mismatches: tuple[Decimal, ...]
    refunds: tuple[RefundEvent, ...]
    excluded_events: int

    @property
    def captured_total(self) -> Decimal:
        return sum((c.amount for c in self.captures), Decimal(0))

    def refunds_with_status(self, status: str) -> tuple[RefundEvent, ...]:
        return tuple(r for r in self.refunds if r.status == status)


def payment_facts(timeline_data: Any, refund_data: Any, window: CaseWindow) -> PaymentFacts:
    timeline = _require(timeline_data, "payments", "events")
    events = [_require(e, "event_at", "event_type", "amount_brl", "status")
              for e in timeline["events"]]
    rows = [_require(r, "payment_value") for r in timeline["payments"]]
    captured_events = [e for e in events if e["event_type"] == "captured"]
    # Payment rows carry type/sequence but no timestamp; they are listed in the same
    # order as their capture events. Pair them only when that correspondence holds.
    paired = len(rows) == len(captured_events) and all(
        parse_money(r["payment_value"]) == parse_money(e["amount_brl"])
        for r, e in zip(rows, captured_events, strict=True)
    )
    captures = []
    for index, event in enumerate(captured_events):
        row = rows[index] if paired else {}
        sequential = row.get("payment_sequential")
        captures.append(Capture(
            at=parse_time(event["event_at"]),
            amount=parse_money(event["amount_brl"]),
            payment_type=row.get("payment_type"),
            sequential=int(sequential) if sequential not in (None, "") else None,
        ))
    in_window = tuple(dict.fromkeys(c for c in captures if window.contains(c.at)))
    mismatches = tuple(
        dict.fromkeys(
            (parse_time(e["event_at"]), parse_money(e["amount_brl"]))
            for e in events
            if e["event_type"] == "reconciliation_mismatch" and e["status"] == "open"
            and window.contains(parse_time(e["event_at"]))
        )
    )
    refund_events = [] if refund_data is None else [
        _require(e, "event_at", "amount_brl", "status")
        for e in _require(refund_data, "events")["events"]
    ]
    refunds = tuple(dict.fromkeys(
        RefundEvent(parse_time(e["event_at"]), parse_money(e["amount_brl"]), str(e["status"]))
        for e in refund_events
        if window.contains(parse_time(e["event_at"]))
    ))
    return PaymentFacts(
        captures=in_window,
        open_mismatches=tuple(amount for _, amount in mismatches),
        refunds=refunds,
        excluded_events=sum(
            1 for e in [*events, *refund_events] if not window.contains(parse_time(e["event_at"]))
        ),
    )


# ------------------------------------------------------------- shipment domain


@dataclass(frozen=True)
class ShipmentFacts:
    status: str
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    shipping_limits: tuple[tuple[str, str, datetime], ...]  # (item_id, seller_id, limit)
    excluded_limits: int

    @property
    def delivered_late(self) -> bool:
        return (
            self.delivered_at is not None
            and self.estimated_at is not None
            and self.delivered_at > self.estimated_at
        )

    @property
    def late_sellers(self) -> tuple[str, ...]:
        """Sellers whose handoff to the carrier happened after their shipping limit."""
        if self.carrier_at is None:
            return ()
        return tuple(dict.fromkeys(
            seller for _, seller, limit in self.shipping_limits if self.carrier_at > limit
        ))


def shipment_facts(summary_data: Any, window: CaseWindow) -> ShipmentFacts:
    row = _require(
        summary_data, "order_status", "delivered_carrier_at", "delivered_customer_at",
        "estimated_delivery_at", "shipping_limits",
    )
    limits = [
        (str(r["order_item_id"]), str(r["seller_id"]), parse_time(r["shipping_limit_at"]))
        for r in (_require(r, "order_item_id", "seller_id", "shipping_limit_at")
                  for r in row["shipping_limits"])
    ]
    kept = _one_per_key(
        (limit for limit in limits if window.contains(limit[2])),
        key=lambda limit: limit[0], moment=lambda limit: limit[2],
    )
    return ShipmentFacts(
        status=str(row["order_status"]),
        carrier_at=parse_time(row["delivered_carrier_at"]),
        delivered_at=parse_time(row["delivered_customer_at"]),
        estimated_at=parse_time(row["estimated_delivery_at"]),
        shipping_limits=kept,
        excluded_limits=len(limits) - len(kept),
    )


# ------------------------------------------------------------------- the rules


@dataclass(frozen=True)
class CaseFacts:
    order: OrderFacts | None = None
    payment: PaymentFacts | None = None
    shipment: ShipmentFacts | None = None
    missing: tuple[str, ...] = field(default_factory=tuple)


def _same(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= MONEY_TOLERANCE


def _is_split(payment: PaymentFacts, order_value: Decimal) -> bool:
    """Captures from distinct payment sequences (1, 2, ...) that together pay the order once.

    Checks every subset, so unrelated captures overlapping the window do not hide a split.
    """
    parts = [c for c in payment.captures if c.sequential is not None][:MAX_SPLIT_PARTS]
    return any(
        len({c.sequential for c in subset}) == size
        and _same(sum((c.amount for c in subset), Decimal(0)), order_value)
        for size in range(2, len(parts) + 1)
        for subset in combinations(parts, size)
    )


def _is_duplicate(payment: PaymentFacts, order_value: Decimal) -> bool:
    """The same amount captured more than once, and the total exceeds what the order costs."""
    amounts = [c.amount for c in payment.captures]
    repeated = any(amounts.count(amount) >= 2 for amount in amounts)
    return repeated and payment.captured_total > order_value + MONEY_TOLERANCE


def issue_signals(facts: CaseFacts) -> dict[str, bool]:
    """Which issue patterns the in-window evidence shows. None means 'cannot tell'."""
    order, payment, shipment = facts.order, facts.payment, facts.shipment
    signals: dict[str, bool] = {}
    if order is not None and payment is not None:
        paid = payment.captured_total > 0 and not payment.refunds_with_status("completed")
        signals["canceled_order_paid"] = order.status == "canceled" and paid
        signals["unavailable_order_paid"] = order.status == "unavailable" and paid
        signals["valid_split_payment"] = _is_split(payment, order.order_value)
        signals["duplicate_charge"] = _is_duplicate(payment, order.order_value)
    if payment is not None:
        signals["payment_mismatch"] = bool(payment.open_mismatches)
        signals["refund_pending"] = bool(payment.refunds_with_status("pending"))
        signals["refund_failed"] = bool(payment.refunds_with_status("failed"))
    if shipment is not None:
        signals["late_delivery_seller"] = shipment.delivered_late and bool(shipment.late_sellers)
        signals["late_delivery_logistics"] = (
            shipment.delivered_late and not shipment.late_sellers
        )
    return signals


# Which facts each issue depends on (drives both the decision and which evidence to cite).
ISSUE_DOMAINS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("order", "payment"),
    "unavailable_order_paid": ("order", "payment"),
    "late_delivery_seller": ("order", "shipment"),
    "late_delivery_logistics": ("order", "shipment"),
    "valid_split_payment": ("order", "payment"),
    "payment_mismatch": ("order", "payment"),
    "duplicate_charge": ("order", "payment"),
    "refund_pending": ("order", "refund"),
    "refund_failed": ("order", "refund"),
    "unsupported_claim": ("order", "payment", "shipment"),
}
# Which specialist's facts a domain comes from (refund events are gathered by payment).
DOMAIN_OWNER = {"order": "order", "payment": "payment", "refund": "payment", "shipment": "shipment"}

# If the claimed pattern is absent, the closest pattern that would explain the complaint.
RELATED_ISSUES: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("unavailable_order_paid",),
    "unavailable_order_paid": ("canceled_order_paid",),
    "late_delivery_seller": ("late_delivery_logistics",),
    "late_delivery_logistics": ("late_delivery_seller",),
    "duplicate_charge": ("valid_split_payment", "payment_mismatch"),
    "valid_split_payment": ("duplicate_charge", "payment_mismatch"),
    "payment_mismatch": ("duplicate_charge", "valid_split_payment"),
    "refund_pending": ("refund_failed",),
    "refund_failed": ("refund_pending",),
}


@dataclass(frozen=True)
class Diagnosis:
    primary_issue: str
    claim_supported: bool
    confidence: float
    decision_code: str
    signals: dict[str, bool]


def diagnose(claimed_topic: str, facts: CaseFacts) -> Diagnosis:
    signals = issue_signals(facts)
    needed = {DOMAIN_OWNER[d] for d in ISSUE_DOMAINS.get(claimed_topic, ())}
    if "order" in facts.missing or needed & set(facts.missing):
        return Diagnosis("insufficient_evidence", False, 0.6, "REQUIRED_EVIDENCE_MISSING", signals)
    anomalies = [issue for issue, on in signals.items() if on]
    if claimed_topic == "unsupported_claim":
        if not anomalies:
            return Diagnosis("unsupported_claim", False, 0.9, "NO_ANOMALY_FOUND", signals)
        return Diagnosis(anomalies[0], True, 0.55, "ANOMALY_FOUND_FOR_GENERIC_CLAIM", signals)
    if signals.get(claimed_topic):
        # Other patterns firing at the same time means overlapping records: less certain.
        confidence = 0.95 if anomalies == [claimed_topic] else 0.85
        return Diagnosis(claimed_topic, True, confidence, "CLAIM_PATTERN_CONFIRMED", signals)
    for alternative in RELATED_ISSUES.get(claimed_topic, ()):
        if signals.get(alternative):
            return Diagnosis(alternative, False, 0.7, "RELATED_PATTERN_FOUND", signals)
    return Diagnosis("unsupported_claim", False, 0.8, "CLAIM_PATTERN_ABSENT", signals)
