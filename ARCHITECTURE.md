# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống xử lý từng case bằng một chuỗi agent cố định, không dùng LLM. Mọi quyết định đều theo luật và lặp lại được.

```text
inputs/<case_id>.json
        │  case_received
        ▼
   Coordinator ──task_assigned──► order-agent ──┐ (song song)
        │      ──task_assigned──► policy-agent ─┤
        │◄────────── handoff (order facts + case window, policy loaded)
        │
        │      ──task_assigned──► payment-agent ──┐ (song song, chỉ đọc bản ghi
        │      ──task_assigned──► shipment-agent ─┤  nằm trong case window)
        │◄────────── handoff (payment facts, shipment facts)
        │
        │  handoff FACTS_FOR_DECISION
        ▼
   policy-agent ── policy_decided ──► handoff DRAFT_FOR_VERIFICATION
        ▼
     verifier ── verification_completed (PASS/FAIL) ──► handoff VERIFIED
        ▼
   Coordinator ── ghi outputs/<case_id>.json ── case_finalized

Mọi MCP call ──► tool_result_consumed (actor, tool_name, evidence_ref)
```

Code liên quan:

| File | Nội dung |
| --- | --- |
| `workflow.py` | Coordinator, `solve_case()`, dựng output |
| `agents.py` | Các specialist agent và policy agent (có phân quyền tool) |
| `analysis.py` | Hàm thuần: lọc theo case window, trích facts, luật chẩn đoán |
| `verifier.py` | Các bất biến kiểm tra trước khi finalize |
| `a2a.py` | Message envelope và kênh A2A (trace `task_assigned`/`handoff`) |
| `evidence.py` | Evidence ledger theo từng case |

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `inputs/<case_id>.json` | Tách claim chính, giao việc theo thứ tự phụ thuộc, gom facts, dựng output | Output JSON; handoff `FACTS_FOR_DECISION` sang policy-agent |
| Order/item (`order-agent`) | `order_id`, `opened_at` | Đọc order row, item, seller; xác định **case window** = [thời điểm mua, thời điểm mở case]; loại item ngoài window | `OrderFacts`, seller ids, window → `ORDER_FACTS_READY` |
| Payment (`payment-agent`) | `order_id`, case window | Capture, reconciliation mismatch, refund event trong window; ghép payment row với capture event | `PaymentFacts` → `PAYMENT_FACTS_READY` |
| Shipment (`shipment-agent`) | `order_id`, case window | Mốc giao hàng, shipping limit trong window, giao trễ hay không, seller nào bàn giao trễ | `ShipmentFacts` → `SHIPMENT_FACTS_READY` |
| Policy (`policy-agent`) | `policy_version`, facts | Nạp policy; chẩn đoán `primary_issue`; áp rule policy để ra status, action, refund, responsible party | `policy_decided`; handoff `DRAFT_FOR_VERIFICATION` |
| Verifier | Output nháp, evidence ledger | Chạy 12 bất biến (mục 6); nếu fail thì thay bằng output escalation an toàn | `verification_completed`; handoff `VERIFIED`/`REJECTED` |

Quyền gọi tool. `Specialist.fetch()` từ chối mọi tool ngoài danh sách của agent đó bằng `ToolPermissionError`.

| Actor | Tool được gọi |
| --- | --- |
| order-agent | `get_order`, `get_order_items`, `get_sellers` |
| payment-agent | `get_payment_timeline`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |
| coordinator, verifier | không gọi tool |

Không agent nào gọi `get_customer_history`, `get_product_context` hay `get_order_payments`:
- Hai tool đầu không liên quan đến kết luận.
- `get_order_payments` trả về các payment row, vốn đã có sẵn trong `get_payment_timeline`.

## 3. A2A protocol

- **Envelope** (`a2a.Message`) gồm các trường:
  - `case_id`: dùng để correlate mọi message trong cùng một case.
  - `sequence`: số thứ tự tăng dần trong case.
  - `kind`: `task` hoặc `handoff`.
  - `sender`, `recipient`.
  - `code`: decision code, ví dụ `COLLECT_ORDER_FACTS`, `PAYMENT_FACTS_READY`.
  - `evidence_refs`: các ref mà message dựa vào.
  - `body`: payload nội bộ, không được trace.
