"""
deck.py — Boss presentation: Automated Semantic Layer

Run with:
    streamlit run deck.py
"""
import json
import streamlit as st
from pathlib import Path

ROOT = Path(__file__).parent

st.set_page_config(page_title="Semantic Layer — Deck", page_icon="🧠", layout="wide")

st.markdown("""
<style>
    .block-container { padding: 2rem 4rem; max-width: 1100px; }

    .slide-title {
        font-size: 32px; font-weight: 900; color: #0f172a;
        margin-bottom: 4px; line-height: 1.2;
    }
    .slide-sub {
        font-size: 16px; color: #64748b; margin-bottom: 28px;
    }
    .hero {
        background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
        border-radius: 16px; padding: 48px; color: white; margin-bottom: 32px;
    }
    .hero-title { font-size: 40px; font-weight: 900; margin-bottom: 8px; }
    .hero-sub   { font-size: 18px; opacity: 0.8; }

    .card {
        background: #f8fafc; border: 1px solid #e2e8f0;
        border-radius: 12px; padding: 20px 24px; height: 100%;
    }
    .card-title { font-weight: 700; font-size: 15px; color: #0f172a; margin-bottom: 6px; }
    .card-body  { font-size: 13px; color: #475569; line-height: 1.6; }

    .step-badge {
        display: inline-block;
        background: #2563eb; color: white;
        border-radius: 50%; width: 28px; height: 28px;
        text-align: center; line-height: 28px;
        font-weight: 700; font-size: 14px; margin-right: 8px;
    }
    .step-title { font-size: 18px; font-weight: 800; color: #0f172a; }
    .step-sub   { font-size: 13px; color: #64748b; }

    .stat-big { font-size: 42px; font-weight: 900; color: #2563eb; }
    .stat-lbl { font-size: 13px; color: #64748b; margin-top: 2px; }

    .highlight {
        background: #eff6ff; border-left: 4px solid #2563eb;
        border-radius: 0 8px 8px 0; padding: 12px 16px;
        font-size: 14px; color: #1e40af; margin: 12px 0;
    }
    .warn {
        background: #fffbeb; border-left: 4px solid #f59e0b;
        border-radius: 0 8px 8px 0; padding: 12px 16px;
        font-size: 14px; color: #92400e; margin: 12px 0;
    }
    code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 12px; }

    .nav-btn { margin-top: 32px; }
    .divider { border-top: 2px solid #e2e8f0; margin: 28px 0; }
</style>
""", unsafe_allow_html=True)

# ── Slide state ────────────────────────────────────────────────────────────────

SLIDES = [
    "Overview",
    "The Problem",
    "The Solution",
    "Step 1 — Scanner",
    "Step 2 — Catalog Agent (Call A)",
    "Step 3 — Catalog Agent (Call B)",
    "Step 4 — Human Review",
    "The Two Outputs",
    "Data Privacy",
    "Numbers & Cost",
]

if "slide" not in st.session_state:
    st.session_state.slide = 0

def goto(n):
    st.session_state.slide = max(0, min(n, len(SLIDES) - 1))


# ── Sidebar nav ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🧠 Semantic Layer")
    st.caption("Jupiter Money · Product Analytics")
    st.divider()
    for i, name in enumerate(SLIDES):
        active = i == st.session_state.slide
        if st.button(
            f"{'→ ' if active else '   '}{name}",
            key=f"nav_{i}",
            use_container_width=True,
            type="primary" if active else "secondary",
        ):
            goto(i)

    st.divider()
    st.caption(f"Slide {st.session_state.slide + 1} / {len(SLIDES)}")


s = st.session_state.slide


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 0 — Overview
# ─────────────────────────────────────────────────────────────────────────────

