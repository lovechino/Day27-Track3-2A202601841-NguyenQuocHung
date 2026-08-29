# Day27-Track3-2A202601841-NguyenQuocHung — HITL with LangGraph + Streamlit

Lab Human-in-the-Loop: Agent đề xuất retention action → Confidence Routing + Hard Policy → Interrupt → Streamlit Approval (Approve/Reject/Edit) → Audit Log.

## Kiến trúc

```
Customer Data
      |
      v
Agent Reasoning (evaluate_customer)
      | proposed_action, confidence_score, reasoning
      v
Confidence Routing + Hard Rules (route_action)
      |
      +-----------------------------+
      |                             |
      v                             v
Auto Execute                  Interrupt Graph
 (send_email,                (increase_credit_limit
  conf>=0.85)                  hoặc conf<0.85)
                                    |
                                    v
                             Streamlit Review
                              /      |      \
                         Approve   Reject    Edit
                            |        |        |
                            +--------+--------+
                                     |
                                     v
                                Resume Graph
                                     |
                                     v
                                 Audit Log (audit_log.json)
```

## Cấu trúc repo

```
Day27-Track3-2A202601841-NguyenQuocHung/
├── models.py        # AuditEntry (Pydantic)
├── graph.py         # GraphState, evaluate_customer, route_action, execute_* , MemorySaver + interrupt_before
├── app.py           # Streamlit approval interface
├── audit_log.json   # Append-only audit trail
├── requirements.txt
└── .venv/           # local venv (gitignored)
```

## 1. Định nghĩa State & Audit Schema

**GraphState** (`graph.py:25`):
```python
class GraphState(TypedDict):
    customer_id: str
    proposed_action: str
    confidence_score: float
    reasoning: str
    human_decision: str | None
```
Persist xuyên suốt workflow, kể cả khi `interrupt_before` pause. `human_decision` được cập nhật từ Streamlit via `graph.update_state()`.

**AuditEntry** (`models.py:5`):
```python
class AuditEntry(BaseModel):
    timestamp: str      # ISO8601 UTC
    agent_id: str       # churn-risk-agent
    action: str
    confidence: float
    reviewer_id: str    # operator_01 / auto
    decision: str       # approve / reject / edit / auto_execute
```

## 2. Agent Reasoning Node

`evaluate_customer(state)` (`graph.py:70`) giả lập TOI + churn:

| Customer | TOI | Churn | Output |
|----------|-----|-------|--------|
| CUST001 | 120M | 0.78 | `increase_credit_limit`, 0.91 |
| CUST002 | 25M  | 0.22 | `send_email`, 0.92 |
| CUST003 | 80M  | 0.65 | `increase_credit_limit`, 0.91 |
| CUST004 | 15M  | 0.12 | `send_email`, 0.92 |
| CUST005 | 200M | 0.88 | `increase_credit_limit`, 0.94 |
| Unknown | N/A | N/A | `send_email`, 0.72 (force escalation) |

`confidence_score` luôn `0.0-1.0`.

## 3. Confidence Routing & Hard Rules

`route_action(state)` (`graph.py:134`):

- **Rule 1 - Policy Override**: `if "increase_credit_limit" in action → high_risk` (luôn human review, bất kể confidence 0.99). Substring check để hỗ trợ Edit `increase_credit_limit = 20,000,000`.
- **Rule 2 - Auto-Execute**: `confidence >= 0.85` + `send_email` → `low_risk`
- **Rule 3 - Escalate**: `confidence < 0.85` → `high_risk`

`CONFIDENCE_THRESHOLD = 0.85`

## 4. Compile Graph với Interrupts

```python
from langgraph.checkpoint.memory import MemorySaver
memory = MemorySaver()
graph = builder.compile(
    checkpointer=memory,
    interrupt_before=["execute_high_risk_action"]
)
```
- `MemorySaver` bắt buộc để không mất state khi chờ human.
- `interrupt_before` dừng TRƯỚC node high-risk; `snapshot.next == ("execute_high_risk_action",)` và `graph.get_state(config)` vẫn giữ `proposed_action/confidence/reasoning`.

## 5. Streamlit Approval Interface