- **Trace:** `A2AChannel.assign()` emit `task_assigned`, `A2AChannel.handoff()` emit `handoff`. Chỉ ghi code, số đếm và ref. Không ghi nội dung suy luận.
- **Điều kiện handoff:**
  - payment-agent và shipment-agent chỉ được giao việc sau khi order-agent trả về case window.
  - Nếu thiếu order, hai agent này không chạy và case kết luận `insufficient_evidence`.
  - policy-agent chỉ quyết định sau khi coordinator đã gom đủ facts.
- **Timeout:**
  - Mỗi MCP call có timeout 90 giây, retry tối đa 3 lần. Tool chỉ đọc nên retry idempotent.
  - HTTP client có read timeout 300 giây.
- **Tránh vòng lặp:**
  - Luồng là một DAG cố định; mỗi specialist chạy đúng 1 lần cho mỗi case.
  - `A2AChannel` dừng với lỗi `MessageLoopError` nếu vượt 40 message trong một case. Một case bình thường có khoảng 11 message.

## 4. Evidence lifecycle

1. **Validate.** `EvidenceGateway.call()` validate mọi response theo `mcp-evidence-response-v1`.
   - Response lỗi từ server, ví dụ record không tồn tại, được chuyển thành `ToolCallError`.
2. **Lưu.** `Specialist.fetch()` ghi evidence vào `EvidenceLedger` của đúng case đó, rồi emit ngay `tool_result_consumed` với `actor`, `tool_name`, `evidence_refs=[ref]` và `domain`.
   - Ledger được tạo mới cho mỗi case, nên ref không thể dùng chéo giữa các case.
3. **Lọc theo case window.** Mỗi order có thể lẫn bản ghi của thời kỳ khác.
   - Chỉ giữ bản ghi có mốc thời gian nằm trong [thời điểm mua, thời điểm mở case]. Mốc được dùng là capture/refund event hoặc shipping limit.
   - Bản ghi trùng hệt nhau được gộp làm một.
   - Nếu cùng một `order_item_id` có nhiều phiên bản, chọn phiên bản có mốc gần thời điểm mua nhất.
   - Số bản ghi bị loại được ghi vào attributes của handoff.
4. **Trích dẫn.** Output chỉ cite ref của các tool mà kết luận thật sự dựa vào (`CITED_TOOLS` trong `workflow.py`), cộng thêm `get_policy`.
   - Ví dụ: vấn đề giao hàng cite order, item và shipment, không cite payment.
   - Ref của từng claim là tập con của `evidence_refs` trong output.
5. **Không bịa dữ liệu.** Không có ref nào được tạo ra hay sửa đổi. Thiếu evidence thì giữ nguyên là thiếu, không suy đoán để lấp vào.
   - Riêng refund timeline trả "not found" nghĩa là chưa có refund nào. Đây là trạng thái hợp lệ, không phải thiếu evidence.

### Quy tắc chẩn đoán (`analysis.diagnose`)

Mọi tín hiệu dưới đây được tính trên facts trong case window:

| Issue | Tín hiệu |
| --- | --- |
| `canceled_order_paid` / `unavailable_order_paid` | Order status là `canceled` / `unavailable` và có capture |
| `late_delivery_seller` | Giao sau ngày dự kiến, và carrier nhận hàng sau shipping limit của seller |
| `late_delivery_logistics` | Giao sau ngày dự kiến, và seller bàn giao đúng hạn |
| `valid_split_payment` | Có một tập capture thuộc nhiều `payment_sequential` khác nhau, tổng bằng giá trị order (price + freight) |
| `duplicate_charge` | Cùng một số tiền bị capture từ 2 lần trở lên, và tổng vượt giá trị order |
| `payment_mismatch` | Có event `reconciliation_mismatch` đang `open` |
| `refund_pending` / `refund_failed` | Có refund event với status `pending` / `failed` |

Thứ tự áp dụng:
1. Topic khách khai có tín hiệu → xác nhận topic đó. Confidence 0.95, hoặc 0.85 nếu có tín hiệu khác cùng bật do bản ghi chồng lấn.
2. Topic khách khai không có tín hiệu → xét issue liên quan, ví dụ khai `duplicate_charge` nhưng thực ra là `valid_split_payment` (confidence 0.7).
3. Không có issue liên quan nào → `unsupported_claim`.
4. Thiếu evidence bắt buộc → `insufficient_evidence`.

