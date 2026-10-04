#!/usr/bin/env python3
"""Cadence compliance report generator — deterministic, auditor-grade.

Scans the project for Cadence artifacts and renders a unified report
covering one or more standards. Each artifact appears once with all
its tags listed (cross-standard deduplication), so an auditor can see
every artifact's full coverage in one place.

Supported standards (out of the box):
    nist-ssdf-1.1     (mature)
    iso-25010-2023    (mature)
    soc2-2017         (stub — see YAML header)
    iso-27001-2022    (stub — see YAML header)

Status determination per item:
    implemented       artifact(s) present AND last verify run passed
    partial           artifact(s) present, no verify evidence
    gap               no artifact found
    planned           YAML explicitly marks status: planned (stubs)
    out-of-scope      YAML explicitly marks status: out-of-scope
    n/a               cadence.yaml's compliance.exclude listed this id

Output formats:
    md     readable markdown for engineers / GitHub
    json   structured form for GRC tool ingestion
    html   single-page printable / PDF-able document for auditors

Determinism:
    Same project state → byte-identical output (modulo the explicit
    `generated_at` timestamp in the header). All lists are sorted by
    id; JSON uses sort_keys=True. The `--reproducible` flag fixes the
    timestamp to a placeholder so test runs can diff exactly.

Exit codes:
    0   report generated
    1   one or more standards had unrecoverable gaps (use --strict)
    2   bad arguments, missing standards directory, IO error
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)


# Standard discovery: filename → display order. Order matters because
# the unified report renders sections in this order; auditors expect
# SSDF before ISO 25010 before SOC2 before ISO 27001.
_STANDARD_FILES: tuple[tuple[str, str], ...] = (
    ("nist-ssdf-1.1.yaml", "nist-ssdf"),
    ("iso-25010-2023.yaml", "iso-25010"),
    ("soc2-2017.yaml", "aicpa-soc2-tsc"),
    ("iso-27001-2022.yaml", "iso-27001"),
)

_STATUS_ORDER = (
    "implemented",
    "partial",
    "gap",
    "planned",
    "out-of-scope",
    "n/a",
)


@dataclass(frozen=True)
class Item:
    """A single mappable point: an SSDF practice, an ISO 25010
    characteristic, a SOC2 CC family, or an ISO 27001 Annex A theme."""

    standard_id: str
    item_id: str
    name: str
    artifacts_declared: tuple[str, ...]
    declared_status: str | None
    notes: str | None
    artifact_count_in_yaml: int


@dataclass(frozen=True)
class ResolvedItem:
    item: Item
    status: str
    artifacts_resolved: tuple[str, ...]
    artifacts_missing: tuple[str, ...]


@dataclass
class Report:
    project_name: str
    project_root: Path
    cadence_version: str
    last_verify_sha: str | None
    generated_at: str
    standards: list[dict[str, Any]] = field(default_factory=list)
    items: list[ResolvedItem] = field(default_factory=list)
    summary: dict[str, dict[str, int]] = field(default_factory=dict)
    reverse_index: dict[str, list[tuple[str, str, str]]] = field(
        default_factory=dict
    )


# --- Loading ---------------------------------------------------------------


def _load_standard_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a YAML mapping")
    return data


def _items_from_yaml(
    data: dict[str, Any], standard_id_expected: str
) -> list[Item]:
    """Extract the flat list of mappable items from a standard YAML.

    Each standard YAML uses its own key for the items list — `practices`
    for SSDF, `characteristics` for ISO 25010, `common_criteria` for
    SOC2, `annex_a_themes` for ISO 27001. We try each.
    """
    declared = (data.get("standard") or {}).get("id", "")
    if declared != standard_id_expected:
        raise ValueError(
            f"standard.id mismatch: yaml says {declared!r}, "
            f"expected {standard_id_expected!r}"
        )

    for key in (
        "practices",
        "characteristics",
        "common_criteria",
        "annex_a_themes",
    ):
        raw = data.get(key)
        if isinstance(raw, list) and raw:
            return [_item_from_entry(entry, declared) for entry in raw]
    return []


def _item_from_entry(entry: dict[str, Any], standard_id: str) -> Item:
    artifacts: list[str] = []
    for art in entry.get("cadence_artifacts") or []:
        if isinstance(art, dict):
            if "path" in art:
                artifacts.append(str(art["path"]))
            elif "glob" in art:
                artifacts.append(str(art["glob"]))
        elif isinstance(art, str):
            artifacts.append(art)
    return Item(
        standard_id=standard_id,
        item_id=str(entry.get("id", "")),
        name=str(entry.get("name", entry.get("description", ""))),
        artifacts_declared=tuple(artifacts),
        declared_status=(
            str(entry["status"]) if "status" in entry else None
        ),
        notes=(str(entry["notes"]).strip() if "notes" in entry else None),
        artifact_count_in_yaml=len(artifacts),
    )


# --- Resolution ------------------------------------------------------------


def _resolve_artifact(project_root: Path, declared: str) -> list[str]:
    """Return all paths (relative to project_root) matching ``declared``.

    ``declared`` may carry an ``#anchor`` suffix (e.g.
    ``docs/PATTERNS.md#security``). The anchor is for human readers; we
    check existence of the base path only.
    """
    base = declared.split("#", 1)[0]
    if not base:
        return []
    matches: list[str] = []
    for hit in sorted(project_root.glob(base)):
        if hit.exists():
            matches.append(hit.relative_to(project_root).as_posix())
    return matches


def _resolve(
    items: list[Item],
    project_root: Path,
    last_verify_ok: bool,
    exclude_ids: set[str],
) -> list[ResolvedItem]:
    resolved: list[ResolvedItem] = []
    for item in items:
        if item.item_id in exclude_ids:
            resolved.append(
                ResolvedItem(
                    item=item,
                    status="n/a",
                    artifacts_resolved=(),
                    artifacts_missing=item.artifacts_declared,
                )
            )
            continue

        if item.declared_status in ("planned", "out-of-scope"):
            resolved.append(
                ResolvedItem(
                    item=item,
                    status=item.declared_status,
                    artifacts_resolved=(),
                    artifacts_missing=(),
                )
            )
            continue

        if not item.artifacts_declared:
            resolved.append(
                ResolvedItem(
                    item=item,
                    status="gap",
                    artifacts_resolved=(),
                    artifacts_missing=(),
                )
            )
            continue

        found: list[str] = []
        missing: list[str] = []
        for declared in item.artifacts_declared:
            hits = _resolve_artifact(project_root, declared)
            if hits:
                found.extend(hits)
            else:
                missing.append(declared)

        if found and not missing:
            status = "implemented" if last_verify_ok else "partial"
        elif found:
            status = "partial"
        else:
            status = "gap"

        resolved.append(
            ResolvedItem(
                item=item,
                status=status,
                artifacts_resolved=tuple(sorted(set(found))),
                artifacts_missing=tuple(missing),
            )
        )
    return resolved


# --- Reverse index ---------------------------------------------------------


def _build_reverse_index(
    resolved: list[ResolvedItem],
) -> dict[str, list[tuple[str, str, str]]]:
    """Map each artifact path → sorted list of (standard_id, item_id, name)."""
    index: dict[str, set[tuple[str, str, str]]] = {}
    for item in resolved:
        for path in item.artifacts_resolved:
            index.setdefault(path, set()).add(
                (item.item.standard_id, item.item.item_id, item.item.name)
            )
    return {
        path: sorted(entries) for path, entries in sorted(index.items())
    }


# --- Summary ---------------------------------------------------------------


def _build_summary(
    resolved: list[ResolvedItem],
) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for item in resolved:
        bucket = summary.setdefault(
            item.item.standard_id, {s: 0 for s in _STATUS_ORDER}
        )
        bucket[item.status] = bucket.get(item.status, 0) + 1
    return summary


# --- Verify-evidence -------------------------------------------------------


def _last_verify_ok(project_root: Path) -> bool:
    """Return True if there's evidence the last verify run passed.

    We look for a single file the verify script writes after a clean
    pass: ``.cadence/.last_verify_ok``. If it doesn't exist, we cannot
    confirm — assume False so we don't over-claim Implemented.
    """
    return (project_root / ".cadence" / ".last_verify_ok").is_file()


def _last_verify_sha(project_root: Path) -> str | None:
    marker = project_root / ".cadence" / ".last_verify_sha"
    if marker.is_file():
        return marker.read_text(encoding="utf-8").strip() or None
    return None


def _cadence_version(project_root: Path) -> str:
    candidates = [
        project_root / ".claude-plugin" / "plugin.json",
        project_root / "plugins" / "cadence" / ".claude-plugin" / "plugin.json",
    ]
    for path in candidates:
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    return str(json.load(fh).get("version", "unknown"))
            except (json.JSONDecodeError, OSError):
                continue
    return "unknown"


def _exclude_ids_from_cadence_yaml(project_root: Path) -> set[str]:
    cfg_path = project_root / ".cadence" / "cadence.yaml"
    if not cfg_path.is_file():
        return set()
    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except yaml.YAMLError:
        return set()
    if not isinstance(cfg, dict):
        return set()
    raw = (cfg.get("compliance") or {}).get("exclude") or []
    return {str(x) for x in raw if isinstance(x, (str, int))}


# --- Rendering: markdown ---------------------------------------------------


def _render_md(report: Report) -> str:
    lines: list[str] = []
    lines.append(f"# Cadence Compliance Report — {report.project_name}")
    lines.append("")
    lines.append(f"- Generated: {report.generated_at}")
    lines.append(f"- Cadence version: {report.cadence_version}")
    lines.append(
        f"- Last verify SHA: {report.last_verify_sha or 'unknown'}"
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(
        "| Standard | " + " | ".join(s.title() for s in _STATUS_ORDER) + " |"
    )
    lines.append("|" + "---|" * (len(_STATUS_ORDER) + 1))
    for std in report.standards:
        bucket = report.summary.get(std["id"], {})
        cells = [str(bucket.get(s, 0)) for s in _STATUS_ORDER]
        lines.append(f"| {std['id']} | " + " | ".join(cells) + " |")
    lines.append("")

    for std in report.standards:
        std_id = std["id"]
        lines.append(f"## {std_id} — {std['version']}")
        lines.append("")
        lines.append(f"Authority: {std['authority']}")
        lines.append(f"URL: {std['url']}")
        if std.get("reference_implementation_status"):
            lines.append(
                f"Status: {std['reference_implementation_status']}"
            )
        lines.append("")

        items = [r for r in report.items if r.item.standard_id == std_id]
        for r in items:
            lines.append(f"### {r.item.item_id} — {r.item.name}")
            lines.append(f"Status: **{r.status}**")
            if r.artifacts_resolved:
                lines.append("")
                lines.append("Artifacts:")
                for art in r.artifacts_resolved:
                    lines.append(f"- `{art}`")
            if r.artifacts_missing:
                lines.append("")
                lines.append("Missing:")
                for miss in r.artifacts_missing:
                    lines.append(f"- `{miss}` (declared, not found)")
            if r.item.notes:
                lines.append("")
                lines.append(f"> {r.item.notes}")
            lines.append("")

    lines.append("## Reverse index — artifact to controls supported")
    lines.append("")
    if not report.reverse_index:
        lines.append("_No resolved artifacts to index._")
    else:
        for path, entries in report.reverse_index.items():
            lines.append(f"### `{path}`")
            for std_id, item_id, name in entries:
                lines.append(f"- {std_id}/{item_id} — {name}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "> This report is **opinionated, not authoritative**. Cadence "
        "maps artifacts that *support* compliance; it does not constitute "
        "formal certification. Stubs (status: planned) are roadmap "
        "scaffolding, not compliance claims. For SOC 2 attestation or "
        "ISO 27001 certification, work with a licensed auditor."
    )
    lines.append("")
    return "\n".join(lines)


# --- Rendering: JSON -------------------------------------------------------


def _render_json(report: Report) -> str:
    payload: dict[str, Any] = {
        "project": report.project_name,
        "cadence_version": report.cadence_version,
        "generated_at": report.generated_at,
        "last_verify_sha": report.last_verify_sha,
        "standards": report.standards,
        "summary": report.summary,
        "items": [
            {
                "standard_id": r.item.standard_id,
                "item_id": r.item.item_id,
                "name": r.item.name,
                "status": r.status,
                "artifacts_resolved": list(r.artifacts_resolved),
                "artifacts_missing": list(r.artifacts_missing),
                "notes": r.item.notes,
            }
            for r in report.items
        ],
        "reverse_index": {
            path: [
                {"standard": s, "item_id": i, "name": n}
                for (s, i, n) in entries
            ]
            for path, entries in report.reverse_index.items()
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# --- Rendering: HTML (auditor-grade single page) ---------------------------


_HTML_STYLE = """
:root {
  --c-implemented: #1f7a3a;
  --c-partial: #c98300;
  --c-gap: #b3261e;
  --c-planned: #5e6266;
  --c-out-of-scope: #888;
  --c-na: #888;
  --c-border: #d0d7de;
  --c-text: #1f2328;
  --c-muted: #57606a;
  --c-bg-alt: #f6f8fa;
}
* { box-sizing: border-box; }
body {
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
    Roboto, "Helvetica Neue", Arial, sans-serif;
  color: var(--c-text);
  max-width: 880px;
  margin: 2rem auto;
  padding: 0 1.5rem;
}
h1 { font-size: 1.8rem; margin: 0 0 .25rem; }
h2 { font-size: 1.3rem; margin: 2rem 0 .75rem;
     border-bottom: 1px solid var(--c-border); padding-bottom: .25rem; }
