"""app.py - Streamlit HITL Approval Interface for Day27."""
import json
import os
import sys

# Ensure local imports work regardless of cwd
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

from graph import graph, CUSTOMER_DB, CONFIDENCE_THRESHOLD

AUDIT_FILE = os.path.join(os.path.dirname(__file__), "audit_log.json")

st.set_page_config(page_title="HITL Review - Day27", layout="wide", page_icon="🛡️")

# --- Keep graph singleton in session_state to avoid recompilation on rerun ---
if "graph" not in st.session_state:
    st.session_state.graph = graph

# --- Helper to load audit log ---
def load_audit():
    if not os.path.exists(AUDIT_FILE):
        return []
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


# --- Sidebar ---
with st.sidebar:
    st.title("⚙️ Cấu hình")
    st.markdown(f"**Confidence threshold**: `{CONFIDENCE_THRESHOLD}`")
    st.markdown("**Hard policy**: `increase_credit_limit` → luôn `Human Review`")
    st.markdown("---")
    st.markdown("**Luồng:**\n\n`Customer → evaluate → route → Low:Auto / High:Interrupt → Review → Resume → Audit`")
    if st.button("🔄 Reset audit_log.json", use_container_width=True):
        with open(AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        st.success("Đã reset audit log.")
        st.rerun()
    st.markdown("---")
    st.caption("Lab Day27 - Track3 - HITL + LangGraph + Streamlit")


st.title("🛡️ HITL Approval Dashboard — Customer Retention")
st.caption("Agent đề xuất action → Confidence Routing + Hard Rules → Human Review → Audit Log")

# --- Customer selector ---
col1, col2 = st.columns([2, 1])
with col1:
    customer_options = list(CUSTOMER_DB.keys()) + ["CUST999 (unknown)"]
    selected = st.selectbox("Chọn Customer ID", customer_options, index=0)
    # Normalize unknown
    customer_id_raw = selected.split(" ")[0]
    st.markdown(f"**TOI / Churn (mock):** `{CUSTOMER_DB.get(customer_id_raw, {'toi':'N/A','churn':'N/A'})}`")
with col2:
    thread_id = st.text_input("Thread ID (theo customer)", value=customer_id_raw, help="Dùng làm thread_id cho checkpointer")
    reviewer_id = st.text_input("Reviewer ID", value="operator_01")

config = {"configurable": {"thread_id": thread_id}}

# --- Evaluate button ---
if st.button("▶️ Evaluate Customer", type="primary", use_container_width=True):
    # Reset: invoke fresh. If thread already has state, this will start new run for that thread.
    # To allow re-evaluate, we invoke with new input.
    with st.spinner("Agent đang đánh giá..."):
        result = st.session_state.graph.invoke(
            {"customer_id": customer_id_raw, "human_decision": None},
            config=config,
        )
    st.success(f"Done. Result keys: {list(result.keys())}")
    st.rerun()

# --- Inspect pending state ---
snapshot = st.session_state.graph.get_state(config)

st.divider()

# Show current state if exists
if snapshot.values:
    st.subheader("📋 Current Graph State")
    # snapshot.values contains the merged state
    values = snapshot.values
    # snapshot.next indicates next nodes to execute (tuple)
    next_nodes = snapshot.next

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Customer ID", values.get("customer_id", "-"))
    c2.metric("Proposed Action", values.get("proposed_action", "-"))
    conf = values.get("confidence_score", 0)
    c3.metric("Confidence", f"{conf:.2f}" if isinstance(conf, float) else str(conf))
    c4.metric("Next Node", str(next_nodes) if next_nodes else "END / Completed")

    # Reasoning card
    st.info(f"**Reasoning:** {values.get('reasoning', '-')}")

    # Human decision status
    hd = values.get("human_decision")
    if hd:
        st.markdown(f"**Human decision (state):** `{hd}`")

    # Check if interrupted before high-risk
    is_pending_high_risk = "execute_high_risk_action" in (next_nodes or ())

    if is_pending_high_risk:
        st.warning("⏸️ **Graph đang PAUSE trước `execute_high_risk_action` — cần Human Review**")
        # Action Card
        with st.container(border=True):
            st.markdown("### 🃏 Action Card (Pending Review)")
            st.markdown(f"- **Customer ID:** `{values.get('customer_id')}`")
            st.markdown(f"- **Proposed Action:** `{values.get('proposed_action')}`")
            st.markdown(f"- **Confidence:** `{values.get('confidence_score')}` (threshold {CONFIDENCE_THRESHOLD})")
            st.markdown(f"- **Reasoning:** {values.get('reasoning')}")
            st.markdown(f"- **Reviewer:** `{reviewer_id}`")

            st.markdown("---")
            st.markdown("**Chọn hành động:**")

            edit_value = st.text_input(
                "Edit proposed_action (chỉ khi bấm Edit)",
                value=values.get("proposed_action", ""),
                help="Ví dụ: increase_credit_limit = 20,000,000 hoặc send_email",
            )

            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button("✅ Approve", use_container_width=True, type="primary"):
                    st.session_state.graph.update_state(config, {"human_decision": "approve"})
                    # Also update reviewer context if needed (audit uses operator_01 fixed)
                    result = st.session_state.graph.invoke(None, config=config)
                    st.success(f"Approved → resumed. Result: {result}")
                    st.rerun()
            with b2:
                if st.button("❌ Reject", use_container_width=True):
                    st.session_state.graph.update_state(config, {"human_decision": "reject"})
                    result = st.session_state.graph.invoke(None, config=config)
                    st.error(f"Rejected → aborted. Result: {result}")
                    st.rerun()
            with b3:
                if st.button("✏️ Edit & Approve", use_container_width=True):
                    # Edit means human changes proposed_action then approves
                    st.session_state.graph.update_state(
                        config, {"human_decision": "edit", "proposed_action": edit_value}
                    )
                    result = st.session_state.graph.invoke(None, config=config)
                    st.info(f"Edited to `{edit_value}` → resumed. Result: {result}")
                    st.rerun()
    else:
        # No pending high risk - check if completed
        if not next_nodes:
            # Graph finished
            if values.get("human_decision") == "auto_executed":
                st.success("✅ Auto-executed (low-risk, high confidence). Xem Audit Log bên dưới.")
            elif values.get("human_decision") in ("approved", "rejected", "edited"):
                st.success(f"✅ High-risk flow completed with decision: `{values.get('human_decision')}`")
            else:
                st.info("Graph đã hoàn thành. Chưa có interrupt pending.")
        else:
            st.info(f"Next: {next_nodes}")

    # Debug expander
    with st.expander("🔍 Debug: full snapshot"):
        st.json(
            {
                "values": values,
                "next": list(next_nodes) if next_nodes else [],
                "config": config,
            }
        )
else:
    st.info("Chưa có state. Hãy bấm **Evaluate Customer** để bắt đầu.")

st.divider()
st.subheader("📜 Audit Trail (audit_log.json)")
audit = load_audit()
if audit:
    st.dataframe(audit, use_container_width=True)
    with st.expander("Xem JSON thô"):
        st.json(audit)
else:
    st.caption("Chưa có audit entry nào.")

# Footer: routing rules visualization
st.divider()
with st.expander("📖 Routing Rules & Reflection"):
    st.markdown("""
**Rule 1 - Policy Override:** `increase_credit_limit` → luôn `execute_high_risk_action` (interrupt) bất kể confidence.

**Rule 2 - Auto-Execute:** `confidence >= 0.85` + low-risk (`send_email`) → `execute_low_risk_action` auto.

**Rule 3 - Escalate:** `confidence < 0.85` → `execute_high_risk_action` (interrupt) để ép human review.

**Reflection Q1:** Nếu muốn con người rewrite email trước khi tới routing node → dùng `interrupt_after=["generate_email"]` (không phải before routing). `interrupt_before` pause trước node đích; `interrupt_after` pause sau khi generation xong để edit output rồi mới route.

**Q2 - Alert Fatigue (500 emails 0.82):** Batch review, nâng threshold calibration, thêm band `0.80-0.85` auto với sampling 10% review, grouping similar customers, UI bulk action + confidence histogram.

**Q3 - Overconfident LLM (0.95 sai):** LLM confidence là token probability không được calibrate. Cần calibrate bằng holdout set (Platt scaling / temperature scaling, isotonic regression), hoặc dùng external verifier (thu nhập từ DB, không từ LLM), hoặc ensemble.
    """)
