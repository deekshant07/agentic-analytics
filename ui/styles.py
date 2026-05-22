"""ui/styles.py — Global CSS injection for the Streamlit app."""
import streamlit as st


def inject_styles() -> None:
    st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

/* ─────────────────────────────────────────────
   BASE & PAGE
───────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; }

[data-testid="stAppViewContainer"] {
    background: #E8EFFC !important;
    font-family: 'Inter', sans-serif;
}
[data-testid="stMain"] { background: #E8EFFC !important; }
.main .block-container {
    background: #E8EFFC !important;
    padding-top: 0 !important;
    max-width: 960px;
}

[data-testid="stHeader"] { display: none !important; }
header[data-testid="stHeader"] { display: none !important; }

/* ─────────────────────────────────────────────
   SIDEBAR — dark narrow panel
───────────────────────────────────────────── */
[data-testid="stSidebar"] {
    background: #1C2131 !important;
    border-right: none !important;
    min-width: 220px !important;
    max-width: 220px !important;
}
[data-testid="stSidebar"] > div:first-child { padding-top: 0 !important; }

.sb-brand {
    display: flex; align-items: center; gap: 0.65rem;
    padding: 1.1rem 1rem 0.9rem;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    margin-bottom: 0.5rem;
}
.sb-brand-icon {
    width: 32px; height: 32px; border-radius: 8px;
    background: linear-gradient(135deg, #2EB8A6 0%, #1A8A9E 100%);
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; color: white; flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(46,184,166,0.35);
}
.sb-brand-name {
    font-size: 0.88rem; font-weight: 700; color: #F1F5F9;
    font-family: 'Inter', sans-serif; letter-spacing: -0.01em;
}
.sb-brand-sub {
    font-size: 0.62rem; color: rgba(148,163,184,0.5);
    font-family: 'Inter', sans-serif; margin-top: 0.05rem;
}

.sb-section-label {
    font-size: 0.6rem; font-weight: 600; color: rgba(100,116,139,0.7);
    text-transform: uppercase; letter-spacing: 0.1em;
    padding: 0.6rem 1rem 0.25rem; font-family: 'Inter', sans-serif;
}
.sb-nav-item {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.5rem 1rem; border-radius: 7px; margin: 0.08rem 0.5rem;
    color: rgba(148,163,184,0.7); font-size: 0.82rem;
    font-family: 'Inter', sans-serif; cursor: pointer;
    transition: all 0.13s; font-weight: 500;
}
.sb-nav-item:hover { background: rgba(255,255,255,0.06); color: #F1F5F9; }
.sb-nav-active { background: rgba(59,91,219,0.18) !important; color: #93C5FD !important; }

.sb-session-item {
    padding: 0.45rem 1rem; border-radius: 7px;
    margin: 0.04rem 0.5rem; cursor: pointer; transition: background 0.12s;
}
.sb-session-item:hover { background: rgba(255,255,255,0.05); }
.sb-session-title {
    font-size: 0.77rem; color: #94A3B8; font-family: 'Inter', sans-serif;
    font-weight: 500; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; max-width: 170px;
}
.sb-session-date { font-size: 0.65rem; color: rgba(100,116,139,0.5); }
.sb-session-active .sb-session-title { color: #93C5FD !important; font-weight: 600 !important; }
.sb-active-badge {
    display: inline-block; font-size: 0.57rem; font-weight: 700;
    color: #34D399; background: rgba(52,211,153,0.12);
    border: 1px solid rgba(52,211,153,0.25); border-radius: 999px;
    padding: 0.06rem 0.4rem; margin-left: 0.35rem;
    font-family: 'Inter', sans-serif;
}

/* ─────────────────────────────────────────────
   TOP BAR — breadcrumb style
───────────────────────────────────────────── */
.main-topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 1rem 0 0.9rem;
    border-bottom: 1px solid #D4DFF0;
    margin-bottom: 1.5rem;
}
.topbar-breadcrumb {
    display: flex; align-items: center; gap: 0.4rem;
    font-family: 'Inter', sans-serif;
}
.topbar-crumb-parent {
    font-size: 0.8rem; color: #94A3B8; font-weight: 500;
    cursor: pointer;
}
.topbar-crumb-parent:hover { color: #3B5BDB; }
.topbar-crumb-sep { font-size: 0.78rem; color: #CBD5E1; }
.topbar-crumb-current {
    font-size: 0.8rem; color: #1A2332; font-weight: 600;
}
.main-topbar-actions { display: flex; align-items: center; gap: 0.4rem; }
.topbar-btn {
    display: inline-flex; align-items: center; gap: 0.3rem;
    padding: 0.38rem 0.85rem; border-radius: 8px;
    border: 1px solid #D4DFF0; background: #FFFFFF;
    font-size: 0.77rem; color: #374151; cursor: pointer;
    font-family: 'Inter', sans-serif; font-weight: 500;
    transition: all 0.13s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.topbar-btn:hover { background: #F1F5F9; border-color: #BBC8DC; color: #1A2332; }
.topbar-btn-primary {
    background: #3B5BDB !important; border-color: #3B5BDB !important;
    color: white !important;
    box-shadow: 0 2px 8px rgba(59,91,219,0.3) !important;
}
.topbar-btn-primary:hover { background: #3451C7 !important; }
.topbar-icon-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; border-radius: 7px;
    border: 1px solid #D4DFF0; background: #FFFFFF;
    color: #64748B; cursor: pointer; font-size: 0.9rem;
    transition: all 0.13s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.topbar-icon-btn:hover { background: #F1F5F9; color: #1A2332; }

/* ─────────────────────────────────────────────
   KPI METRIC CARDS
───────────────────────────────────────────── */
.kpi-row { display: flex; gap: 0.9rem; margin-bottom: 1.2rem; }
.kpi-card {
    flex: 1; background: #FFFFFF;
    border: 1px solid #E2EAF4; border-radius: 12px;
    padding: 1.1rem 1.3rem 1rem;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05);
    position: relative;
}
.kpi-card-menu {
    position: absolute; top: 0.75rem; right: 0.75rem;
    font-size: 0.85rem; color: #CBD5E1; cursor: pointer;
}
.kpi-label {
    font-size: 0.75rem; color: #94A3B8; font-family: 'Inter', sans-serif;
    font-weight: 500; margin-bottom: 0.4rem;
}
.kpi-value {
    font-size: 1.8rem; font-weight: 700; color: #0F172A;
    font-family: 'Inter', sans-serif; letter-spacing: -0.03em; line-height: 1.1;
}
.kpi-delta-pos {
    font-size: 0.72rem; color: #10B981; font-weight: 600;
    margin-top: 0.3rem; font-family: 'Inter', sans-serif;
}
.kpi-delta-neg {
    font-size: 0.72rem; color: #EF4444; font-weight: 600;
    margin-top: 0.3rem; font-family: 'Inter', sans-serif;
}

/* ─────────────────────────────────────────────
   PANEL / CHART CARDS
───────────────────────────────────────────── */
.panel-card {
    background: #FFFFFF;
    border: 1px solid #E2EAF4;
    border-radius: 12px;
    padding: 1.1rem 1.4rem 1.2rem;
    margin-bottom: 1rem;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05);
}
.panel-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.8rem;
}
.panel-title {
    font-size: 0.88rem; font-weight: 600; color: #1A2332;
    font-family: 'Inter', sans-serif; letter-spacing: -0.01em;
}
.panel-actions {
    display: flex; align-items: center; gap: 0.5rem;
}
.panel-action-btn {
    display: inline-flex; align-items: center; gap: 0.25rem;
    font-size: 0.72rem; color: #64748B; font-family: 'Inter', sans-serif;
    font-weight: 500; cursor: pointer; padding: 0.25rem 0.55rem;
    border-radius: 6px; border: 1px solid #E2EAF4; background: #F8FAFC;
    transition: all 0.12s;
}
.panel-action-btn:hover { background: #EEF2FF; border-color: #C7D2FE; color: #3B5BDB; }

/* ─────────────────────────────────────────────
   RESPONSE / ANSWER CARD
───────────────────────────────────────────── */
.response-card {
    background: #FFFFFF;
    border: 1px solid #E2EAF4;
    border-radius: 14px;
    padding: 1.5rem 1.8rem;
    margin: 0.5rem 0 1.6rem;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06);
}
.response-badge {
    display: inline-flex; align-items: center; gap: 0.35rem;
    font-size: 0.65rem; font-weight: 700; color: #3B5BDB;
    text-transform: uppercase; letter-spacing: 0.08em;
    font-family: 'Inter', sans-serif; margin-bottom: 1rem;
    padding: 0.22rem 0.7rem;
    background: #EEF2FF; border: 1px solid #C7D2FE;
    border-radius: 999px;
}

/* ─────────────────────────────────────────────
   INSIGHT CALLOUT
───────────────────────────────────────────── */
.insight-callout {
    background: #F0F7FF;
    border: 1px solid #BFDBFE;
    border-left: 3px solid #3B82F6;
    border-radius: 10px;
    padding: 0.9rem 1.2rem;
    margin: 1rem 0;
    font-size: 0.87rem; color: #1E3A5F;
    font-family: 'Inter', sans-serif; line-height: 1.65;
}
.insight-callout-title {
    font-size: 0.67rem; font-weight: 700; color: #3B82F6;
    text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 0.35rem;
}

/* ─────────────────────────────────────────────
   EVIDENCE HEADER
───────────────────────────────────────────── */
.evidence-header {
    font-size: 0.68rem; font-weight: 700; color: #94A3B8;
    text-transform: uppercase; letter-spacing: 0.1em;
    font-family: 'Inter', sans-serif;
    padding: 0.9rem 0 0.4rem;
    border-top: 1px solid #F1F5F9; margin-top: 1rem;
}

/* ─────────────────────────────────────────────
   CHART CONTAINER
───────────────────────────────────────────── */
.chart-wrap {
    background: #FAFBFE;
    border: 1px solid #E8EFF9;
    border-radius: 10px;
    padding: 0.9rem 0.5rem 0.5rem;
    margin: 0.4rem 0 0.8rem;
}
.chart-title {
    font-size: 0.8rem; font-weight: 600; color: #374151;
    font-family: 'Inter', sans-serif; padding: 0 0.5rem 0.35rem;
}
.chart-subtitle {
    font-size: 0.7rem; color: #94A3B8;
    font-family: 'Inter', sans-serif; padding: 0 0.5rem 0.7rem;
}

/* ─────────────────────────────────────────────
   METRIC CARD (inline, in response)
───────────────────────────────────────────── */
.metric-card {
    background: #F8FAFF;
    border: 1px solid #DBEAFE;
    border-radius: 10px;
    padding: 0.9rem 1.4rem;
    margin: 0.5rem 0;
    display: inline-block; min-width: 155px;
    box-shadow: 0 1px 4px rgba(59,91,219,0.07);
}
.metric-value { font-size: 1.85rem; font-weight: 800; color: #3B5BDB; line-height: 1.1; }
.metric-label { font-size: 0.72rem; color: #94A3B8; margin-top: 0.3rem;
                font-family: 'Inter', sans-serif; font-weight: 500; }

/* ─────────────────────────────────────────────
   FEEDBACK BAR
───────────────────────────────────────────── */
.fb-bar {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.45rem 0.2rem 0.6rem;
    border-top: 1px solid #EEF2F8;
    margin-top: 0.4rem;
}
.fb-label {
    font-size: 0.72rem; color: #94A3B8;
    font-family: 'Inter', sans-serif; font-weight: 500;
    margin-right: 0.15rem; white-space: nowrap;
}
.fb-done {
    font-size: 0.72rem; color: #10B981;
    font-family: 'Inter', sans-serif; font-weight: 500;
    padding: 0.45rem 0.2rem 0.6rem;
    border-top: 1px solid #EEF2F8; margin-top: 0.4rem;
}
.fb-input { flex-direction: column; align-items: flex-start; }

/* Make feedback thumb buttons tiny and borderless */
.fb-bar [data-testid="stButton"] button,
.fb-input [data-testid="stButton"] button {
    padding: 0.15rem 0.4rem !important;
    font-size: 0.85rem !important;
    background: transparent !important;
    border: 1px solid #E2EAF4 !important;
    border-radius: 6px !important;
    color: #94A3B8 !important;
    min-height: unset !important;
    height: 28px !important;
    box-shadow: none !important;
}
.fb-bar [data-testid="stButton"] button:hover,
.fb-input [data-testid="stButton"] button:hover {
    background: #F1F5F9 !important;
    border-color: #CBD5E1 !important;
    color: #374151 !important;
}

/* ─────────────────────────────────────────────
   SUGGESTION CHIPS
───────────────────────────────────────────── */
.chip-row { display: flex; flex-wrap: wrap; gap: 0.45rem; margin: 0.9rem 0 1.3rem; }
.chip {
    display: inline-block; padding: 0.35rem 0.85rem;
    border-radius: 999px; border: 1px solid #D4DFF0;
    background: #FFFFFF; color: #64748B; font-size: 0.77rem;
    cursor: pointer; transition: all 0.13s; white-space: nowrap;
    font-family: 'Inter', sans-serif;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}
.chip:hover { background: #EEF2FF; border-color: #A5B4FC; color: #3B5BDB; }

/* ─────────────────────────────────────────────
   CHAT MESSAGES
───────────────────────────────────────────── */
[data-testid="stChatMessage"] {
    background: transparent !important;
    border: none !important; padding: 0 !important;
    box-shadow: none !important;
}
[data-testid="stChatMessage"] > div:first-child { display: none !important; }

.user-msg-wrap { margin: 1.3rem 0 0.5rem; }
.user-msg-label {
    font-size: 0.67rem; font-weight: 600; color: #94A3B8;
    text-transform: uppercase; letter-spacing: 0.08em;
    font-family: 'Inter', sans-serif; margin-bottom: 0.3rem;
}
.user-msg-text {
    font-size: 0.93rem; color: #1A2332;
    font-family: 'Inter', sans-serif; line-height: 1.65;
}

/* ─────────────────────────────────────────────
   STEP PILLS
───────────────────────────────────────────── */
.step-line { font-size: 0.81rem; color: #94A3B8; line-height: 1.8;
             font-family: 'Inter', sans-serif; }

/* ─────────────────────────────────────────────
   EXPANDERS
───────────────────────────────────────────── */
[data-testid="stExpander"] {
    background: #FAFBFE !important;
    border: 1px solid #E2EAF4 !important;
    border-radius: 9px !important;
}
[data-testid="stExpander"] summary {
    color: #64748B !important; font-size: 0.79rem;
    font-family: 'Inter', sans-serif !important;
}

/* ─────────────────────────────────────────────
   DEBUG PIPELINE
───────────────────────────────────────────── */
.debug-pipeline-trace {
    background: #0F172A !important; border: 1px solid #1E293B !important;
    border-radius: 10px !important; padding: 1rem 1.1rem !important;
    margin: 0.5rem 0 !important; font-family: Inter, system-ui, sans-serif !important;
    color: #E2E8F0 !important;
}
.debug-pipeline-trace .dp-flow {
    margin-bottom: 0.85rem !important; padding: 0.55rem 0.7rem !important;
    background: #0C1220 !important; border: 1px solid #1E3A5F !important;
    border-radius: 8px !important; color: #CBD5E1 !important;
}
.debug-pipeline-trace .dp-flow li { color: #CBD5E1 !important; font-size: 0.78rem !important; }
.debug-pipeline-trace .dp-step-title { color: #CBD5E1 !important; font-weight: 600 !important; }
.debug-pipeline-trace .dp-k { color: #64748B !important; }
.debug-pipeline-trace .dp-v { color: #E2E8F0 !important; }
.debug-pipeline-trace .dp-box {
    background: #0F1623 !important; border: 1px solid #1E293B !important;
    border-radius: 6px !important; padding: 0.55rem 0.75rem !important;
}
.debug-pipeline-trace pre { color: #A5B4FC !important; }
.debug-pipeline-trace summary { color: #3B82F6 !important; }

/* ─────────────────────────────────────────────
   DATAFRAME & ALERTS
───────────────────────────────────────────── */
[data-testid="stDataFrame"] {
    border-radius: 10px; overflow: hidden;
    border: 1px solid #E2EAF4 !important;
}
[data-testid="stAlert"] {
    border-radius: 10px !important; font-size: 0.85rem;
    background: #EEF2FF !important; border: 1px solid #C7D2FE !important;
}

/* ─────────────────────────────────────────────
   PROSE TYPOGRAPHY
───────────────────────────────────────────── */
[data-testid="stMarkdownContainer"] p {
    color: #374151; font-size: 0.92rem; line-height: 1.75;
    font-family: 'Inter', sans-serif;
}
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3 {
    color: #0F172A; font-family: 'Inter', sans-serif;
}
[data-testid="stMarkdownContainer"] li {
    color: #374151; font-size: 0.91rem;
    font-family: 'Inter', sans-serif; line-height: 1.75;
}

/* ─────────────────────────────────────────────
   CHAT INPUT
───────────────────────────────────────────── */
[data-testid="stChatInput"] {
    background: #FFFFFF !important;
    border: 1px solid #D4DFF0 !important;
    border-radius: 12px !important;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08) !important;
}
[data-testid="stChatInput"] textarea {
    background: transparent !important; border: none !important;
    color: #1A2332 !important; font-size: 0.92rem;
    font-family: 'Inter', sans-serif !important;
}
[data-testid="stChatInput"] textarea::placeholder { color: #94A3B8 !important; }
[data-testid="stChatInput"]:focus-within {
    border-color: #3B5BDB !important;
    box-shadow: 0 0 0 3px rgba(59,91,219,0.1), 0 2px 12px rgba(0,0,0,0.08) !important;
}

/* ─────────────────────────────────────────────
   TOGGLE & SELECTBOX
───────────────────────────────────────────── */
.stToggle > label { font-size: 0.77rem; color: #64748B; }
[data-testid="stSelectbox"] > div > div {
    background: #FFFFFF !important; border: 1px solid #D4DFF0 !important;
    color: #374151 !important; border-radius: 8px !important;
}
[data-testid="stSelectbox"] label { color: #64748B !important; font-size: 0.77rem !important; }

/* ─────────────────────────────────────────────
   BUTTONS
───────────────────────────────────────────── */
[data-testid="stButton"] button {
    font-family: 'Inter', sans-serif !important;
    font-size: 0.82rem !important; border-radius: 8px !important;
    transition: all 0.15s !important;
}
[data-testid="stButton"] button[kind="secondary"] {
    background: #FFFFFF !important; color: #374151 !important;
    border: 1px solid #D4DFF0 !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
}
[data-testid="stButton"] button[kind="secondary"]:hover {
    background: #F8FAFC !important; border-color: #BBC8DC !important;
}
[data-testid="stButton"] button[kind="primary"] {
    background: #3B5BDB !important; color: white !important;
    border: none !important;
    box-shadow: 0 2px 8px rgba(59,91,219,0.25) !important;
}
[data-testid="stButton"] button[kind="primary"]:hover {
    background: #3451C7 !important;
    box-shadow: 0 4px 14px rgba(59,91,219,0.4) !important;
    transform: translateY(-1px) !important;
}

/* ─────────────────────────────────────────────
   SIDEBAR BUTTONS
───────────────────────────────────────────── */
[data-testid="stSidebar"] [data-testid="stButton"] button {
    background: rgba(59,91,219,0.12) !important; color: #93C5FD !important;
    border: 1px solid rgba(59,91,219,0.25) !important;
    border-radius: 8px !important; font-size: 0.82rem !important;
}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover {
    background: rgba(59,91,219,0.22) !important;
    border-color: rgba(59,91,219,0.4) !important;
}

/* ─────────────────────────────────────────────
   CODE BLOCKS
───────────────────────────────────────────── */
[data-testid="stCodeBlock"] {
    background: #0F172A !important; border: 1px solid #1E293B !important;
    border-radius: 10px !important;
}
[data-testid="stCodeBlock"] code { font-size: 0.81rem !important; }

/* ─────────────────────────────────────────────
   HOME: USE-CASE CARDS
───────────────────────────────────────────── */
.uc-card {
    background: #FFFFFF;
    border: 1px solid #E2EAF4;
    border-radius: 14px;
    padding: 1.3rem 1.4rem 1.1rem;
    margin-bottom: 0.2rem;
    min-height: 210px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05);
    transition: box-shadow 0.18s ease, border-color 0.18s ease, transform 0.18s ease;
}
.uc-card:hover {
    box-shadow: 0 6px 24px rgba(59,91,219,0.12);
    border-color: #A5B4FC;
    transform: translateY(-2px);
}

/* ─────────────────────────────────────────────
   HOME: HERO
───────────────────────────────────────────── */
.home-hero {
    text-align: center; padding: 2.5rem 0 1.6rem;
    font-family: 'Inter', sans-serif;
}

/* ─────────────────────────────────────────────
   SCROLLBAR
───────────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #CBD5E1; border-radius: 999px; }
::-webkit-scrollbar-thumb:hover { background: #94A3B8; }
</style>
""", unsafe_allow_html=True)