h3 { font-size: 1.05rem; margin: 1.25rem 0 .5rem; }
.meta { color: var(--c-muted); margin-bottom: 1.5rem;
        font-size: .9rem; }
.meta dt { font-weight: 600; display: inline; }
.meta dd { display: inline; margin: 0 1rem 0 .25rem; }
table { border-collapse: collapse; width: 100%;
        margin: 0 0 1rem; font-size: .9rem; }
th, td { border: 1px solid var(--c-border); padding: .4rem .6rem;
         text-align: left; }
th { background: var(--c-bg-alt); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.status { display: inline-block; padding: .1rem .5rem;
          border-radius: 3px; color: white; font-size: .75rem;
          font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
.status.implemented { background: var(--c-implemented); }
.status.partial { background: var(--c-partial); }
.status.gap { background: var(--c-gap); }
.status.planned { background: var(--c-planned); }
.status.out-of-scope { background: var(--c-out-of-scope); }
.status.na { background: var(--c-na); }
.item { padding: .75rem; border: 1px solid var(--c-border);
        border-radius: 4px; margin: .5rem 0; }
.item .head { display: flex; justify-content: space-between;
              align-items: center; gap: 1rem; }
.item .head .id { font-family: ui-monospace, SFMono-Regular, monospace;
                   font-weight: 600; }
.item .head .name { color: var(--c-muted); flex: 1; }
.item ul { margin: .4rem 0 .25rem 1.25rem; padding: 0; }
.item li { margin: .1rem 0; }
.item code, code { font: .85em ui-monospace, SFMono-Regular, monospace;
                   background: var(--c-bg-alt); padding: 1px 4px;
                   border-radius: 3px; }
.item blockquote { margin: .5rem 0; padding: .25rem .75rem;
                   border-left: 3px solid var(--c-border);
                   color: var(--c-muted); font-size: .9rem; }
.disclaimer { margin-top: 3rem; padding: 1rem;
              background: var(--c-bg-alt); border-radius: 4px;
              font-size: .85rem; color: var(--c-muted); }
@media print {
  body { max-width: none; margin: 0; padding: 1cm; font-size: 11pt; }
  .item { break-inside: avoid; }
  h2, h3 { break-after: avoid; }
  a { color: inherit; text-decoration: none; }
}
"""


def _status_class(status: str) -> str:
    return status.replace("/", "").replace("-", "-")


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _render_html(report: Report) -> str:
    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append('<html lang="en"><head>')
    parts.append('<meta charset="utf-8">')
    parts.append(
        f"<title>Cadence Compliance Report — {_esc(report.project_name)}</title>"
    )
    parts.append(f"<style>{_HTML_STYLE}</style>")
    parts.append("</head><body>")

    parts.append(
        f"<h1>Cadence Compliance Report — {_esc(report.project_name)}</h1>"
    )
    parts.append('<dl class="meta">')
    parts.append(
        f"<dt>Generated</dt><dd>{_esc(report.generated_at)}</dd>"
    )
    parts.append(
        f"<dt>Cadence version</dt><dd>{_esc(report.cadence_version)}</dd>"
    )
    parts.append(
        f"<dt>Last verify SHA</dt>"
        f"<dd>{_esc(report.last_verify_sha or 'unknown')}</dd>"
    )
    parts.append("</dl>")

    # Summary table
    parts.append("<h2>Summary</h2>")
    parts.append("<table><thead><tr><th>Standard</th>")
    for s in _STATUS_ORDER:
        parts.append(f"<th>{_esc(s.title())}</th>")
    parts.append("</tr></thead><tbody>")
    for std in report.standards:
        bucket = report.summary.get(std["id"], {})
        parts.append(f"<tr><td><code>{_esc(std['id'])}</code></td>")
        for s in _STATUS_ORDER:
            parts.append(f'<td class="num">{_esc(bucket.get(s, 0))}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")

    # Per-standard
    for std in report.standards:
        std_id = std["id"]
        parts.append(
            f"<h2>{_esc(std_id)} — {_esc(std.get('version', ''))}</h2>"
        )
        parts.append('<dl class="meta">')
        parts.append(
            f"<dt>Authority</dt><dd>{_esc(std.get('authority', ''))}</dd>"
        )
        if std.get("url"):
            parts.append(
                f'<dt>URL</dt><dd><code>{_esc(std["url"])}</code></dd>'
            )
        if std.get("reference_implementation_status"):
            parts.append(
                "<dt>Status</dt>"
                f"<dd>{_esc(std['reference_implementation_status'])}</dd>"
            )
        parts.append("</dl>")

        items = [r for r in report.items if r.item.standard_id == std_id]
        for r in items:
            cls = _status_class(r.status)
            parts.append('<div class="item">')
            parts.append('<div class="head">')
            parts.append(f'<span class="id">{_esc(r.item.item_id)}</span>')
            parts.append(f'<span class="name">{_esc(r.item.name)}</span>')
            parts.append(
                f'<span class="status {cls}">{_esc(r.status)}</span>'
            )
            parts.append("</div>")
            if r.artifacts_resolved:
                parts.append("<div><strong>Artifacts:</strong><ul>")
                for art in r.artifacts_resolved:
                    parts.append(f"<li><code>{_esc(art)}</code></li>")
                parts.append("</ul></div>")
            if r.artifacts_missing:
                parts.append("<div><strong>Missing:</strong><ul>")
                for miss in r.artifacts_missing:
                    parts.append(
                        f"<li><code>{_esc(miss)}</code> "
                        "<em>(declared, not found)</em></li>"
                    )
                parts.append("</ul></div>")
            if r.item.notes:
                parts.append(f"<blockquote>{_esc(r.item.notes)}</blockquote>")
            parts.append("</div>")

    # Reverse index
    parts.append("<h2>Reverse index — artifact to controls supported</h2>")
    if not report.reverse_index:
        parts.append("<p><em>No resolved artifacts to index.</em></p>")
    else:
        for path, entries in report.reverse_index.items():
            parts.append(f"<h3><code>{_esc(path)}</code></h3>")
            parts.append("<ul>")
            for std_id, item_id, name in entries:
                parts.append(
                    f"<li><code>{_esc(std_id)}/{_esc(item_id)}</code> — "
                    f"{_esc(name)}</li>"
                )
            parts.append("</ul>")

    parts.append(
        '<p class="disclaimer">This report is <strong>opinionated, not '
        "authoritative</strong>. Cadence maps artifacts that <em>support</em> "
        "compliance; it does not constitute formal certification. Stubs "
        "(status: planned) are roadmap scaffolding, not compliance claims. "
        "For SOC 2 attestation or ISO 27001 certification, work with a "
        "licensed auditor.</p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


# --- Audit packet ----------------------------------------------------------


def _build_audit_packet(
    project_root: Path,
    report_dir: Path,
    report_paths: dict[str, Path],
) -> Path:
    """Bundle the report + ADR index + FRAMEWORK_CHANGELOG + last verify
    output into a single timestamped folder under report_dir."""
    packet_root = report_dir / "audit-packet"
    packet_root.mkdir(parents=True, exist_ok=True)

    for fmt, src in report_paths.items():
        if src.is_file():
            shutil.copy2(src, packet_root / f"compliance-report.{fmt}")

    # ADRs
    adr_dir = project_root / "docs" / "ADR"
    if adr_dir.is_dir():
        target = packet_root / "ADR"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(adr_dir, target)

    # Framework changelog + retro protocol
    for rel in ("docs/FRAMEWORK_CHANGELOG.md", "docs/RETROSPECTIVE_PROTOCOL.md",
                "docs/DEFINITION_OF_DONE.md", "docs/PATTERNS.md"):
        src = project_root / rel
        if src.is_file():
            shutil.copy2(src, packet_root / Path(rel).name)

    # Last verify output if any
    verify_log = project_root / ".cadence" / "last_verify.log"
    if verify_log.is_file():
        shutil.copy2(verify_log, packet_root / "last_verify.log")

    # Manifest so the auditor sees what's in the packet without ls
    manifest = packet_root / "MANIFEST.txt"
    files = sorted(p.relative_to(packet_root).as_posix()
                   for p in packet_root.rglob("*")
                   if p.is_file() and p.name != "MANIFEST.txt")
    manifest.write_text(
        "Cadence audit packet\n\n"
        "Contents:\n" + "\n".join(f"  {f}" for f in files) + "\n",
        encoding="utf-8",
    )
    return packet_root


# --- Standard discovery ----------------------------------------------------


def _find_standards_dir(project_root: Path) -> Path | None:
    candidates = [
        project_root / "standards",
        project_root / ".cadence" / "standards",
        project_root / "plugins" / "cadence" / "standards",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


# --- Main ------------------------------------------------------------------


def generate(
    project_root: Path,
    standards_dir: Path,
    selected: list[str],
    reproducible: bool,
) -> Report:
    project_name = project_root.name
    last_verify_ok = _last_verify_ok(project_root)
    last_sha = _last_verify_sha(project_root)
    excludes = _exclude_ids_from_cadence_yaml(project_root)
    cadence_version = _cadence_version(project_root)

    standards_meta: list[dict[str, Any]] = []
    all_items: list[Item] = []
    for filename, std_id in _STANDARD_FILES:
        if selected and std_id not in selected:
            continue
        path = standards_dir / filename
        if not path.is_file():
            continue
        data = _load_standard_yaml(path)
        standards_meta.append(data.get("standard") or {"id": std_id})
        all_items.extend(_items_from_yaml(data, std_id))

    resolved = _resolve(all_items, project_root, last_verify_ok, excludes)
    reverse = _build_reverse_index(resolved)
    summary = _build_summary(resolved)

    generated_at = (
        "REPRODUCIBLE-PLACEHOLDER"
        if reproducible
        else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

    return Report(
        project_name=project_name,
        project_root=project_root,
        cadence_version=cadence_version,
        last_verify_sha=last_sha,
        generated_at=generated_at,
        standards=standards_meta,
        items=resolved,
        summary=summary,
        reverse_index=reverse,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cadence unified compliance report generator.",
    )
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(),
        help="Project root (default: cwd).",
    )
    parser.add_argument(
        "--standards-dir", type=Path, default=None,
        help="Directory containing standard YAMLs "
        "(default: <root>/standards or <root>/.cadence/standards or "
        "plugins/cadence/standards).",
    )
    parser.add_argument(
        "--standard", action="append", default=[],
        help="Filter to specific standard id(s); can be passed multiple "
        "times. Pass 'all' (the default) or omit to include every "
        "known standard. Recognised ids: ssdf, nist-ssdf, iso25010, "
        "iso-25010, soc2, aicpa-soc2-tsc, iso27001, iso-27001.",
    )
    parser.add_argument(
        "--format", choices=("md", "json", "html", "all"),
        default="html",
        help="Output format. 'all' emits md+json+html (default for "
        "--audit-packet).",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Output directory "
        "(default: <root>/.cadence/reports/<YYYY-MM-DD>).",
    )
    parser.add_argument(
        "--audit-packet", action="store_true",
        help="Also bundle the report with ADR index, FRAMEWORK_CHANGELOG, "
        "and last verify output into <output-dir>/audit-packet/. "
        "Implies --format all.",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any in-scope item is in status 'gap'.",
    )
    parser.add_argument(
        "--reproducible", action="store_true",
        help="Use a fixed placeholder for the generated-at timestamp "
        "(for byte-stable test output).",
    )
    args = parser.parse_args(argv)

    project_root = args.project_root.resolve()
    standards_dir = (
        args.standards_dir.resolve()
        if args.standards_dir is not None
        else _find_standards_dir(project_root)
    )
    if standards_dir is None or not standards_dir.is_dir():
        print(
            "ERROR: could not locate standards directory. Pass "
            "--standards-dir explicitly.",
            file=sys.stderr,
        )
        return 2

    if args.audit_packet and args.format != "all":
        args.format = "all"

    # Resolve --standard aliases and the "all" sentinel. "all" or no
    # --standard means no filter; everything else is normalized to its
    # canonical YAML id (e.g. "ssdf" → "nist-ssdf").
    aliases = {
        "ssdf": "nist-ssdf",
        "nist-ssdf": "nist-ssdf",
        "iso25010": "iso-25010",
        "iso-25010": "iso-25010",
        "soc2": "aicpa-soc2-tsc",
        "aicpa-soc2-tsc": "aicpa-soc2-tsc",
        "iso27001": "iso-27001",
        "iso-27001": "iso-27001",
    }
    raw_selected = args.standard or []
    if not raw_selected or "all" in raw_selected:
        selected: list[str] = []
    else:
        selected = []
        for value in raw_selected:
            canonical = aliases.get(value)
            if canonical is None:
                print(
                    f"ERROR: unknown --standard value {value!r}. "
                    f"Known: {', '.join(sorted(set(aliases))) }, all.",
                    file=sys.stderr,
                )
                return 2
            if canonical not in selected:
                selected.append(canonical)

    report = generate(
        project_root=project_root,
        standards_dir=standards_dir,
        selected=selected,
        reproducible=args.reproducible,
    )

    today = dt.date.today().isoformat()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else (project_root / ".cadence" / "reports" / today).resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    formats = ["md", "json", "html"] if args.format == "all" else [args.format]
    written: dict[str, Path] = {}
    for fmt in formats:
        if fmt == "md":
            content = _render_md(report)
        elif fmt == "json":
            content = _render_json(report)
        else:
            content = _render_html(report)
        target = output_dir / f"compliance-report.{fmt}"
        target.write_text(content, encoding="utf-8")
        written[fmt] = target
        print(f"wrote: {target}")

    if args.audit_packet:
        packet = _build_audit_packet(project_root, output_dir, written)
        print(f"audit packet: {packet}")

    if args.strict:
        gaps = [r for r in report.items if r.status == "gap"]
        if gaps:
            print(
                f"\n--strict: {len(gaps)} gap(s) in report.",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
