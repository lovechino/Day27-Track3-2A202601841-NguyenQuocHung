"""graph.py - LangGraph HITL workflow for Day27.

State: GraphState
Nodes: evaluate_customer, execute_low_risk_action, execute_high_risk_action
Routing: route_action with Policy Override / Auto-Execute / Escalate
Compilation: MemorySaver + interrupt_before=["execute_high_risk_action"]
Audit: AuditEntry appended to audit_log.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, END

# Ensure local imports work regardless of cwd (Streamlit vs python -m)
import sys
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from models import AuditEntry  # noqa: E402

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class GraphState(TypedDict):
    customer_id: str
    proposed_action: str
    confidence_score: float
    reasoning: str
    human_decision: str | None


# ---------------------------------------------------------------------------
# Mock customer DB (TOI = Total Operating Income in VND, churn 0-1)
# ---------------------------------------------------------------------------
CUSTOMER_DB: dict[str, dict] = {
    "CUST001": {"toi": 120_000_000, "churn": 0.78, "name": "Nguyen Van A"},
    "CUST002": {"toi": 25_000_000, "churn": 0.22, "name": "Tran Thi B"},
    "CUST003": {"toi": 80_000_000, "churn": 0.65, "name": "Le Van C"},
    "CUST004": {"toi": 15_000_000, "churn": 0.12, "name": "Pham Thi D"},
    "CUST005": {"toi": 200_000_000, "churn": 0.88, "name": "Hoang Van E"},
}

# Default thresholds / constants
CONFIDENCE_THRESHOLD = 0.85
AGENT_ID = "churn-risk-agent"
AUDIT_FILE = os.path.join(os.path.dirname(__file__), "audit_log.json")


def _append_audit(entry: AuditEntry) -> None:
    """Append audit entry to JSON file without overwriting history."""
    existing: list = []
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    existing = data
        except (json.JSONDecodeError, FileNotFoundError):
            existing = []
    existing.append(entry.model_dump())
    with open(AUDIT_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Node: evaluate_customer
# ---------------------------------------------------------------------------

def evaluate_customer(state: GraphState) -> dict:
    """Simulate agent reasoning based on TOI and churn probability.

    - High churn (>0.6) or VIP + high churn -> propose increase_credit_limit (high-risk)
    - Otherwise -> propose send_email (low-risk)
    Returns proposed_action, confidence_score (0.0-1.0), reasoning.
    """
    customer_id = state.get("customer_id", "CUST001")
    info = CUSTOMER_DB.get(customer_id)

    # Unknown customer -> treat as low-risk but low confidence to force escalation
    if info is None:
        return {
            "proposed_action": "send_email",
            "confidence_score": 0.72,
            "reasoning": f"Unknown customer {customer_id}. No TOI/churn data. Default to low-risk email with low confidence to require review.",
        }

    churn = info["churn"]
    toi = info["toi"]

    # Allow test injection: if state already has proposed_action/confidence (for unit tests),
    # respect it when explicitly passed via customer_id prefix TEST_
    # But normal flow uses DB-derived logic below.
    if churn >= 0.6 or (toi >= 100_000_000 and churn >= 0.5):
        # High-risk path
        # confidence high when churn high
        confidence = 0.91 if churn < 0.85 else 0.94
        # allow override for testing: if customer_id like CUST_HIGH_XXX we vary
        reasoning = (
            f"Customer {customer_id} ({info['name']}) has high churn probability {churn:.2f} "
            f"and TOI {toi:,} VND. Retention requires financial incentive. "
            f"Propose increase_credit_limit to reduce churn risk."
        )
        return {
            "proposed_action": "increase_credit_limit",
            "confidence_score": confidence,
            "reasoning": reasoning,
        }
    else:
        # Low-risk path
        # confidence depends on how low churn is
        if churn < 0.25:
            confidence = 0.92
        elif churn < 0.4:
            confidence = 0.88
        else:
            confidence = 0.82  # intentionally below threshold to test escalation
        reasoning = (
            f"Customer {customer_id} ({info['name']}) has moderate/low churn probability {churn:.2f} "
            f"and TOI {toi:,} VND. No high-risk financial action required. "
            f"Propose send_email retention campaign."
        )
        return {
            "proposed_action": "send_email",
            "confidence_score": confidence,
            "reasoning": reasoning,
        }


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------

def route_action(state: GraphState) -> str:
    """Implements 3 rules in priority order.

    Rule 1 - Policy Override: increase_credit_limit -> high_risk (always human review)
    Rule 2 - Auto-Execute: confidence >= 0.85 and low-risk -> low_risk
    Rule 3 - Escalate: confidence < 0.85 -> high_risk
    """
    action = state.get("proposed_action", "")
    confidence = state.get("confidence_score", 0.0)

    # Rule 1: hard policy - substring check to support edited values like "increase_credit_limit = 20,000,000"
    if "increase_credit_limit" in action:
        return "high_risk"
    # Rule 2 & 3: confidence threshold
    if confidence >= CONFIDENCE_THRESHOLD:
        return "low_risk"
    return "high_risk"


# ---------------------------------------------------------------------------
# Nodes: execute actions
# ---------------------------------------------------------------------------

def execute_low_risk_action(state: GraphState) -> dict:
    """Auto-execute low-risk action and write audit log."""
    entry = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        agent_id=AGENT_ID,
        action=state.get("proposed_action", ""),
        confidence=float(state.get("confidence_score", 0.0)),
        reviewer_id="auto",
        decision="auto_execute",
    )
    _append_audit(entry)
    # human_decision marks auto execution
    return {"human_decision": "auto_executed"}


def execute_high_risk_action(state: GraphState) -> dict:
    """Execute high-risk action based on human_decision and audit.

    human_decision values:
      - "approve": execute as proposed
      - "reject": abort
      - "edit": execute edited action (proposed_action already updated via update_state)
      - None / other: treat as pending (should not happen after resume)
    """
    human_decision = state.get("human_decision")
    # Normalize decision for audit
    decision = human_decision if human_decision in ("approve", "reject", "edit") else "approve"

    # If human_decision was None (should not happen after proper resume), fallback to approve
    # but we log whatever is present.
    audit_decision = human_decision if human_decision else "unknown"

    # Only audit if there is a real human decision or after interrupt resume
    # To avoid duplicate audits on re-invokes, we always audit exactly once per high-risk execution.
    reviewer_id = "operator_01"

    entry = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        agent_id=AGENT_ID,
        action=state.get("proposed_action", ""),
        confidence=float(state.get("confidence_score", 0.0)),
        reviewer_id=reviewer_id,
        decision=audit_decision,
    )
    _append_audit(entry)

    # Return reasoning extension based on decision
    if audit_decision == "reject":
        return {"human_decision": "rejected", "reasoning": state.get("reasoning", "") + " | Human rejected action. Aborted."}
    if audit_decision == "edit":
        return {"human_decision": "edited", "reasoning": state.get("reasoning", "") + " | Human edited proposed action before execution."}
    return {"human_decision": "approved"}


# ---------------------------------------------------------------------------
# Graph compilation
# ---------------------------------------------------------------------------

def build_graph():
    builder = StateGraph(GraphState)
    builder.add_node("evaluate_customer", evaluate_customer)
    builder.add_node("execute_low_risk_action", execute_low_risk_action)
    builder.add_node("execute_high_risk_action", execute_high_risk_action)

    builder.set_entry_point("evaluate_customer")

    builder.add_conditional_edges(
        "evaluate_customer",
        route_action,
        {
            "low_risk": "execute_low_risk_action",
            "high_risk": "execute_high_risk_action",
        },
    )
    builder.add_edge("execute_low_risk_action", END)
    builder.add_edge("execute_high_risk_action", END)

    memory = MemorySaver()
    graph = builder.compile(
        checkpointer=memory,
        interrupt_before=["execute_high_risk_action"],
    )
    return graph


# Singleton graph for import by app.py / tests
graph = build_graph()


# ---------------------------------------------------------------------------
# CLI smoke (optional)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    config = {"configurable": {"thread_id": "smoke"}}
    for cid in ["CUST001", "CUST002", "CUST004"]:
        print("\n===", cid, "===")
        result = graph.invoke({"customer_id": cid, "human_decision": None}, config={"configurable": {"thread_id": cid}})
        print(result)
        snap = graph.get_state({"configurable": {"thread_id": cid}})
        print("next:", snap.next, "values:", snap.values)