if s == 0:
    st.markdown("""
    <div class="hero">
        <div class="hero-title">🧠 Automated Semantic Layer</div>
        <div class="hero-sub">Turning raw product data into a documented, queryable knowledge base — automatically</div>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("""<div class="card">
            <div class="card-title">🔍 What it does</div>
            <div class="card-body">Reads any product analytics database and automatically produces human-readable documentation for every event, column, and metric.</div>
        </div>""", unsafe_allow_html=True)
    with c2:
        st.markdown("""<div class="card">
            <div class="card-title">⚡ How fast</div>
            <div class="card-body">27 events, 91 columns, full property mapping — documented end-to-end in under 6 minutes. Zero manual work required.</div>
        </div>""", unsafe_allow_html=True)
    with c3:
        st.markdown("""<div class="card">
            <div class="card-title">🌍 How generic</div>
            <div class="card-body">Works on any product DB — fintech, healthtech, SaaS. No hardcoding. The system discovers event types, categories, and properties from the data itself.</div>
        </div>""", unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("### Pipeline at a glance")
    st.markdown("""
    ```
    Raw DB  →  Step 1: Scanner     →  raw_schema.json   (zero LLM, zero cost)
            →  Step 2: LLM Call A  →  column docs        (1 LLM call per table)
            →  Step 3: LLM Call B  →  event docs         (1 LLM call per event table)
            →  Step 4: Human UI    →  reviewed catalog   (edit, enrich, approve)
            →  Output              →  catalog.json + catalog.md  (feeds analytics agent)
    ```
    """)


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 1 — The Problem
# ─────────────────────────────────────────────────────────────────────────────

elif s == 1:
    st.markdown('<div class="slide-title">The Problem</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">What analysts face every day without a semantic layer</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Without this system")
        st.markdown("""
- Analyst sees `vkyc_failed` in a table — has no idea what it means
- Has to ask in Slack "what columns does this event have?"
- Doesn't know `failure_reason` has 19 specific values — guesses wrong ones
- Writes SQL with `event_status` instead of `transaction_status` — query fails
- Doesn't know `user_id LIKE 'test_%'` rows should be excluded
- Every new analyst learns this from scratch
        """)

    with c2:
        st.markdown("#### With this system")
        st.markdown("""
- Every event has a plain-English name, description, and business tags
- Every property is documented per event with valid values
- PII columns are flagged — never appear in analyst output
- SQL conventions (DuckDB dialect, time windows) are pre-loaded
- Business glossary (DAU, MAU, Activation) defined once, used everywhere
- Analytics agent uses this as its system prompt — zero hallucination
        """)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("""
    <div class="highlight">
    The semantic layer is the difference between an analytics agent that <strong>guesses</strong>
    and one that <strong>knows</strong> your data.
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 2 — The Solution
# ─────────────────────────────────────────────────────────────────────────────

elif s == 2:
    st.markdown('<div class="slide-title">The Solution</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">4-step pipeline from raw DB to documented catalog</div>', unsafe_allow_html=True)

    steps = [
        ("1", "Scanner", "Zero LLM · Zero cost",
         "Reads DuckDB. Profiles every table, column, cardinality, null %. Detects which tables are event logs. Discovers all 27 event types. Maps sparse columns to the specific events they belong to. Auto-detects category columns from naming patterns."),
        ("2", "LLM Call A — Columns", "1 call per table · gpt-4o-mini",
         "Sends column metadata (names, types, distinct values). Gets back: human display names, descriptions, PII flags, analysis priority (high/medium/low/skip), value meanings for categorical columns, suggested metrics."),
        ("3", "LLM Call B — Events", "1 call per event table · gpt-4o-mini",
         "Sends all event names + per-event property lists. Gets back: display name, description, analysis tags (funnel/retention/rca/cohort), and per-property descriptions written from each event's specific context."),
        ("4", "Human Review UI", "Streamlit app",
         "Analysts browse events grouped by category. They see what the LLM auto-generated (🤖) vs what a human edited (✏️). They can add business context the LLM couldn't know, define glossary terms, set exclusion filters, add metrics. One click saves and regenerates the catalog."),
    ]

    for icon, title, sub, body in steps:
        st.markdown(
            f'<div style="display:flex;gap:16px;margin-bottom:20px;padding:16px;'
            f'background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px">'
            f'<div><span class="step-badge">{icon}</span></div>'
            f'<div><div class="step-title">{title}</div>'
            f'<div class="step-sub">{sub}</div>'
            f'<div style="font-size:13px;color:#475569;margin-top:6px">{body}</div>'
            f'</div></div>',
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 3 — Scanner Input / Output
# ─────────────────────────────────────────────────────────────────────────────

elif s == 3:
    st.markdown('<div class="slide-title">Step 1 — Scanner</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">Pure Python. Reads the DB directly. Zero LLM. Zero cost.</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Input — Raw DuckDB")
        st.markdown("""
What the scanner receives:
- Path to `jupiter.duckdb`
- No schema, no config, no hints
        """)
        st.markdown("**What it auto-discovers:**")
        st.markdown("""
- 2 tables (`events`, `users`)
- 893,010 rows in events
- 112 columns total
- `events` is an event log (has `user_id` + `timestamp`)
- 27 distinct event types from actual data
- 91 sparse columns (null > 5%) — candidates for per-event properties
- `event_category` is a grouping column (3 values, name contains "category")
        """)

    with c2:
        st.markdown("#### Output — `raw_schema.json`")
        st.code("""{
  "events": {
    "row_count": 893010,
    "is_event_table": true,
    "event_names": [
      "aadhaar_number_entered",
      "app_install",
      "bureau_pull_completed",
      "transaction_reconciled",
      "vkyc_failed",
      ...27 total
    ],
    "event_groups": {
      "aadhaar_number_entered": {
        "event_category": "onboarding"
      },
      "transaction_reconciled": {
        "event_category": "payments"
      }
    },
    "event_properties": {
      "vkyc_failed": [
        "failure_reason",
        "attempt_number",
        "can_reschedule"
      ],
      "transaction_reconciled": [
        "amount", "merchant_name",
        "transaction_channel", ...28 total
      ]
    }
  }
}""", language="json")

    st.markdown("""
    <div class="highlight">
    Key insight: <code>event_properties</code> is discovered by querying which columns are
    non-null for each event — e.g. <code>cibil_score</code> only appears in
    <code>bureau_pull_completed</code> rows. No manual mapping needed.
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 4 — LLM Call A
# ─────────────────────────────────────────────────────────────────────────────

elif s == 4:
    st.markdown('<div class="slide-title">Step 2 — LLM Call A: Columns</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">1 call per table · Documents columns, priorities, value meanings, suggested metrics</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### What we send to the LLM")
        st.code("""{
  "table_name": "events",
  "row_count": 893010,
  "is_event_table": true,
  "columns": [
    {
      "name": "event_category",
      "type": "VARCHAR",
      "cardinality": 3,
      "null_pct": 0.0,
      "sample_values": [
        "engagement",
        "onboarding",
        "payments"
      ]
    },
    {
      "name": "transaction_channel",
      "type": "VARCHAR",
      "cardinality": 6,
      "null_pct": 79.4,
      "sample_values": [
        "IMPS","NACH","NEFT",
        "RTGS","UPI","debit_card"
      ]
    },
    {
      "name": "amount",
      "type": "DOUBLE",
      "cardinality": 154414,
      "null_pct": 79.4,
      "sample_values": []
    }
  ],
  "sample_rows": [
    {"event_name": "app_opened",
     "event_category": "onboarding",
     "platform": "android", ...}
  ]
}""", language="json")

    with c2:
        st.markdown("#### What the LLM returns")
        st.code("""{
  "table_display_name": "User Events Log",
  "table_description": "Stores user interaction
    events within the Jupiter Money app.",
  "table_type": "event_log",
  "columns": [
    {
      "raw_name": "event_category",
      "display_name": "Event Category",
      "description": "Business category of the
        event. Never null.",
      "is_pii": false,
      "analysis_priority": "high",
      "value_meanings": {
        "onboarding": "User onboarding flow",
        "engagement": "Post-signup engagement",
        "payments": "Payment activities"
      }
    },
    {
      "raw_name": "amount",
      "display_name": "Transaction Amount (INR)",
      "description": "Amount in rupees. Null for
        non-payment events.",
      "is_pii": false,
      "analysis_priority": "medium",
      "value_meanings": {}
    }
  ],
  "suggested_metrics": [
    {
      "name": "Transaction Success Rate",
      "sql_hint": "COUNT(CASE WHEN
        transaction_status='SUCCESS'
        THEN 1 END) / COUNT(*)"
    }
  ]
}""", language="json")


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 5 — LLM Call B
# ─────────────────────────────────────────────────────────────────────────────

elif s == 5:
    st.markdown('<div class="slide-title">Step 3 — LLM Call B: Events</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">1 call per event table · Documents all 27 events with per-event properties</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="highlight">
    Why a separate call? A single call for 27 events + 64 columns exceeded the output token limit
    — the LLM silently stopped at event 11. Splitting into two focused calls solves this cleanly.
    </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### What we send")
        st.code("""{
  "event_names": [
    "aadhaar_number_entered",
    "app_install",
    "vkyc_failed",
    "transaction_reconciled",
    ...27 total
  ],
  "event_properties": {
    "vkyc_failed": [
      {
        "name": "failure_reason",
        "type": "VARCHAR",
        "sample_values": [
          "face_mismatch",
          "blurry_document",
          "poor_connectivity",
          "agent_disconnect",
          "aadhaar_mismatch"
        ]
      },
      {
        "name": "attempt_number",
        "type": "DOUBLE",
        "sample_values": []
      },
      {
        "name": "can_reschedule",
        "type": "BOOLEAN",
        "sample_values": ["True","False"]
      }
    ]
  }
}""", language="json")

    with c2:
        st.markdown("#### What the LLM returns (per event)")
        st.code("""{
  "raw_name": "vkyc_failed",
  "display_name": "vKYC Failed",
  "description": "Fired when a video KYC
    attempt fails. Used to diagnose
    drop-off in the KYC funnel.",
  "analysis_tags": ["funnel", "rca"],
  "groups": {
    "event_category": "onboarding"
  },
  "properties": [
    {
      "raw_name": "failure_reason",
      "display_name": "Failure Reason",
      "description": "Why the vKYC call
        failed for this specific attempt.",
      "value_meanings": {
        "face_mismatch": "Face did not match
          ID document",
        "blurry_document": "Document image
          was not clear",
        "poor_connectivity": "Network issues
          during call",
        "agent_disconnect": "Agent dropped
          the call"
      }
    },
    {
      "raw_name": "attempt_number",
      "display_name": "Attempt Number",
      "description": "Which retry this was.
        Used to measure retry funnel."
    },
    {
      "raw_name": "can_reschedule",
      "display_name": "Can Reschedule",
      "description": "Whether the user is
        eligible to book a new slot."
    }
  ]
}""", language="json")


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 6 — Human Review UI
# ─────────────────────────────────────────────────────────────────────────────

elif s == 6:
    st.markdown('<div class="slide-title">Step 4 — Human Review UI</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">Streamlit app — browse, edit, approve, enrich</div>', unsafe_allow_html=True)

    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("#### What analysts see")
        st.markdown("""
**Events grouped by auto-detected category:**
```
ONBOARDING   23 events
  aadhaar_number_entered    🤖 auto
  vkyc_failed               ✏️ edited   ← amber highlight
  bureau_pull_completed     🤖 auto

ENGAGEMENT   2 events
  home_screen_viewed        🤖 auto
  notification_received     🤖 auto

PAYMENTS   1 event
  transaction_reconciled    🤖 auto
```
        """)

        st.markdown("**Filter bar:**")
        st.markdown("""
- 🔍 Search by name
- Event Category dropdown (onboarding / engagement / payments)
- Analysis tag filter (funnel, rca, retention...)
- ☑️ Show edited only
        """)

    with c2:
        st.markdown("#### What analysts can do")
        st.markdown("""
**Per event (expandable):**
- Edit display name & description
- Change analysis tags
- Edit per-property descriptions

**Metrics tab:**
- Edit LLM-suggested metrics
- Add new metrics with SQL hints

**Business Rules tab:**
- Add always-filter clauses (exclude test users)
- Define glossary terms (DAU, MAU, Activation Rate)
- Set SQL conventions (DuckDB dialect, time windows)

**Data Stats tab:**
- Event volume charts
- Events over time
- Category breakdown (donut charts)
- Raw sample rows from DB
        """)

    st.markdown("""
    <div class="highlight">
    <strong>🤖 auto</strong> = LLM generated &nbsp;|&nbsp;
    <strong>✏️ edited</strong> = Human changed &nbsp;|&nbsp;
    One click saves to <code>catalog.json</code> and regenerates <code>catalog.md</code>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 7 — The Two Outputs
# ─────────────────────────────────────────────────────────────────────────────

elif s == 7:
    st.markdown('<div class="slide-title">The Two Outputs</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">Same source of truth, two consumers</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("""
        <div style="background:#f0f9ff;border:2px solid #2563eb;border-radius:12px;padding:20px">
        <div style="font-size:18px;font-weight:800;color:#1e40af;margin-bottom:8px">📄 catalog.json</div>
        <div style="font-size:13px;color:#1e40af;margin-bottom:12px">Read by Python code</div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("""
**What it contains:**
- Structured JSON — every event, column, property, metric
- Valid filter values per column (`failure_reason` has exactly 19 values)
- PII flags — columns that must never appear in output
- Per-event property map — which columns belong to which event

**How it's used:**
- Analytics agent reads this to validate SQL before running
- Prevents hallucinated column names or invalid filter values
- Drives autocomplete and query suggestions
        """)

    with c2:
        st.markdown("""
        <div style="background:#f0fdf4;border:2px solid #16a34a;border-radius:12px;padding:20px">
        <div style="font-size:18px;font-weight:800;color:#15803d;margin-bottom:8px">📝 catalog.md</div>
        <div style="font-size:13px;color:#15803d;margin-bottom:12px">Injected into LLM system prompt</div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("""
**What it contains:**
- Plain English — event names, descriptions, tags
- Key dimensions and their meanings
- Business glossary (DAU, MAU, Activation Rate)
- Always-filter rules (exclude test/bot users)
- SQL conventions (DuckDB dialect, time windows)

**How it's used:**
- Prepended to every analytics agent conversation
- LLM "knows" the data model before the user asks anything
- ~8,000 tokens — small enough to fit in context, complete enough to be accurate
        """)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("""
    <div class="highlight">
    Both files are regenerated together with one click. Human edits are preserved — the catalog
    agent only overwrites if you re-run the full pipeline.
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 8 — Data Privacy
# ─────────────────────────────────────────────────────────────────────────────

elif s == 8:
    st.markdown('<div class="slide-title">Data Privacy</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">What goes to the LLM — and what stays in your environment</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### ✅ What we send")
        st.markdown("""
- Column names, types, cardinality, null %
- Distinct values for **low-cardinality columns only** (≤ 100 unique values)
  - e.g. `["onboarding", "payments", "engagement"]`
  - e.g. `["UPI", "IMPS", "NEFT", "RTGS"]`
- 3 sample rows (configurable)
- Table row count
        """)

        st.markdown("#### 🚫 What we never send")
        st.markdown("""
- Any column with cardinality > 100 gets **no sample values**
  - `user_id` (15,000 unique) → `sample_values: []`
  - `amount` (154,414 unique) → `sample_values: []`
  - `aadhaar_masked` → `sample_values: []`
  - `transaction_id` → `sample_values: []`
- No raw financial amounts, no phone numbers, no national IDs
        """)

    with c2:
        st.markdown("#### ⚠️ Current risk")
        st.markdown("""
        <div class="warn">
        The 3 sample rows contain real <code>user_id</code> values and whatever
        columns happen to be populated. For a production deployment, these should be
        scrubbed before the LLM call.
        </div>
        """, unsafe_allow_html=True)

        st.markdown("#### 🔧 Easy fix (next step)")
        st.markdown("""
Add a scrubbing step in `scanner.py` before building the prompt:

```python
# Replace PII in sample rows
for row in sample_rows:
    for col in pii_columns:
        if col in row:
            row[col] = "***"
```

Or: skip sample rows entirely — the column metadata alone gives the LLM enough signal.
        """)


# ─────────────────────────────────────────────────────────────────────────────
# SLIDE 9 — Numbers & Cost
# ─────────────────────────────────────────────────────────────────────────────

elif s == 9:
    st.markdown('<div class="slide-title">Numbers & Cost</div>', unsafe_allow_html=True)
    st.markdown('<div class="slide-sub">Jupiter Money — actual run metrics</div>', unsafe_allow_html=True)

    # Stats
    c1, c2, c3, c4 = st.columns(4)
    stats = [
        ("893K", "Events in DB"),
        ("27", "Event types documented"),
        ("112", "Columns documented"),
        ("91", "Sparse columns mapped\nto specific events"),
    ]
    for col, (num, lbl) in zip([c1,c2,c3,c4], stats):
        col.markdown(
            f'<div style="text-align:center;padding:16px;background:#f8fafc;'
            f'border:1px solid #e2e8f0;border-radius:12px">'
            f'<div class="stat-big">{num}</div>'
            f'<div class="stat-lbl">{lbl}</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Time breakdown")
        st.markdown("""
| Step | Time |
|------|------|
| Scanner (DB profiling) | ~3 sec |
| LLM Call A — columns × 2 tables | ~90 sec |
| LLM Call B — 27 events | ~240 sec |
| Write files | < 1 sec |
| **Total** | **~6 min** |
        """)

    with c2:
        st.markdown("#### Cost breakdown")
        st.markdown("""
| | Tokens | Cost |
|--|--------|------|
| Call A (events table) | ~8K in / ~3K out | ~$0.002 |
| Call B (events) | ~12K in / ~5K out | ~$0.003 |
| Call A (users table) | ~3K in / ~1K out | ~$0.001 |
| **Total per run** | | **~$0.006** |

Model: `gpt-4o-mini`

Re-run cost if schema changes: same ~$0.006
        """)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("#### What's generic — works on any product DB")
    st.markdown("""
- Event type discovery → from actual data, not config
- Category column detection → from column name patterns (`category`, `type`, `area`, `flow`...)
- Per-event property mapping → from null patterns in the data
- LLM descriptions → inferred from column names + distinct values + sample rows
    """)

    st.markdown("""
    <div class="highlight">
    Give it a healthtech DB with <code>condition_type</code>, <code>procedure_category</code>,
    <code>visit_flow</code> and the same pipeline produces a fully documented semantic layer
    for that domain — no changes to code.
    </div>
    """, unsafe_allow_html=True)


# ── Navigation buttons ─────────────────────────────────────────────────────────

st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
c1, c2, c3 = st.columns([1, 3, 1])
with c1:
    if s > 0:
        if st.button("← Previous", use_container_width=True):
            goto(s - 1)
            st.rerun()
with c2:
    st.markdown(
        f'<div style="text-align:center;color:#94a3b8;font-size:13px;padding-top:8px">'
        f'{s+1} / {len(SLIDES)} — {SLIDES[s]}</div>',
        unsafe_allow_html=True,
    )
with c3:
    if s < len(SLIDES) - 1:
        if st.button("Next →", use_container_width=True, type="primary"):
            goto(s + 1)
            st.rerun()