Sau khi có issue, policy-agent áp `rules[issue]` của policy:
- `case_status`, action và refund lấy trực tiếp từ rule. Refund được giới hạn không vượt số tiền đã capture.
- `party_id` của seller lấy từ evidence của chính case, không dùng id mẫu trong policy.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Có, tối đa 3 lần/call (90 giây mỗi lần) | Coi domain đó là thiếu; nếu là domain bắt buộc → `insufficient_evidence` | handoff `*_EVIDENCE_MISSING`; `policy_decided` với `rule_basis=REQUIRED_EVIDENCE_MISSING` |
| Mất kết nối MCP | Có, reconnect tối đa 5 lần | Rollback trace của case đang dở (trace được đệm theo case). Chạy lại case đó cùng **5 case vừa xong trước đó** trong session mới, vì các call ngay trước lúc rớt có thể chưa vào audit của server | Trace và output của các case chạy lại được thay mới; không có event trùng |
| Not found | Không | Order không tồn tại → `insufficient_evidence`; refund không tồn tại → "chưa có refund" | handoff `ORDER_EVIDENCE_MISSING` hoặc attribute `refund_events_in_window=0` |
| Source conflict | Không | Ghi vào `data_conflicts`, chọn nguồn gốc của từng field, trừ confidence 0.1 | handoff attribute `conflicts` |
| Invalid specialist result | Không | Payload sai cấu trúc (`InvalidEvidence`) → domain coi là thiếu | handoff `*_EVIDENCE_MISSING` |
| Verifier fail | Không | Thay bằng output `insufficient_evidence` / `needs_investigation` / `escalate_manual_review`, refund 0 | `verification_completed` `FAIL` với danh sách check, handoff `REJECTED` |

## 6. Verification invariants

`verifier.verify()` chạy các check sau trước khi finalize:

| Check | Nội dung |
| --- | --- |
| `SCHEMA` | Đúng JSON Schema `l3a-output-v2` |
| `CASE_ID` | `case_id` khớp input |
| `ENTITY_SCOPE` | `order_ids` đúng bằng `[claimed_order_id]` |
| `EVIDENCE_OWNERSHIP` | Có ít nhất 1 ref, và mọi ref nằm trong ledger của case |
| `CLAIM_LINKAGE` | Ref của mỗi claim nằm trong `evidence_refs` của output |
| `MONEY_TOTAL` | `recommended_refund_brl` bằng tổng `refund_lines` (sai số ±0.01) |
| `REFUND_CAP` | Refund không vượt số tiền đã capture trong window |
| `STATUS_REFUND` | Chỉ `action_required` mới có refund > 0 hoặc có refund line |
| `STATUS_ACTION` | Luôn có ít nhất 1 action |
| `DUPLICATE_ACTIONS` | Không có action trùng |
| `SELLER_RESPONSIBILITY` | Seller bị quy trách nhiệm phải có trong `seller_ids` |
| `CONFIDENCE_BOUNDS` | Mọi confidence nằm trong [0, 1] |

CLI kiểm tra lại schema và `case_id` lần nữa trước khi ghi file.

## 7. Reproducibility

- **Môi trường:** Python 3.11; dependency theo `pyproject.toml`. Không có model hay LLM, không có random seed. `event_id` là ngẫu nhiên nhưng không ảnh hưởng kết quả.
- **Tính quyết định:** cùng một bộ MCP evidence luôn cho ra cùng output. Chỉ có `evidence_ref` và `event_id` thay đổi giữa các lần chạy.
- **Concurrency:**
  - Các case chạy tuần tự trên một MCP session.
  - Các agent vẫn chạy đồng thời (order + policy, rồi payment + shipment). Tuy vậy, `EvidenceGateway` dùng lock nên **mỗi thời điểm chỉ có 1 MCP call**: server audit nhận traffic tuần tự như một client đơn luồng.
  - Mỗi case gọi 7 tool.
- **Giới hạn:** timeout 90 giây/call × 3 lần; reconnect tối đa 5 lần (mỗi lần chạy lại 5 case gần nhất); tối đa 40 A2A message/case.
- **Lệnh:**

  ```bash
  pip install -e ".[dev]"
  day09 validate-inputs
  day09 run                 # hoặc: day09 run --case L3A_CASE_001  (chỉ khi phát triển)
  day09 validate
  day09 package --output dist/submission.zip
  pytest -q && ruff check .
  ```

- Không ghi API key vào code, output hay trace. Packager chặn nếu phát hiện chuỗi `sk-team-...`.
