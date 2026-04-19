"""
catalog_editor.py — Visual editor for the analytics catalog.

Run with:
    streamlit run catalog_editor.py
"""

import json
import sys
import streamlit as st
import altair as alt
import pandas as pd
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "semantic-layer"))

CATALOG_CANDIDATES = [
    ROOT / "catalog.json",
    ROOT / "semantic-layer" / "catalog.json",
]

ALL_TAGS   = ["funnel", "retention", "cohort", "rca", "segmentation"]
TAG_COLORS = {
    "funnel":       "#4f8ef7",
    "retention":    "#22c55e",
    "cohort":       "#a855f7",
    "rca":          "#f97316",
    "segmentation": "#06b6d4",
}
PRIORITY_COLORS = {
    "high":   ("#f59e0b", "white"),
    "medium": ("#3b82f6", "white"),
    "low":    ("#94a3b8", "white"),
    "skip":   ("#e2e8f0", "#64748b"),
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_catalog_path() -> Path | None:
    for p in CATALOG_CANDIDATES:
        if p.exists():
            return p
    return None


def find_db_path(catalog_path: Path) -> Path | None:
    for p in catalog_path.parent.glob("*.duckdb"):
        return p
    for p in catalog_path.parent.parent.glob("*.duckdb"):
        return p
    return None


@st.cache_data(show_spinner=False)
def load_catalog(path: str) -> dict:
    return json.loads(Path(path).read_text())


def save_and_regenerate(catalog: dict, path: Path):
    # Write session-state business context back into catalog before saving
    biz = catalog.setdefault("__business_context__", {})
    biz["custom_events"] = st.session_state.get("custom_events", [])
    biz["exclusions"]    = st.session_state.get("exclusions", {})
    biz["industry"]      = st.session_state.get("biz_industry", biz.get("industry", ""))
    biz["company"]       = st.session_state.get("biz_company",  biz.get("company", ""))
    path.write_text(json.dumps(catalog, indent=2))
    from semantic_writer import generate_semantic_files
    generate_semantic_files(catalog, output_dir=str(path.parent))
    st.cache_data.clear()


def tag_badge(tag: str) -> str:
    color = TAG_COLORS.get(tag, "#64748b")
    return (
        f'<span style="background:{color};color:white;padding:2px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600;margin-right:4px">{tag}</span>'
    )


def priority_badge(p: str) -> str:
    bg, tc = PRIORITY_COLORS.get(p, ("#e2e8f0", "#64748b"))
    return (
        f'<span style="background:{bg};color:{tc};padding:1px 7px;'
        f'border-radius:8px;font-size:11px;font-weight:600">{p}</span>'
    )


def source_badge(edited: bool) -> str:
    if edited:
        return '<span title="Human edited" style="background:#fef3c7;color:#92400e;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid #fcd34d">✏️ edited</span>'
    return '<span title="LLM generated" style="background:#f0f9ff;color:#0369a1;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600;border:1px solid #bae6fd">🤖 auto</span>'


def is_event_edited(table_name: str, raw_name: str, current: dict) -> bool:
    """Compare current event to the version on disk to detect human edits."""
    orig = st.session_state.get("original_catalog", {})
    orig_events = {e["raw_name"]: e for e in orig.get(table_name, {}).get("events", [])}
    if raw_name not in orig_events:
        return False
    orig_ev = orig_events[raw_name]
    for field in ("display_name", "description", "analysis_tags"):
        if current.get(field) != orig_ev.get(field):
            return True
    # Check property descriptions
    orig_props = {p["raw_name"]: p for p in orig_ev.get("properties", [])}
    for prop in current.get("properties", []):
        op = orig_props.get(prop["raw_name"], {})
        if prop.get("description") != op.get("description"):
            return True
    return False


# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Catalog Editor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .event-title { font-size: 15px; font-weight: 700; color: #0f172a; }
    .raw-name {
        font-family: monospace; font-size: 12px;
        background: #f1f5f9; color: #475569;
        padding: 2px 8px; border-radius: 6px;
    }
    .section-label {
        font-size: 11px; font-weight: 700; color: #94a3b8;
        text-transform: uppercase; letter-spacing: 0.08em;
        margin: 16px 0 6px;
    }
    .table-chip {
        font-size: 11px; background: #f1f5f9; color: #475569;
        padding: 2px 8px; border-radius: 6px; font-family: monospace;
    }
    .stat-card {
        background: #f8fafc; border: 1px solid #e2e8f0;
        border-radius: 10px; padding: 16px; text-align: center;
    }
    .stat-num { font-size: 28px; font-weight: 800; color: #0f172a; }
    .stat-label { font-size: 12px; color: #64748b; margin-top: 2px; }
</style>
""", unsafe_allow_html=True)


# ── Load catalog ───────────────────────────────────────────────────────────────

catalog_path = find_catalog_path()
if not catalog_path:
    st.error("catalog.json not found. Run the pipeline first:\n```\ncd semantic-layer && python run.py ../jupiter.duckdb\n```")
    st.stop()

catalog = load_catalog(str(catalog_path))

# Session state
if "catalog" not in st.session_state:
    st.session_state.catalog = json.loads(json.dumps(catalog))

# Keep a frozen copy of what's on disk for edit detection
if "original_catalog" not in st.session_state:
    st.session_state.original_catalog = json.loads(json.dumps(catalog))

_biz = catalog.get("__business_context__", {})

if "biz_industry" not in st.session_state:
    st.session_state.biz_industry = _biz.get("industry", "")
if "biz_company" not in st.session_state:
    st.session_state.biz_company = _biz.get("company", "")

if "custom_events" not in st.session_state:
    st.session_state.custom_events = _biz.get("custom_events", [])

if "exclusions" not in st.session_state:
    _default_excl = {
        "always_filter": [],
        "glossary": [],
        "conventions": [
            "Default time window: last 7 days unless user specifies otherwise",
            "Always use DATE(event_time) when grouping by day",
            "DuckDB syntax: use INTERVAL '7 days' not DATEADD",
        ],
    }
    st.session_state.exclusions = _biz.get("exclusions", _default_excl)


# ── Sidebar ────────────────────────────────────────────────────────────────────

valid_tables = {k: v for k, v in st.session_state.catalog.items()
                if not k.startswith("__") and "error" not in v}

with st.sidebar:
    st.markdown("## 📊 Catalog Editor")
    st.caption(f"`{catalog_path.relative_to(ROOT)}`")
    st.divider()

    table_items = []
    for tname, tdata in valid_tables.items():
        ttype   = tdata.get("table_type", "dimension")
        icon    = "⚡" if ttype == "event_log" else "👤"
        display = tdata.get("table_display_name", tname)
        table_items.append(f"{icon} {display}")

    nav_options = table_items + ["─────────────", "📊 Data Stats", "🏢 Business Context"]

    page = st.radio("Navigate", nav_options, label_visibility="collapsed")

    st.divider()

    total_events = sum(len(t.get("events", [])) for t in valid_tables.values())
    edited_count = sum(
        1 for tname, tdata in valid_tables.items()
        for ev in tdata.get("events", [])
        if is_event_edited(tname, ev["raw_name"], ev)
    )
    c1, c2 = st.columns(2)
    c1.metric("Events", total_events)
    c2.metric("Edited", edited_count)

    st.divider()
    if st.button("💾 Save & Regenerate MD", use_container_width=True, type="primary"):
        save_and_regenerate(st.session_state.catalog, catalog_path)
        st.session_state.original_catalog = json.loads(json.dumps(st.session_state.catalog))
        st.success("Saved!")


# ── Resolve selected table ─────────────────────────────────────────────────────

selected_table_name = None
selected_table_data = None

for tname, tdata in valid_tables.items():
    display = tdata.get("table_display_name", tname)
    ttype   = tdata.get("table_type", "dimension")
    icon    = "⚡" if ttype == "event_log" else "👤"
    if page == f"{icon} {display}":
        selected_table_name = tname
        selected_table_data = tdata
        break


# ─────────────────────────────────────────────────────────────────────────────
# TABLE VIEW
# ─────────────────────────────────────────────────────────────────────────────

if selected_table_name and selected_table_data:
    ttype        = selected_table_data.get("table_type", "dimension")
    display_name = selected_table_data.get("table_display_name", selected_table_name)

    type_color = "#4f8ef7" if ttype == "event_log" else "#22c55e"
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">'
        f'<span style="font-size:22px;font-weight:800;color:#0f172a">{display_name}</span>'
        f'<span style="background:{type_color};color:white;padding:2px 10px;border-radius:8px;font-size:12px;font-weight:600">{ttype}</span>'
        f'<span class="table-chip">{selected_table_name}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.caption(selected_table_data.get("table_description", ""))
    st.divider()

    # ── EVENT LOG ──────────────────────────────────────────────────────────────
    if ttype == "event_log":
        events = selected_table_data.get("events", [])

        all_group_keys = sorted({k for ev in events for k in ev.get("groups", {}).keys()})

        filter_cols = st.columns([3] + [2] * len(all_group_keys) + [2])
        with filter_cols[0]:
            search = st.text_input("🔍 Search", placeholder="vkyc, transaction...", label_visibility="collapsed")
        group_filters = {}
        for i, gkey in enumerate(all_group_keys):
            values = sorted({ev.get("groups", {}).get(gkey) for ev in events if ev.get("groups", {}).get(gkey)})
            with filter_cols[i + 1]:
                group_filters[gkey] = st.selectbox(
                    gkey.replace("_", " ").title(), ["All"] + values, key=f"gf_{gkey}"
                )
        with filter_cols[-1]:
            tag_filter = st.multiselect("Tags", ALL_TAGS, placeholder="All tags", label_visibility="collapsed")

        show_edited_only = st.checkbox("Show edited only")

        def matches(ev):
            if show_edited_only and not is_event_edited(selected_table_name, ev["raw_name"], ev):
                return False
            if search and search.lower() not in ev["raw_name"].lower() \
                    and search.lower() not in ev.get("display_name", "").lower():
                return False
            for gkey, gval in group_filters.items():
                if gval != "All" and ev.get("groups", {}).get(gkey) != gval:
                    return False
            if tag_filter and not any(t in ev.get("analysis_tags", []) for t in tag_filter):
                return False
            return True

        filtered = [ev for ev in events if matches(ev)]

        def render_event(ev):
            raw    = ev["raw_name"]
            tags   = ev.get("analysis_tags", [])
            props  = ev.get("properties", [])
            grps   = ev.get("groups", {})
            edited = is_event_edited(selected_table_name, raw, ev)

            tags_html = "".join(tag_badge(t) for t in tags) if tags else \
                '<span style="color:#cbd5e1;font-size:11px">no tags</span>'
            grp_html = "".join(
                f'<span style="background:#f1f5f9;color:#64748b;padding:1px 7px;'
                f'border-radius:6px;font-size:11px;margin-right:4px">{v}</span>'
                for v in grps.values()
            )
            sbadge = source_badge(edited)
            border = "#fbbf24" if edited else "#e2e8f0"

            st.markdown(
                f'<div style="border-left:3px solid {border};padding-left:10px;'
                f'display:flex;align-items:center;gap:8px;margin-bottom:2px;flex-wrap:wrap">'
                f'<span class="event-title">{ev.get("display_name", raw)}</span>'
                f'<span class="raw-name">{raw}</span>'
                f'{grp_html}{tags_html}{sbadge}</div>',
                unsafe_allow_html=True,
            )

            label = f"Edit · {len(props)} properties" if props else "Edit"
            with st.expander(label, expanded=False):
                c1, c2 = st.columns([3, 1])
                with c1:
                    ev["display_name"] = st.text_input(
                        "Display name", value=ev.get("display_name", raw),
                        key=f"dn_{selected_table_name}_{raw}"
                    )
                    ev["description"] = st.text_area(
                        "Description", value=ev.get("description", ""),
                        height=80, key=f"desc_{selected_table_name}_{raw}"
                    )
                with c2:
                    ev["analysis_tags"] = st.multiselect(
                        "Tags", ALL_TAGS,
                        default=[t for t in tags if t in ALL_TAGS],
                        key=f"tags_{selected_table_name}_{raw}"
                    )
                    if grps:
                        st.markdown('<div class="section-label" style="margin-top:8px">Groups</div>',
                                    unsafe_allow_html=True)
                        for gkey, gval in grps.items():
                            ev["groups"][gkey] = st.text_input(
                                gkey, value=gval, key=f"grp_{selected_table_name}_{raw}_{gkey}"
                            )

                if props:
                    st.markdown('<div class="section-label">Event Properties</div>', unsafe_allow_html=True)
                    for pi, prop in enumerate(props):
                        pc1, pc2 = st.columns([2, 3])
                        with pc1:
                            st.markdown(
                                f'<div style="font-size:13px;padding:6px 0">'
                                f'<code>{prop["raw_name"]}</code><br>'
                                f'<span style="color:#64748b;font-size:12px">'
                                f'{prop.get("display_name","")}</span></div>',
                                unsafe_allow_html=True,
                            )
                        with pc2:
                            prop["description"] = st.text_input(
                                "desc", value=prop.get("description", ""),
                                label_visibility="collapsed",
                                key=f"pdesc_{selected_table_name}_{raw}_{pi}"
                            )
            st.markdown("---")

        st.caption(f"Showing **{len(filtered)}** of {len(events)} events")
        st.divider()

        if all_group_keys and group_filters.get(all_group_keys[0], "All") == "All":
            primary_key = all_group_keys[0]
            group_values = sorted({ev.get("groups", {}).get(primary_key, "Other") for ev in filtered})
            for gval in group_values:
                group_events = [ev for ev in filtered
                                if ev.get("groups", {}).get(primary_key, "Other") == gval]
                edited_in_group = sum(1 for ev in group_events
                                      if is_event_edited(selected_table_name, ev["raw_name"], ev))
                edit_note = f" · {edited_in_group} edited" if edited_in_group else ""
                st.markdown(
                    f'<div class="section-label">{gval.replace("_"," ").title()} &nbsp;'
                    f'<span style="font-weight:400;color:#94a3b8">'
                    f'{len(group_events)} events{edit_note}</span></div>',
                    unsafe_allow_html=True,
                )
                for ev in group_events:
                    render_event(ev)
        else:
            for ev in filtered:
                render_event(ev)

    # ── DIMENSION TABLE ────────────────────────────────────────────────────────
    else:
        columns  = selected_table_data.get("columns", [])
        pri_order = {"high": 0, "medium": 1, "low": 2, "skip": 3}
        sorted_cols = sorted(columns, key=lambda c: pri_order.get(c.get("analysis_priority", "medium"), 1))

        high_cols = [c for c in columns if c.get("analysis_priority") == "high"]
        pii_cols  = [c for c in columns if c.get("is_pii")]
        s1, s2, s3 = st.columns(3)
        s1.metric("Total columns", len(columns))
        s2.metric("★ Key dimensions", len(high_cols))
        s3.metric("PII columns", len(pii_cols))
        st.divider()

        for col in sorted_cols:
            raw      = col["raw_name"]
            pri      = col.get("analysis_priority", "medium")
            p_html   = priority_badge(pri)
            pii_html = '<span style="background:#ef4444;color:white;padding:1px 7px;border-radius:8px;font-size:11px;font-weight:600;margin-left:4px">PII</span>' \
                       if col.get("is_pii") else ""

            st.markdown(
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:2px">'
                f'<strong style="font-size:14px">{col.get("display_name", raw)}</strong>'
                f'<code style="font-size:12px;color:#64748b">{raw}</code>'
                f'{p_html}{pii_html}</div>',
                unsafe_allow_html=True,
            )

            with st.expander("Edit", expanded=False):
                c1, c2, c3 = st.columns([4, 1, 1])
                with c1:
                    col["description"] = st.text_area(
                        "Description", value=col.get("description", ""),
                        height=70, key=f"cdesc_{selected_table_name}_{raw}"
                    )
                with c2:
                    col["analysis_priority"] = st.selectbox(
                        "Priority", ["high", "medium", "low", "skip"],
                        index=["high", "medium", "low", "skip"].index(pri),
                        key=f"cpri_{selected_table_name}_{raw}"
                    )
                with c3:
                    col["is_pii"] = st.checkbox("PII", value=col.get("is_pii", False),
                                                key=f"cpii_{selected_table_name}_{raw}")
                vm = col.get("value_meanings", {})
                if vm:
                    st.markdown('<div class="section-label">Value Meanings</div>', unsafe_allow_html=True)
                    for k, v in vm.items():
                        st.markdown(f"- `{k}` → {v}")

            st.markdown("---")


# ─────────────────────────────────────────────────────────────────────────────
# DATA STATS
# ─────────────────────────────────────────────────────────────────────────────

elif page == "📊 Data Stats":
    import duckdb

    db_path = find_db_path(catalog_path)
    if not db_path:
        st.error("No .duckdb file found near catalog.json")
        st.stop()

    @st.cache_data(show_spinner="Querying database...")
    def query(sql: str) -> pd.DataFrame:
        conn = duckdb.connect(str(db_path), read_only=True)
        df = conn.execute(sql).df()
        conn.close()
        return df

    st.markdown("## Data Stats")
    st.caption(f"Source: `{db_path.name}`  ·  Catalog generated by LLM, human edits tracked below")

    # ── Table selector ─────────────────────────────────────────────────────────
    event_tables = [k for k, v in valid_tables.items() if v.get("table_type") == "event_log"]
    if not event_tables:
        st.info("No event tables found.")
        st.stop()

    sel_table = st.selectbox("Table", event_tables,
                             format_func=lambda k: valid_tables[k].get("table_display_name", k))
    tinfo = valid_tables[sel_table]
    event_name_col = "event_name"   # standard — could read from catalog

    st.divider()

    # ── Overview cards ─────────────────────────────────────────────────────────
    try:
        overview = query(f"""
            SELECT
                COUNT(*)                        AS total_events,
                COUNT(DISTINCT user_id)         AS unique_users,
                COUNT(DISTINCT {event_name_col}) AS unique_event_types,
                MIN(DATE(timestamp))            AS first_date,
                MAX(DATE(timestamp))            AS last_date
            FROM "{sel_table}"
        """)
        row = overview.iloc[0]

        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="stat-card"><div class="stat-num">{int(row.total_events):,}</div><div class="stat-label">Total Events</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="stat-card"><div class="stat-num">{int(row.unique_users):,}</div><div class="stat-label">Unique Users</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="stat-card"><div class="stat-num">{int(row.unique_event_types)}</div><div class="stat-label">Event Types</div></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="stat-card"><div class="stat-num" style="font-size:16px">{row.first_date}<br>→ {row.last_date}</div><div class="stat-label">Date Range</div></div>', unsafe_allow_html=True)
    except Exception as e:
        st.warning(f"Could not load overview: {e}")

    st.divider()

    col_left, col_right = st.columns(2)

    # ── Event distribution ─────────────────────────────────────────────────────
    with col_left:
        st.markdown("#### Event Distribution")
        try:
            df_dist = query(f"""
                SELECT {event_name_col} AS event_name, COUNT(*) AS count
                FROM "{sel_table}"
                GROUP BY 1 ORDER BY 2 DESC
            """)
            # Enrich with display names from catalog
            name_map = {e["raw_name"]: e.get("display_name", e["raw_name"])
                        for e in tinfo.get("events", [])}
            df_dist["display"] = df_dist["event_name"].map(lambda x: name_map.get(x, x))

            chart = alt.Chart(df_dist).mark_bar(color="#4f8ef7").encode(
                x=alt.X("count:Q", title="Event count"),
                y=alt.Y("display:N", sort="-x", title=None),
                tooltip=["display:N", "count:Q"]
            ).properties(height=max(250, len(df_dist) * 22))
            st.altair_chart(chart, use_container_width=True)
        except Exception as e:
            st.warning(str(e))

    # ── Events over time ───────────────────────────────────────────────────────
    with col_right:
        st.markdown("#### Events Over Time")
        try:
            df_time = query(f"""
                SELECT DATE(timestamp) AS date, COUNT(*) AS events
                FROM "{sel_table}"
                GROUP BY 1 ORDER BY 1
            """)
            chart_t = alt.Chart(df_time).mark_area(
                color="#4f8ef7", opacity=0.3, line={"color": "#4f8ef7"}
            ).encode(
                x=alt.X("date:T", title="Date"),
                y=alt.Y("events:Q", title="Events"),
                tooltip=["date:T", "events:Q"]
            ).properties(height=300)
            st.altair_chart(chart_t, use_container_width=True)
        except Exception as e:
            st.warning(str(e))

    st.divider()

    # ── Category breakdown ─────────────────────────────────────────────────────
    # Auto-detect group columns from catalog
    group_keys = sorted({k for ev in tinfo.get("events", []) for k in ev.get("groups", {}).keys()})
    if group_keys:
        st.markdown("#### Category Breakdown")
        gcols = st.columns(len(group_keys))
        for i, gkey in enumerate(group_keys):
            with gcols[i]:
                try:
                    df_g = query(f"""
                        SELECT "{gkey}", COUNT(*) AS count
                        FROM "{sel_table}"
                        WHERE "{gkey}" IS NOT NULL
                        GROUP BY 1 ORDER BY 2 DESC
                    """)
                    chart_g = alt.Chart(df_g).mark_arc(innerRadius=40).encode(
                        theta=alt.Theta("count:Q"),
                        color=alt.Color(f"{gkey}:N", legend=alt.Legend(title=gkey.replace("_"," ").title())),
                        tooltip=[f"{gkey}:N", "count:Q"]
                    ).properties(height=200, title=gkey.replace("_", " ").title())
                    st.altair_chart(chart_g, use_container_width=True)
                except Exception as e:
                    st.warning(str(e))

        st.divider()

    # ── What the Catalog Agent produced ───────────────────────────────────────
    st.markdown("#### What the Catalog Agent Documented")
    st.caption("Auto-generated descriptions and properties — with human edits highlighted.")

    events = tinfo.get("events", [])
    edited_events   = [e for e in events if is_event_edited(sel_table, e["raw_name"], e)]
    auto_events     = [e for e in events if not is_event_edited(sel_table, e["raw_name"], e)]

    m1, m2 = st.columns(2)
    m1.metric("🤖 Auto-generated", len(auto_events))
    m2.metric("✏️ Human edited",   len(edited_events))

    st.markdown("")
    for ev in events:
        edited = is_event_edited(sel_table, ev["raw_name"], ev)
        bg     = "#fffbeb" if edited else "#f8fafc"
        border = "#fbbf24" if edited else "#e2e8f0"
        badge  = source_badge(edited)
        props  = ev.get("properties", [])
        props_html = "".join(
            f'<span style="font-family:monospace;font-size:11px;background:#f1f5f9;'
            f'padding:1px 5px;border-radius:4px;margin-right:3px">{p["raw_name"]}</span>'
            for p in props
        ) if props else '<span style="color:#94a3b8;font-size:11px">no properties</span>'

        st.markdown(
            f'<div style="background:{bg};border:1px solid {border};border-radius:8px;'
            f'padding:10px 14px;margin-bottom:8px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
            f'<strong>{ev.get("display_name", ev["raw_name"])}</strong>'
            f'<span class="raw-name">{ev["raw_name"]}</span>{badge}</div>'
            f'<div style="font-size:13px;color:#475569;margin-bottom:6px">{ev.get("description","")}</div>'
            f'<div>{props_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Raw sample rows ────────────────────────────────────────────────────────
    st.markdown("#### Raw Sample Rows")
    evt_filter = st.selectbox("Filter by event", ["(all)"] + sorted(df_dist["event_name"].tolist() if "df_dist" in dir() else []))
    try:
        where = f"WHERE {event_name_col} = '{evt_filter}'" if evt_filter != "(all)" else ""
        df_raw = query(f"SELECT * FROM \"{sel_table}\" {where} LIMIT 20")
        st.dataframe(df_raw, use_container_width=True, height=300)
    except Exception as e:
        st.warning(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS CONTEXT  (merged: custom events + metrics + glossary + rules)
# ─────────────────────────────────────────────────────────────────────────────

elif page == "🏢 Business Context":
    st.markdown("## Business Context")
    st.caption("Everything the analytics agent needs to understand your business — auto-detected by LLM, human-editable here.")

    # ── Industry / Company ────────────────────────────────────────────────────
    c1, c2 = st.columns(2)
    with c1:
        st.session_state.biz_industry = st.text_input(
            "Industry", value=st.session_state.biz_industry,
            help="Inferred from your data. Edit if wrong."
        )
    with c2:
        st.session_state.biz_company = st.text_input(
            "Company name", value=st.session_state.biz_company,
            help="Used as a header in catalog.md."
        )

    st.divider()

    # ── Custom Events ─────────────────────────────────────────────────────────
    st.markdown("### ⚡ Custom Events")
    st.caption("Named user lifecycle segments — each maps to a SQL `WHERE` clause. The analytics agent uses these names without expanding the SQL.")

    ces = st.session_state.custom_events
    if not ces:
        st.info("No custom events yet. Re-run the pipeline to auto-generate, or add one below.")

    for ci, ce in enumerate(ces):
        is_auto = ce.get("_source") != "human"
        badge   = source_badge(not is_auto)
        st.markdown(
            f'<div style="border-left:3px solid #4f8ef7;padding-left:10px;'
            f'display:flex;align-items:center;gap:8px;margin-bottom:4px">'
            f'<strong style="font-family:monospace">{ce.get("name","")}</strong>{badge}</div>',
            unsafe_allow_html=True,
        )
        with st.expander("Edit", expanded=False):
            col1, col2 = st.columns([1, 2])
            with col1:
                ces[ci]["name"]        = st.text_input("Name", value=ce.get("name", ""), key=f"cename_{ci}")
                ces[ci]["description"] = st.text_input("Description", value=ce.get("description", ""), key=f"cedesc_{ci}")
            with col2:
                ces[ci]["sql"] = st.text_area("SQL (WHERE clause)", value=ce.get("sql", ""),
                                              height=80, key=f"cesql_{ci}",
                                              help="e.g. event_name IN ('app_open', 'upi_payment_initiated')")
            if st.button("🗑 Remove", key=f"cedel_{ci}"):
                ces.pop(ci); st.rerun()
        st.markdown("---")

    with st.form("add_custom_event"):
        st.markdown("#### Add custom event")
        col1, col2 = st.columns([1, 2])
        with col1:
            new_ce_name = st.text_input("Name", placeholder="paying_user")
            new_ce_desc = st.text_input("Description", placeholder="User who completed a payment")
        with col2:
            new_ce_sql = st.text_area("SQL (WHERE clause)", height=80, placeholder="event_name = 'upi_payment_success'")
        if st.form_submit_button("➕ Add Custom Event", type="primary"):
            if new_ce_name and new_ce_sql:
                ces.append({"name": new_ce_name, "description": new_ce_desc, "sql": new_ce_sql, "_source": "human"})
                st.success(f"Added '{new_ce_name}'"); st.rerun()
            else:
                st.warning("Name and SQL are required.")

    st.divider()

    # ── Table Metrics ─────────────────────────────────────────────────────────
    st.markdown("### 📐 Table Metrics")
    st.caption("KPI formulas tied to a specific table — auto-suggested by LLM, edit or add your own.")

    table_sel = st.selectbox("Table", list(valid_tables.keys()),
                             format_func=lambda k: valid_tables[k].get("table_display_name", k))
    metrics = st.session_state.catalog[table_sel].setdefault("suggested_metrics", [])

    for mi, m in enumerate(metrics):
        with st.expander(f"📐 {m.get('name', f'Metric {mi+1}')}", expanded=False):
            m["name"]        = st.text_input("Name", value=m.get("name", ""), key=f"mname_{table_sel}_{mi}")
            m["description"] = st.text_area("Description", value=m.get("description", ""), height=60, key=f"mdesc_{table_sel}_{mi}")
            m["sql_hint"]    = st.text_area("SQL hint", value=m.get("sql_hint", ""), height=60, key=f"msql_{table_sel}_{mi}")
            if st.button("🗑 Remove", key=f"mdel_{table_sel}_{mi}"):
                metrics.pop(mi); st.rerun()

    with st.form("add_metric"):
        st.markdown("#### Add metric")
        new_mname = st.text_input("Name", placeholder="KYC Completion Rate")
        new_mdesc = st.text_area("Description", height=60)
        new_msql  = st.text_area("SQL hint", height=60, placeholder="COUNT(kyc_completed) / COUNT(kyc_initiated)")
        if st.form_submit_button("➕ Add Metric", type="primary"):
            if new_mname:
                metrics.append({"name": new_mname, "description": new_mdesc, "sql_hint": new_msql})
                st.success(f"Added '{new_mname}'"); st.rerun()
            else:
                st.warning("Name is required.")

    st.divider()

    # ── Always-Filter Clauses ─────────────────────────────────────────────────
    excl = st.session_state.exclusions
    st.markdown("### 🚫 Always-Filter Clauses")
    st.caption("Applied to every query — filters out test/bot users, internal traffic, etc.")

    filters = excl["always_filter"]
    for fi, f in enumerate(filters):
        c1, c2 = st.columns([6, 1])
        with c1:
            filters[fi] = st.text_input(f"f{fi}", value=f, key=f"filter_{fi}", label_visibility="collapsed")
        with c2:
            if st.button("✕", key=f"fdel_{fi}"):
                filters.pop(fi); st.rerun()
    with st.form("add_filter"):
        new_f = st.text_input("New clause", placeholder="platform != 'web'")
        if st.form_submit_button("➕ Add Filter"):
            if new_f:
                filters.append(new_f); st.rerun()

    st.divider()

    # ── Business Glossary ─────────────────────────────────────────────────────
    st.markdown("### 📖 Business Definitions")
    st.caption("Business concepts with plain English meaning + SQL formula.")

    glossary = excl["glossary"]
    for gi, item in enumerate(glossary):
        with st.expander(f"**{item['term']}**", expanded=False):
            item["term"]        = st.text_input("Term", value=item.get("term", ""), key=f"gterm_{gi}")
            item["description"] = st.text_input(
                "Description (plain English)", value=item.get("description", item.get("definition", "")),
                key=f"gdesc_{gi}"
            )
            item["sql"] = st.text_area(
                "SQL formula", value=item.get("sql", ""),
                height=60, key=f"gsql_{gi}",
                placeholder="COUNT(DISTINCT user_id) WHERE ..."
            )
            if st.button("🗑 Remove", key=f"gdel_{gi}"):
                glossary.pop(gi); st.rerun()
    with st.form("add_glossary"):
        st.markdown("#### Add definition")
        g_term = st.text_input("Term", placeholder="Activated User")
        g_desc = st.text_input("Description", placeholder="User who completed onboarding and made first action")
        g_sql  = st.text_area("SQL formula", height=60, placeholder="COUNT(DISTINCT user_id) WHERE event_name = '...'")
        if st.form_submit_button("➕ Add Term"):
            if g_term:
                glossary.append({"term": g_term, "description": g_desc, "sql": g_sql}); st.rerun()

    st.divider()

    # ── SQL Conventions ───────────────────────────────────────────────────────
    st.markdown("### ⚙️ SQL Conventions")
    st.caption("Rules for SQL generation — dialect, time windows, formatting.")

    conventions = excl["conventions"]
    for ci, conv in enumerate(conventions):
        c1, c2 = st.columns([6, 1])
        with c1:
            conventions[ci] = st.text_input(f"c{ci}", value=conv, key=f"conv_{ci}", label_visibility="collapsed")
        with c2:
            if st.button("✕", key=f"cdel_{ci}"):
                conventions.pop(ci); st.rerun()
    with st.form("add_convention"):
        new_c = st.text_input("New convention", placeholder="Always cast timestamp to DATE for day-level grouping")
        if st.form_submit_button("➕ Add Convention"):
            if new_c:
                conventions.append(new_c); st.rerun()

    st.divider()
    if st.button("💾 Save Business Context", type="primary", use_container_width=True):
        save_and_regenerate(st.session_state.catalog, catalog_path)
        st.session_state.original_catalog = json.loads(json.dumps(st.session_state.catalog))
        st.success("Saved! catalog.json + catalog.md updated.")
