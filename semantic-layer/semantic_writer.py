"""
semantic_writer.py — Step 3
Writes catalog.md covering all 6 HLD layers in one file.
"""

import json
from pathlib import Path
from datetime import datetime

DEFAULT_EXCLUSIONS = {
    "always_filter": [],
    "glossary": [],
    "conventions": [
        "Default time window: last 7 days unless user specifies otherwise",
        "Always use DATE(event_time) when grouping by day",
        "DuckDB syntax: use INTERVAL '7 days' not DATEADD",
    ]
}


def generate_catalog_md(catalog, custom_events=None, exclusions=None, company_name=""):
    # Pull from LLM-generated business context if not explicitly passed
    biz = catalog.get("__business_context__", {})
    custom_events = custom_events if custom_events is not None else biz.get("custom_events", [])
    exclusions    = exclusions    if exclusions    is not None else biz.get("exclusions", DEFAULT_EXCLUSIONS)
    company_name  = company_name or biz.get("company", "") or biz.get("industry", "")

    lines = [
        "# Analytics Catalog",
        f"_{company_name + ' · ' if company_name else ''}Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        "**How to use:**",
        "- Refer to events and columns by their **display names** in responses",
        "- Use `raw_name` values in SQL queries",
        "- `[PII]` columns must never appear in results",
        "- `[skip]` columns are technical — never filter or group by them",
        "- ★ = high-priority dimension — use these first for RCA and segmentation",
        "",
    ]

    for table_name, entry in catalog.items():
        if table_name.startswith("__") or "error" in entry:
            continue
        lines += [
            "---",
            f"## `{table_name}` — {entry.get('table_display_name', table_name)}",
            f"_{entry.get('table_type','unknown')} · {entry.get('table_description','')}_",
            "",
        ]

        events = [e for e in entry.get("events", []) if not e.get("hide")]
        if events:
            lines.append(f"### Events ({len(events)})")
            for ev in events:
                tags = ev.get("analysis_tags", [])
                tag_str = f" `[{', '.join(tags)}]`" if tags else ""
                lines.append(f"- **{ev['display_name']}** → `event_name = '{ev['raw_name']}'`{tag_str}")
                lines.append(f"  _{ev['description']}_")
                props = [p for p in ev.get("properties", []) if not p.get("hide")]
                for prop in props:
                    vm = prop.get("value_meanings", {})
                    val_str = ""
                    if vm:
                        val_str = " — " + ", ".join(f"`{k}`={v}" for k, v in list(vm.items())[:5])
                    lines.append(f"  - `{prop['raw_name']}` ({prop['display_name']}): {prop['description']}{val_str}")
            lines.append("")

        high_cols = [c for c in entry.get("columns",[]) if c.get("analysis_priority")=="high" and not c.get("is_pii")]
        if high_cols:
            lines.append("### Key dimensions ★")
            for col in high_cols:
                vm = col.get("value_meanings", {})
                if vm:
                    val_str = " — " + ", ".join(f"`{k}` = {v}" for k,v in list(vm.items())[:6])
                else:
                    sv = col.get("sample_values",[])[:8]
                    val_str = f" — values: {', '.join(f'`{v}`' for v in sv)}" if sv else ""
                lines.append(f"- **{col['display_name']}** (`{col['raw_name']}`): {col['description']}{val_str}")
            lines.append("")

        medium_cols = [c for c in entry.get("columns",[]) if c.get("analysis_priority")=="medium" and not c.get("is_pii")]
        if medium_cols:
            lines.append("### Other useful columns")
            for col in medium_cols:
                lines.append(f"- **{col['display_name']}** (`{col['raw_name']}`): {col['description']}")
            lines.append("")

        pii_cols = [c for c in entry.get("columns",[]) if c.get("is_pii")]
        if pii_cols:
            lines.append("### PII — never expose in results")
            for col in pii_cols:
                lines.append(f"- `{col['raw_name']}` ({col['display_name']}) [PII]")
            lines.append("")

        metrics = entry.get("suggested_metrics", [])
        if metrics:
            lines.append("### Suggested metrics")
            for m in metrics:
                hint = f"\n  `{m['sql_hint']}`" if m.get("sql_hint") else ""
                lines.append(f"- **{m['name']}**: {m['description']}{hint}")
            lines.append("")

    lines += ["---", "## Custom Events", "_Use these names directly — never expand the SQL manually._", ""]
    for ce in custom_events:
        lines += [f"### {ce['name']}", f"_{ce['description']}_", "```sql", f"WHERE {ce['sql']}", "```", ""]

    lines += ["---", "## Exclusions — Apply to every single query", "", "```sql", "WHERE"]
    for f in exclusions.get("always_filter", []):
        lines.append(f"  AND {f}")
    lines += ["```", ""]

    lines += ["## Business Definitions", ""]
    lines += ["_How the business defines composite concepts — use these consistently in all queries._", ""]
    for item in exclusions.get("glossary", []):
        desc = item.get("description", item.get("definition", ""))
        sql  = item.get("sql", "")
        lines.append(f"- **{item['term']}**: {desc}")
        if sql:
            lines.append(f"  `{sql}`")
    lines.append("")

    lines += ["## Conventions", ""]
    for c in exclusions.get("conventions", []):
        lines.append(f"- {c}")
    lines.append("")

    return "\n".join(lines)


def write_per_table_files(catalog, tables_dir):
    tables_dir = Path(tables_dir)
    tables_dir.mkdir(exist_ok=True)
    for table_name, entry in catalog.items():
        if table_name.startswith("__") or "error" in entry:
            continue
        d = tables_dir / table_name
        d.mkdir(exist_ok=True)
        desc = [f"# {entry.get('table_display_name', table_name)}", "", entry.get("table_description",""), ""]
        for ev in entry.get("events",[]):
            desc += [f"### {ev['display_name']}", f"`{ev['raw_name']}`", ev["description"], ""]
        (d / "description.md").write_text("\n".join(desc))
        col_lines = [f"# Columns — {table_name}", ""]
        for col in entry.get("columns",[]):
            p = col.get("analysis_priority","medium")
            col_lines += [f"## {col['display_name']}{'★' if p=='high' else ''}{'[PII]' if col.get('is_pii') else ''}{'[skip]' if p=='skip' else ''}", f"- `{col['raw_name']}` | {p} | {col.get('description','')}",""]
        (d / "columns.md").write_text("\n".join(col_lines))


def generate_semantic_files(catalog, output_dir=".", custom_events=None, exclusions=None, company_name=""):
    out = Path(output_dir)
    out.mkdir(exist_ok=True)
    md = generate_catalog_md(catalog, custom_events, exclusions, company_name)
    (out / "catalog.md").write_text(md)
    print(f"  catalog.md  ({len(md):,} chars, ~{len(md)//4} tokens)")
    (out / "catalog.json").write_text(json.dumps(catalog, indent=2))
    print(f"  catalog.json")
    write_per_table_files(catalog, out / "tables")
    n_tables = sum(1 for k in catalog if not k.startswith("__"))
    print(f"  tables/  ({n_tables} folders)")
    return md


if __name__ == "__main__":
    import sys
    catalog_path = sys.argv[1] if len(sys.argv) > 1 else "catalog.json"
    if not Path(catalog_path).exists():
        print(f"Error: {catalog_path} not found.")
        raise SystemExit(1)
    catalog = json.loads(Path(catalog_path).read_text())
    print("\nWriting semantic files...")
    md = generate_semantic_files(catalog)
    print("\n─── catalog.md preview ──────────────────────────")
    print(md[:3000])
    print("─────────────────────────────────────────────────")
