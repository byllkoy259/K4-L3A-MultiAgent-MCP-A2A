"""Synthetic MCP payload builders shaped like the gateway's responses (no competition data)."""

from __future__ import annotations

import hashlib
from typing import Any

ORDER_ID = "0123456789abcdef0123456789abcdef"
OPENED_AT = "2018-01-13T09:00:00-03:00"
PURCHASED_AT = "2018-01-01T09:00:00-03:00"
APPROVED_AT = "2018-01-01T10:00:00-03:00"
SELLER = "seller-0123456789ab"
ITEM = "item-0123456789ab"


def order(
    status: str = "delivered",
    carrier: str | None = "2018-01-03T09:00:00-03:00",
    delivered: str | None = "2018-01-10T09:00:00-03:00",
    estimated: str = "2018-01-11T09:00:00-03:00",
) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID, "customer_id": "customer-row-0123", "order_status": status,
        "order_purchase_timestamp": PURCHASED_AT, "order_approved_at": APPROVED_AT,
        "order_delivered_carrier_date": carrier, "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": estimated,
    }


def item(limit: str = "2018-01-04T09:00:00-03:00", price: str = "79.00",
         freight: str = "10.00") -> dict[str, Any]:
    return {"order_id": ORDER_ID, "order_item_id": ITEM, "product_id": "product-0123",
            "seller_id": SELLER, "shipping_limit_date": limit, "price": price,
            "freight_value": freight}


def payment(value: str, seq: int = 1, kind: str = "credit_card") -> dict[str, Any]:
    return {"order_id": ORDER_ID, "payment_sequential": str(seq), "payment_type": kind,
            "payment_installments": "1", "payment_value": value}


def event(at: str, amount: str, event_type: str = "captured",
          status: str = "confirmed") -> dict[str, Any]:
    return {"order_id": ORDER_ID, "event_at": at, "event_type": event_type,
            "amount_brl": amount, "status": status}


def timeline(payments: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"order_id": ORDER_ID, "payments": payments, "events": events}


def refunds(*events: dict[str, Any]) -> dict[str, Any]:
    return {"order_id": ORDER_ID, "events": list(events)}


def shipment(order_row: dict[str, Any], limits: list[str]) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID, "order_status": order_row["order_status"],
        "delivered_carrier_at": order_row["order_delivered_carrier_date"],
        "delivered_customer_at": order_row["order_delivered_customer_date"],
        "estimated_delivery_at": order_row["order_estimated_delivery_date"],
        "shipping_limits": [
            {"order_item_id": ITEM, "seller_id": SELLER, "shipping_limit_at": limit}
            for limit in limits
        ],
        "events": [],
    }


POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V1",
    "rules": {
        "canceled_order_paid": {"case_status": "action_required",
                                "recommended_action": "issue_refund", "refund_brl": 79.0,
                                "responsible_parties": [{"party_id": None,
                                                         "party_type": "platform"}]},
        "late_delivery_seller": {"case_status": "action_required",
                                 "recommended_action": "refund_freight", "refund_brl": 18.0,
                                 "responsible_parties": [{"party_id": "seller-other",
                                                          "party_type": "seller"}]},
        "valid_split_payment": {"case_status": "no_action",
                                "recommended_action": "document_no_action", "refund_brl": 0.0,
                                "responsible_parties": [{"party_id": None,
                                                         "party_type": "customer"}]},
        "unsupported_claim": {"case_status": "no_action",
                              "recommended_action": "document_no_action", "refund_brl": 0.0,
                              "responsible_parties": [{"party_id": None,
                                                       "party_type": "customer"}]},
    },
}

DOMAINS = {
    "get_order": "order", "get_order_items": "item", "get_sellers": "seller",
    "get_payment_timeline": "payment", "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment", "get_policy": "policy",
}


def envelope(case_id: str, tool: str, data: Any) -> dict[str, Any]:
    digest = hashlib.sha256(f"{case_id}:{tool}".encode()).hexdigest()
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{digest[:32]}",
        "result_hash": f"sha256:{digest}",
        "domain": DOMAINS[tool],
        "data": data,
        "warnings": [],
    }


def case_input(case_id: str, topic: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "opened_at": OPENED_AT,
        "customer_request": {
            "language": "vi", "message": "synthetic", "claimed_order_id": ORDER_ID,
            "claims": [{"claim_id": "claim-a", "topic": topic},
                       {"claim_id": "claim-b", "topic": "requested_full_refund"}],
        },
        "policy_version": "EC_POLICY_V1",
    }
