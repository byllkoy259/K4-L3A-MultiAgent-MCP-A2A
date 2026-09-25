# L3A Architecture Record

Tài liệu mô tả các quyết định có thể kiểm chứng trong source (`src/student_agent/agents.py`,
`src/student_agent/workflow.py`). Hệ thống hoàn toàn rule-based và deterministic: không dùng LLM,
không có prompt, mọi kết luận suy ra từ dữ liệu MCP.

## 1. System overview

```text
inputs/<case_id>.json
      │  (cli.py emit case_received)
      ▼
 Coordinator ──task_assigned──► Order agent      get_order, get_order_items, get_sellers
      │                           │  → định nghĩa CaseWindow (vòng đời đơn hàng)
      │◄──────── handoff ─────────┘
      ├──task_assigned──► Payment agent   get_payment_timeline
      ├──task_assigned──► Refund agent    get_refund_timeline
      ├──task_assigned──► Shipment agent  get_shipment_summary
      │◄──────── handoff (Finding: facts, evidence, conflicts, errors)
      │
      │  classify(): primary_issue từ facts (không lấy từ claim topic)
      ├──handoff──► Policy agent  get_policy → policy_decided (status, action, refund, parties)
      │◄──handoff──┘
      ├──handoff──► Verifier → verification_completed (PASS | FIXED)
      ▼
 outputs/<case_id>.json  +  traces/trace.jsonl   (cli.py emit case_finalized)
```

Order agent chạy trước vì mốc thời gian của đơn (`order_purchase_timestamp`, `order_approved_at`,
`order_estimated_delivery_date`) cùng `opened_at` của case tạo thành `CaseWindow` mà mọi specialist
dùng để lọc dữ liệu.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case input, các `Finding` | Giao việc, gom facts, `classify()` chọn `primary_issue`, chọn evidence trích dẫn, dựng output | Handoff issue cho policy agent, output cho verifier |
| Order/item (`order-agent`) | `claimed_order_id`, `opened_at` | Lấy order row có thẩm quyền, lọc item theo `shipping_limit` trong vòng đời đơn, bỏ dòng trùng, tính tổng tiền và phí ship theo seller, lấy hồ sơ seller | `order`, `window`, `item_ids`, `seller_ids`, `items_total`, `freight_by_seller` |
| Payment (`payment-agent`) | `order_id`, `CaseWindow` | Chỉ giữ payment event trong 24h sau `order_approved_at`; tách capture và `reconciliation_mismatch` | `captures`, `captured_total`, `mismatches` |
| Refund (`refund-agent`) | `order_id`, `CaseWindow` | Refund event trong khoảng `[purchase, opened_at]`; phân loại `failed` và `pending` | `failed`, `pending` |
| Shipment (`shipment-agent`) | `order_id`, order row, items | Mốc giao hàng lấy từ order row/summary; xác định seller giao cho carrier trễ hạn (`carrier_at > shipping_limit`) | `delivered_at`, `estimated_at`, `late_seller_ids` |
| Policy (`policy-agent`) | issue, facts | Áp rule của `EC_POLICY_V1`: `case_status`, `recommended_action`, bên chịu trách nhiệm (gắn `party_id` vào seller của đơn này), tính tiền hoàn từ dữ liệu và đối chiếu `refund_brl` của policy | Event `policy_decided` + decision |
| Verifier | output, tập evidence đã consume | Kiểm tra invariants (mục 6) và tự sửa nếu cần | Event `verification_completed` |

Quyền gọi tool được thực thi trong code: `Specialist.fetch()` ném `PermissionError` nếu actor gọi tool
ngoài danh sách `tools` của mình. Không actor nào gọi `get_product_context` hoặc `get_customer_history`
vì không issue nào cần đến.

## 3. A2A protocol

- **Envelope:** mỗi specialist nhận `(case, context)` và trả về một `Finding` gồm `agent`, `facts`,
  `evidence` (tool → `evidence_ref`), `conflicts`, `not_found`, `errors`.
- **Correlation:** mọi message và trace event mang `case_id`. `context` được tạo mới cho từng case nên
  không có state hoặc evidence dùng lại giữa các case.
- **Handoff:** coordinator emit `task_assigned` (target = specialist). Specialist emit
  `tool_result_consumed` cho từng evidence rồi `handoff` về coordinator với `decision_code`
  `OK`/`FAILED`. Coordinator emit `handoff` tới `policy-agent` với `decision_code = <ISSUE>`, rồi
  `handoff` tới `verifier`.
- **Điều kiện handoff:** payment, refund và shipment chỉ chạy khi order agent xác định được order và
  `CaseWindow`.
- **Không có vòng lặp:** pipeline là DAG cố định (order → payment → refund → shipment → policy →
  verifier). Mỗi agent chạy đúng một lần mỗi case, không có re-delegation.
