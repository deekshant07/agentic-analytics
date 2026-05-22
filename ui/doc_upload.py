"""
doc_upload.py — Streamlit sidebar component for uploading business documents
into the analytics agent's semantic layer.

Upload flow:
  1. User uploads a file (.txt / .md / .pdf)
  2. On "Ingest": extract text — NOT saved yet
  3. Similarity check against existing entries (fuzzy title match, no LLM)
  4a. No match → save immediately, reload catalog
  4b. Similar entry found → show conflict card (nothing saved yet):
        "Keep as separate"  → save new entry as-is, reload
        "Preview changes"   → run merge LLM (dry_run), show changelog preview
          ├─ "Confirm merge" → apply merge, discard new draft, reload
          └─ "Cancel"        → save new entry as separate, reload

Nothing is written to catalog.json until the user has made an explicit choice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import streamlit as st

from core.semantic.doc_ingestor import (
    BusinessEvent,
    delete_business_event,
    find_similar_event,
    ingest_document,
    list_business_events,
    merge_update_event,
    save_business_event,
)


def _read_uploaded_bytes(filename: str, data: bytes) -> str:
    """Extract plain text from an uploaded file. Supports .txt, .md, and .pdf."""
    if Path(filename).suffix.lower() == ".pdf":
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join(p.extract_text() or "" for p in reader.pages).strip()
            if not text:
                raise ValueError("PDF appears to be image-based (no extractable text).")
            return text
        except ImportError:
            raise ImportError("pypdf is required for PDF files. Run: uv add pypdf")
    return data.decode("utf-8", errors="replace").strip()

_TYPE_BADGE: dict[str, str] = {
    "feature_launch":  "🚀",
    "campaign":        "📣",
    "strategy":        "🎯",
    "incident":        "⚠️",
    "data_dictionary": "📖",
    "general":         "📄",
}


# ── Public entry point ────────────────────────────────────────────────────────

def render_doc_upload(catalog_path: str, api_key: Optional[str] = None) -> None:
    """Render the document upload section in the Streamlit sidebar."""
    with st.expander("📄 Business Documents", expanded=False):

        uploaded = st.file_uploader(
            "Upload PRD, campaign brief, or strategy doc",
            type=["txt", "md", "pdf"],
            key="doc_uploader",
            help="Extracted facts feed the hypothesis engine and narrative generator.",
        )

        if uploaded is not None:
            if st.button("Ingest document", key="btn_ingest", use_container_width=True):
                _start_ingest(uploaded, catalog_path, api_key)

        # Step 2: conflict card (shown when a similar entry exists, nothing saved yet)
        _render_conflict_card(catalog_path, api_key)

        # Step 3: merge preview card (shown after "Preview changes", before confirm)
        _render_merge_preview_card(catalog_path, api_key)

        st.markdown('<div style="height:0.4rem"></div>', unsafe_allow_html=True)
        _render_event_list(catalog_path)


# ── Step 1: extraction (no save) ──────────────────────────────────────────────

def _start_ingest(uploaded, catalog_path: str, api_key: Optional[str]) -> None:
    """
    Extract document WITHOUT saving. Then check for conflicts.
    Nothing is written to catalog.json at this stage.
    """
    try:
        raw_bytes = uploaded.read()
        text = _read_uploaded_bytes(uploaded.name, raw_bytes)
    except Exception as exc:
        st.error(f"Could not read file: {exc}")
        return

    with st.spinner(f"Extracting from {uploaded.name}…"):
        try:
            new_evt = ingest_document(
                text=text,
                filename=uploaded.name,
                catalog_path=catalog_path,
                api_key=api_key,
                save=False,          # ← don't persist yet
            )
        except Exception as exc:
            st.error(f"Extraction failed: {exc}")
            return

    existing = list_business_events(catalog_path)
    match = find_similar_event(new_evt.title, existing)

    if match is None:
        # No conflict — safe to save immediately
        save_business_event(new_evt, catalog_path)
        _reload_catalog()
        st.success(f"Ingested: **{new_evt.title}**")
        if new_evt.affected_metrics:
            st.caption(f"Affects: {', '.join(new_evt.affected_metrics)}")
        st.rerun()
    else:
        similar_evt, score = match
        # Hold the extracted event in session state until user decides
        from dataclasses import asdict
        st.session_state["_doc_pending"]  = asdict(new_evt)
        st.session_state["_doc_conflict"] = {
            "similar_id":      similar_evt.id,
            "similar_title":   similar_evt.title,
            "similar_date":    similar_evt.date_from or "",
            "similar_version": similar_evt.version,
            "score":           round(score, 2),
        }
        st.session_state.pop("_doc_merge_preview", None)


# ── Step 2: conflict card ─────────────────────────────────────────────────────

def _render_conflict_card(catalog_path: str, api_key: Optional[str]) -> None:
    """
    Shown when extraction found a similar existing entry.
    User picks: keep separate (saves draft as-is) or preview changes (runs merge LLM).
    Nothing is written until the user chooses.
    """
    conflict = st.session_state.get("_doc_conflict")
    pending  = st.session_state.get("_doc_pending")
    if not conflict or not pending or st.session_state.get("_doc_merge_preview"):
        return   # hide once preview is ready

    similar_label = conflict["similar_title"]
    if conflict["similar_date"]:
        similar_label += f" ({conflict['similar_date']})"
    if conflict["similar_version"] > 1:
        similar_label += f" · v{conflict['similar_version']}"

    st.markdown(
        f'<div style="background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.3);'
        f'border-radius:6px;padding:0.6rem 0.7rem;margin:0.4rem 0">'
        f'<div style="font-size:0.72rem;font-weight:600;color:#fbbf24;margin-bottom:0.25rem">'
        f'⚠️ Similar document found</div>'
        f'<div style="font-size:0.7rem;color:rgba(226,232,240,0.8);margin-bottom:0.1rem">'
        f'<b>{pending["title"]}</b> may be a revision of:</div>'
        f'<div style="font-size:0.68rem;color:rgba(148,163,184,0.7);margin-bottom:0.4rem">'
        f'{similar_label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    col_preview, col_separate = st.columns(2)

    with col_preview:
        if st.button("Preview changes", key="btn_preview", use_container_width=True):
            _compute_merge_preview(conflict, pending, catalog_path, api_key)

    with col_separate:
        if st.button("Keep separate", key="btn_keep_separate", use_container_width=True):
            _save_pending_as_new(pending, catalog_path)


# ── Step 3: merge preview card ────────────────────────────────────────────────

def _render_merge_preview_card(catalog_path: str, api_key: Optional[str]) -> None:
    """
    Shown after "Preview changes". Displays the changelog before anything is saved.
    User must explicitly confirm to apply the merge.
    """
    preview = st.session_state.get("_doc_merge_preview")
    pending = st.session_state.get("_doc_pending")
    if not preview or not pending:
        return

    changelog = preview.get("changelog") or []
    merged    = preview.get("merged") or {}

    st.markdown(
        f'<div style="background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.3);'
        f'border-radius:6px;padding:0.6rem 0.7rem;margin:0.4rem 0">'
        f'<div style="font-size:0.72rem;font-weight:600;color:#818cf8;margin-bottom:0.3rem">'
        f'Merge preview — {merged.get("title", "")}</div>',
        unsafe_allow_html=True,
    )

    if changelog:
        for change in changelog:
            st.markdown(
                f'<div style="font-size:0.68rem;color:rgba(226,232,240,0.75);'
                f'padding:0.1rem 0">• {change}</div>',
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            '<div style="font-size:0.68rem;color:rgba(148,163,184,0.6)">No significant changes detected.</div>',
            unsafe_allow_html=True,
        )

    st.markdown('</div>', unsafe_allow_html=True)

    col_confirm, col_cancel = st.columns(2)

    with col_confirm:
        if st.button("Confirm merge", key="btn_confirm_merge", use_container_width=True):
            _apply_confirmed_merge(preview, catalog_path, api_key)

    with col_cancel:
        if st.button("Keep separate", key="btn_cancel_merge", use_container_width=True):
            _save_pending_as_new(pending, catalog_path)


# ── Actions ───────────────────────────────────────────────────────────────────

def _compute_merge_preview(
    conflict: dict, pending: dict, catalog_path: str, api_key: Optional[str]
) -> None:
    """Run merge LLM in dry_run mode and store result for the preview card."""
    from core.semantic.doc_ingestor import BusinessEvent as BE
    existing_events = list_business_events(catalog_path)
    evt_map = {e.id: e for e in existing_events}
    existing_evt = evt_map.get(conflict["similar_id"])
    if not existing_evt:
        st.error("Original entry not found — it may have been deleted.")
        _clear_state()
        return

    new_evt = BE(**pending)

    with st.spinner("Computing what would change…"):
        try:
            merged_evt, changelog = merge_update_event(
                new_event=new_evt,
                existing_event=existing_evt,
                catalog_path=catalog_path,
                api_key=api_key,
                dry_run=True,        # ← preview only, nothing saved
            )
        except Exception as exc:
            st.error(f"Preview failed: {exc}")
            return

    from dataclasses import asdict
    st.session_state["_doc_merge_preview"] = {
        "merged":           asdict(merged_evt),
        "changelog":        changelog,
        "existing_id":      existing_evt.id,
    }
    st.rerun()


def _apply_confirmed_merge(preview: dict, catalog_path: str, api_key: Optional[str]) -> None:
    """User confirmed — apply the merge that was previewed."""
    from core.semantic.doc_ingestor import BusinessEvent as BE
    pending  = st.session_state.get("_doc_pending")
    existing_events = list_business_events(catalog_path)
    evt_map  = {e.id: e for e in existing_events}
    existing_evt = evt_map.get(preview["existing_id"])
    if not existing_evt or not pending:
        st.error("Entries not found — state may be stale. Please re-upload.")
        _clear_state()
        return

    new_evt = BE(**pending)

    with st.spinner("Applying merge…"):
        try:
            updated, changelog = merge_update_event(
                new_event=new_evt,
                existing_event=existing_evt,
                catalog_path=catalog_path,
                api_key=api_key,
                dry_run=False,       # ← actually save this time
            )
        except Exception as exc:
            st.error(f"Merge failed: {exc}")
            _clear_state()
            return

    _clear_state()
    _reload_catalog()
    st.success(f"Updated: **{updated.title}** (v{updated.version})")
    if changelog:
        st.markdown("**Changes applied:**\n" + "\n".join(f"- {c}" for c in changelog))
    st.rerun()


def _save_pending_as_new(pending: dict, catalog_path: str) -> None:
    """Save the extracted draft as a separate new entry."""
    from core.semantic.doc_ingestor import BusinessEvent as BE
    evt = BE(**pending)
    save_business_event(evt, catalog_path)
    _clear_state()
    _reload_catalog()
    st.rerun()


def _clear_state() -> None:
    for key in ("_doc_pending", "_doc_conflict", "_doc_merge_preview"):
        st.session_state.pop(key, None)


# ── Event list ────────────────────────────────────────────────────────────────

def _render_event_list(catalog_path: str) -> None:
    events = list_business_events(catalog_path)
    if not events:
        st.markdown(
            '<div style="font-size:0.72rem;color:rgba(148,163,184,0.5)">'
            'No documents ingested yet.</div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<div style="font-size:0.72rem;color:rgba(148,163,184,0.6);margin-bottom:0.3rem">'
        f'{len(events)} document(s) in semantic layer</div>',
        unsafe_allow_html=True,
    )

    for evt in events:
        _render_event_row(evt, catalog_path)


def _render_event_row(evt: BusinessEvent, catalog_path: str) -> None:
    icon = _TYPE_BADGE.get(evt.type, "📄")
    date_str = evt.date_from or ""
    if evt.date_to:
        date_str += f" – {evt.date_to}"

    version_badge = f" · v{evt.version}" if evt.version > 1 else ""

    col_text, col_del = st.columns([5, 1])
    with col_text:
        st.markdown(
            f'<div style="font-size:0.75rem;font-weight:600;'
            f'color:rgba(226,232,240,0.9);line-height:1.3">'
            f'{icon} {evt.title}{version_badge}</div>'
            f'<div style="font-size:0.67rem;color:rgba(148,163,184,0.6)">'
            f'{evt.type}{(" · " + date_str) if date_str else ""}</div>',
            unsafe_allow_html=True,
        )
        if evt.metric_impacts:
            impact_str = " · ".join(
                f"{m.get('metric','')} {m.get('direction','')}"
                + (f" {m.get('magnitude','')}" if m.get("magnitude") else "")
                for m in evt.metric_impacts[:3]
            )
            st.markdown(
                f'<div style="font-size:0.65rem;color:rgba(148,163,184,0.5)">'
                f'{impact_str}</div>',
                unsafe_allow_html=True,
            )
        # Show latest changelog entry if this is a revised document
        if evt.changelog:
            last_change = evt.changelog[-1]
            st.markdown(
                f'<div style="font-size:0.63rem;color:rgba(251,191,36,0.6);'
                f'margin-top:0.1rem">↻ {last_change[:70]}{"…" if len(last_change) > 70 else ""}</div>',
                unsafe_allow_html=True,
            )

    with col_del:
        if st.button("✕", key=f"del_doc_{evt.id}", help="Remove from semantic layer"):
            if delete_business_event(evt.id, catalog_path):
                _reload_catalog()
                st.rerun()

    st.markdown(
        '<div style="border-bottom:1px solid rgba(255,255,255,0.04);margin:0.3rem 0"></div>',
        unsafe_allow_html=True,
    )


# ── Catalog reload ────────────────────────────────────────────────────────────

def _reload_catalog() -> None:
    """Clear cached schema so chat.py reloads catalog on next run."""
    st.session_state.pop("schema", None)