**Chạy:**
```bash
# Tạo venv (đã có sẵn .venv)
python -m venv .venv
.\.venv\Scripts\activate        # Windows
# hoặc source .venv/bin/activate # Linux/Mac

pip install -r requirements.txt
streamlit run app.py
```

**Luồng UI** (`app.py`):
1. Chọn `Customer ID` + `Thread ID` + `Reviewer ID`
2. Bấm **Evaluate Customer** → `graph.invoke({"customer_id":...}, config)`
3. `graph.get_state(config)` → hiển thị Action Card (proposed_action, confidence, reasoning)
4. Ba nút:
   - **Approve** → `graph.update_state(config, {"human_decision":"approve"}); graph.invoke(None, config)`
   - **Reject**  → `graph.update_state(config, {"human_decision":"reject"}); graph.invoke(None, config)`
   - **Edit**    → sửa text `proposed_action` (vd `increase_credit_limit = 20,000,000`) → `graph.update_state(config, {"human_decision":"edit","proposed_action":edited}); graph.invoke(None, config)`
5. Xem `Audit Trail` dataframe bên dưới.

`st.session_state.graph` giữ singleton để không recompile mỗi rerun.

## 6. Audit Log

Mỗi `execute_*_action` gọi `_append_audit()` (đọc → append → ghi) để không overwrite history. File `audit_log.json` dạng:

```json
[
  {
    "timestamp": "2026-08-29T04:48:33.935556+00:00",
    "agent_id": "churn-risk-agent",
    "action": "increase_credit_limit = 20,000,000",
    "confidence": 0.94,
    "reviewer_id": "operator_01",
    "decision": "edit"
  }
]
```

Production nên thay bằng append-only DB (PostgreSQL).

## Kiểm thử nhanh (không cần UI)

```bash
.venv/Scripts/python -c "
import sys; sys.path.insert(0,'.')
from graph import graph, route_action
# Hard rule test
assert route_action({'proposed_action':'increase_credit_limit','confidence_score':0.99})=='high_risk'
# Auto
assert route_action({'proposed_action':'send_email','confidence_score':0.90})=='low_risk'
# Escalation
assert route_action({'proposed_action':'send_email','confidence_score':0.82})=='high_risk'
print('routing ok')

# Interrupt test
cfg={'configurable':{'thread_id':'CUST001'}}
graph.invoke({'customer_id':'CUST001','human_decision':None}, config=cfg)
assert graph.get_state(cfg).next==('execute_high_risk_action',)
graph.update_state(cfg, {'human_decision':'approve'})
graph.invoke(None, config=cfg)
print('HITL flow ok')
"
```

## Reflection

**Q1:** Dùng `interrupt_after` (sau generation) nếu muốn rewrite email trước routing. `interrupt_before` pause trước node đích; `interrupt_after=["generate_email"]` pause ngay sau khi email được tạo, cho phép edit rồi mới `route_action`.

**Q2 - Alert Fatigue (500 emails 0.82):** Batching + sampling (chỉ review 10% ngẫu nhiên), nâng/conf calibration, band `0.80-0.85` auto với human spot-check, grouping theo cohort, UI bulk approve, histogram confidence + smart filter.

**Q3 - Overconfident 0.95:** LLM logit không calibrate với thực tế. Cần calibration (temperature scaling, Platt, isotonic trên holdout), external verifier (TOI từ DB), ensemble hoặc judge model, và không dùng raw confidence làm single gate.

## Dependency

```
langgraph>=0.2.0
langchain-core
pydantic>=2.0
streamlit>=1.30.0
python-dotenv
```

Cài: `pip install -r requirements.txt` (Python 3.10+).

## Checklist lab

- [x] GraphState đủ 5 keys, persist qua interrupt
- [x] AuditEntry Pydantic 6 fields
- [x] evaluate_customer trả proposed_action/confidence/reasoning (0.0-1.0)
- [x] route_action: hard rule > threshold, auto, escalate
- [x] MemorySaver + interrupt_before=["execute_high_risk_action"]
- [x] Streamlit Approve/Reject/Edit + update_state + invoke(None)
- [x] Audit log append không overwrite