- **Timeout:** timeout HTTP của gateway (30s connect, 300s read) cộng với retry có giới hạn (mục 5).
- Trace chỉ chứa sự kiện quan sát được và decision code, không chứa nội dung suy luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call()` validate mọi response theo `mcp-evidence-response-v1.schema.json`.
2. `Specialist.fetch()` lưu `evidence_ref` vào `Finding.evidence` và emit ngay
   `tool_result_consumed` kèm `evidence_refs=[ref]` và attribute `domain`, `attempt`.
3. Coordinator chỉ trích các evidence hỗ trợ kết luận, theo `EVIDENCE_BY_ISSUE`. Ví dụ
   `late_delivery_seller` trích order, items, sellers, shipment, payment (cơ sở tiền hoàn) và policy.
   Claim hoàn tiền được gắn với evidence payment, refund và policy.
4. Verifier loại mọi ref không nằm trong tập đã consume của chính case đó trong lần chạy này. Evidence
  không bao giờ được cache hoặc tái sử dụng giữa các case hay các lần chạy.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / transport error trong một call | Có, tối đa 5 lần, backoff 2s → 5s → 15s → 30s → 60s | Hết lượt thì case thất bại và chuyển lên retry cấp case | `handoff` `decision_code=FAILED` |
| Mất kết nối MCP (session bị huỷ) hoặc case thất bại | Có, retry cả case tối đa 4 lần, backoff 10s → 30s → 60s → 120s, mỗi lần một session mới | Trace của lần thử hỏng bị huỷ (buffer theo case) nên mỗi case chỉ có một lifecycle; hết lượt thì dừng run, không finalize output đoán mò | Cảnh báo trên stderr; run exit 1 |
| Tool error trên tool bắt buộc (order, items, sellers, payment, shipment, policy) | Có, như trên (gateway trả lỗi chung khi quá tải) | Như trên | `handoff` `FAILED` |
| Not found (`get_refund_timeline` của đơn không có refund) | Không | Coi là không có refund | `handoff` `OK`, attribute `not_found=get_refund_timeline` |
| Source conflict (dòng nằm ngoài vòng đời đơn, dòng trùng, shipment event lệch order row) | Không | Order row có thẩm quyền được chọn; dòng lệch bị loại khỏi tính toán | Ghi vào `data_conflicts` (`resolution_code=EXCLUDED_OUTSIDE_ORDER_LIFECYCLE` hoặc `ORDER_DELIVERY_TIMESTAMP_AUTHORITATIVE`) |
| Order không khớp `claimed_order_id` / thiếu mốc thời gian | Không | `insufficient_evidence`, `needs_investigation`, confidence 0.5 | `handoff` coordinator `INSUFFICIENT_EVIDENCE` |
| Invalid specialist result (vi phạm invariant) | Không | Verifier sửa về trạng thái an toàn | `verification_completed` `decision_code=FIXED`, attribute `fixes` |

Mọi retry đều idempotent vì tool MCP chỉ đọc. Missing evidence không bao giờ được thay bằng dữ liệu
phỏng đoán.

## 6. Verification invariants

Verifier kiểm tra trước khi finalize (`Verifier.verify` trong `workflow.py`). `cli.py` sau đó validate
output theo JSON Schema.

- **Evidence ownership:** `evidence_refs` ⊆ các ref đã consume cho case này trong lần chạy này;
  evidence của từng claim ⊆ `evidence_refs`.
- **Money totals:** tổng `refund_lines` = `recommended_refund_brl`, làm tròn 2 chữ số, BRL.
- **Status/refund/action:** `no_action` thì refund bằng 0 và không có refund line; `action_required`
  thì phải có ít nhất 1 action; `resolution_actions` không trùng lặp; refund bằng 0 thì claim hoàn tiền
  không được `supported`.
- **Entity scope:** `party_id` của seller chịu trách nhiệm phải thuộc `seller_ids` của đơn. Với
  `late_delivery_seller`, chỉ seller giao carrier trễ hạn mới bị quy trách nhiệm.
- **Confidence bounds:** trong khoảng [0, 1]. Mức 0.98 khi dữ liệu xác nhận claim, 0.8 khi dữ liệu mâu
  thuẫn với claim, 0.5 khi thiếu evidence.

## 7. Reproducibility

- **Model/config:** không dùng LLM. Kết quả deterministic với cùng dữ liệu MCP. Không có random seed.
  `evidence_ref` do server cấp, khác nhau giữa các lần chạy.
- **Dependencies:** theo `pyproject.toml` (Python ≥ 3.11, `mcp>=2,<3`, `httpx2`, `jsonschema`).
  Đã kiểm thử với `mcp 2.2.0`.
- **Concurrency:** xử lý tuần tự từng case, mỗi case gọi 7 tool (khoảng 700 call mỗi run, khoảng
  6 phút).
- **Lệnh chạy:**
  ```bash
  day09 run && day09 validate && day09 package --output dist/submission.zip
  ```
- **Kiểm thử:** `pytest -q tests/test_workflow.py` dùng gateway giả với dữ liệu tổng hợp (không chứa
  dữ liệu thi).
- **Cấu hình:** đọc từ `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`).
  Không ghi API key vào output, trace hoặc tài liệu.
