from __future__ import annotations

from typing import Any

import synthetic as s
from student_agent.analysis import (
    CaseFacts,
    diagnose,
    order_facts,
    order_window,
    parse_time,
    payment_facts,
    shipment_facts,
)

OUTSIDE = "2018-05-01T10:00:00-03:00"  # after the case was opened: another period's record


def facts(
    order: dict[str, Any] | None = None,
    items: list[dict[str, Any]] | None = None,
    pay: dict[str, Any] | None = None,
    refund: dict[str, Any] | None = None,
    limits: list[str] | None = None,
) -> CaseFacts:
    order = order or s.order()
    window = order_window(order, parse_time(s.OPENED_AT))
    return CaseFacts(
        order=order_facts(order, items or [s.item()], window),
        payment=payment_facts(
            pay or s.timeline([s.payment("89.00")], [s.event(s.APPROVED_AT, "89.00")]),
            refund, window,
        ),
        shipment=shipment_facts(s.shipment(order, limits or ["2018-01-04T09:00:00-03:00"]),
                                window),
    )


def test_rows_outside_the_case_window_are_ignored() -> None:
    result = facts(
        items=[s.item(), s.item(limit="2018-05-04T09:00:00-03:00", freight="18.00")],
        pay=s.timeline([s.payment("89.00"), s.payment("18.00")],
                       [s.event(s.APPROVED_AT, "89.00"), s.event(OUTSIDE, "18.00")]),
    )
    assert len(result.order.items) == 1 and result.order.excluded_items == 1
    assert [c.amount for c in result.payment.captures] == [89]
    assert diagnose("unsupported_claim", result).primary_issue == "unsupported_claim"


def test_identical_rows_are_one_record_not_a_duplicate_charge() -> None:
    result = facts(
        items=[s.item(), s.item()],
        pay=s.timeline([s.payment("89.00"), s.payment("89.00")],
                       [s.event(s.APPROVED_AT, "89.00"), s.event(s.APPROVED_AT, "89.00")]),
    )
    assert result.payment.captured_total == 89
    assert not diagnose("duplicate_charge", result).signals["duplicate_charge"]


def test_split_payment_matching_the_order_value_is_valid() -> None:
    split = s.timeline(
        [s.payment("44.50", 1), s.payment("44.50", 2, "voucher")],
        [s.event(s.APPROVED_AT, "44.50"), s.event("2018-01-01T11:00:00-03:00", "44.50")],
    )
    diagnosis = diagnose("valid_split_payment", facts(pay=split))
    assert diagnosis.primary_issue == "valid_split_payment" and diagnosis.claim_supported
    # The customer calls it a duplicate, but the parts add up to the order: related issue.
    assert diagnose("duplicate_charge", facts(pay=split)).primary_issue == "valid_split_payment"


def test_same_amount_captured_twice_above_order_value_is_a_duplicate_charge() -> None:
    duplicate = s.timeline(
        [s.payment("64.00", 1), s.payment("64.00", 2, "voucher")],
        [s.event(s.APPROVED_AT, "64.00"), s.event("2018-01-01T11:00:00-03:00", "64.00")],
    )
    assert diagnose("duplicate_charge", facts(pay=duplicate)).primary_issue == "duplicate_charge"


def test_late_delivery_blames_seller_only_when_handoff_missed_the_limit() -> None:
    late = s.order(carrier="2018-01-06T09:00:00-03:00", delivered="2018-01-12T09:00:00-03:00")
    seller_late = facts(order=late, limits=["2018-01-04T09:00:00-03:00"])
    assert diagnose("late_delivery_seller", seller_late).primary_issue == "late_delivery_seller"
    assert seller_late.shipment.late_sellers == (s.SELLER,)

    carrier_late = facts(order=late, limits=["2018-01-07T09:00:00-03:00"])
    assert diagnose("late_delivery_seller", carrier_late).primary_issue == (
        "late_delivery_logistics"
    )


def test_refund_status_comes_from_in_window_refund_events_only() -> None:
    failed = s.refunds(s.event("2018-01-12T09:00:00-03:00", "52.00", "refund_requested", "failed"))
    assert diagnose("refund_failed", facts(refund=failed)).primary_issue == "refund_failed"

    stale = s.refunds(s.event(OUTSIDE, "52.00", "refund_requested", "failed"))
    assert diagnose("refund_failed", facts(refund=stale)).primary_issue == "unsupported_claim"


def test_overlapping_records_keep_the_claimed_issue_but_lower_confidence() -> None:
    noisy = s.timeline(
        [s.payment("52.00"), s.payment("44.50", 1), s.payment("44.50", 2, "voucher")],
        [s.event(s.APPROVED_AT, "52.00"), s.event(s.APPROVED_AT, "44.50"),
         s.event("2018-01-01T11:00:00-03:00", "44.50")],
    )
    failed = s.refunds(s.event("2018-01-12T09:00:00-03:00", "52.00", "refund_requested", "failed"))
    diagnosis = diagnose("refund_failed", facts(pay=noisy, refund=failed))
    assert diagnosis.primary_issue == "refund_failed"
    assert diagnosis.confidence < 0.95


def test_claim_without_matching_evidence_is_unsupported() -> None:
    assert diagnose("canceled_order_paid", facts()).primary_issue == "unsupported_claim"
    canceled = facts(order=s.order(status="canceled", delivered=None))
    assert diagnose("canceled_order_paid", canceled).primary_issue == "canceled_order_paid"


def test_missing_order_evidence_is_never_guessed() -> None:
    diagnosis = diagnose("canceled_order_paid", CaseFacts(missing=("order",)))
    assert diagnosis.primary_issue == "insufficient_evidence"
