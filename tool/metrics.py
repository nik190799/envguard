#!/usr/bin/env python3
"""Cadence learning metrics: repeat rate and escape rate from cadence/state.

Reads only an extracted ``cadence/state`` (and, optionally, the repo's
``.cadence/``) and makes no network calls. Mistake classes are recomputed
from each attempt's evidence with one detector set, so both arms of the
rules-on vs rules-frozen eval are scored the same way, and an edge seeded
later also scores earlier attempts (docs/LEARNING.md, "Metrics").

Contract:
    report   python tool/metrics.py report --state-dir DIR [--repo-root DIR]
                 [--config FILE] [--detector-set current|FILE]
                 [--order time|issue] [--window N] [--since TS]
                 [--judge-pairs FILE] [--schema-dir DIR] [--now EPOCH]
                 --out FILE
             Writes a metrics.schema.json report.
    compare  python tool/metrics.py compare --on DIR [--on DIR ...]
                 --frozen DIR [--frozen DIR ...] --ticket-map FILE
                 [--detector-set union|FILE] [--order issue|time]
                 [--window 0] [--resamples 2000] [--seed 13]
                 [--config FILE] --out FILE
             Writes {"schema": "cadence.eval-compare/1", "arms", "diff",
             "resamples", "seed", "pass", "reasons"}.

Definitions (headline families: import-edge, guarded, missing-test, test):
    - Attempts: build observations whose patch applied, deduped by
      (repo, issue, patch_sha256), earliest kept, published if any
      duplicate was. Other observations are ops and are not scored.
    - Order: completed_at (time) or issue number then completed_at (issue).
    - C(a): the classes of attempt a, from its evidence and the detector
      set M (seeded import edges plus the intrinsic families).
    - Prior_N(a): the union of C(b) over the N attempts before a whose
      issue differs from a's (N = --window, default learning.repeat_window;
      0 = all earlier attempts).
    - Exposure X(a, k): for import-edge:<from>->... and missing-test:<area>,
      a added or modified a file in that area; every other family is
      always exposed.
    - O = {(a, k): k in Prior_N(a) and X(a, k)}; R = {(a, k) in O: k in C(a)};
      E = {(a, k) in R: a was published}. RR = |R|/|O|, ER = |E|/|O|,
      share = |E|/|R|, per_10_attempts = 10 |E| / scored attempts.
    - A Wilson 95% interval is given only when |O| >= 30; below that the
      status is "insufficient". Completeness = run ids with an observation
      / distinct run ids of build records with outcome success or failure
      recorded on or after --since (default: the earliest observation);
      below 0.95 the status is "incomplete" and the report exits 1.
    - Post-PR classes (edit, review) are scored the same way over
      harvested PRs only, and never mixed into RR.
    - learned_check_catches: attempts on an issue outside a learned
      check's evidence, on or after its promotion, where its L- rule hit.
      post_promotion_exposed_no_repeat: such attempts exposed to a promoted
      lesson's class that did not repeat it.
    - lessons_cited (informational; no other number reads it): over the
      scored attempts, those whose observation recorded which active
      lessons the approved spec cited (lessons_cited a list) versus unknown
      (null, or an observation from before the field). For each cited
      lesson L of a known attempt a: "present" when C(a) holds L's class
      (lesson_id(k) == L for some k in C(a)), else "absent". Absent is not
      prevention: a may never have touched L's area. Prevention is measured
      by the rules-on vs rules-frozen eval (compare), not here.
    - first_pass_verify: share of issues whose first attempt passed verify.
      test_tampering_rate: share of attempts that modify or delete files
      under a test root. merge_rate_30d: published PRs merged within 30 days
      of publication, over PRs published at least 30 days ago or already
      closed. new_class_rate: classes first seen per scored attempt.
    - cost: median build cost per attempt (all its runs), mean total spend
      (spec, build, learn) per attempt, and total spend per merged PR.
    - --judge-pairs merges classes labelled same-root-cause in a shadow
      column only (judge_shadow); the structured keys stay the headline.
    - compare: pooled RR and ER per arm; diff = frozen - on with a cluster
      bootstrap over ticket ids (--resamples, --seed). The eval passes when
      the 90% interval of RR_frozen - RR_on lies above 0, ER_on < ER_frozen,
      the on arm's tampering rate is no higher, and its median cost per
      attempt is at most 1.25 x the frozen arm's.

Input files:
    ticket map      {"schema": "cadence.ticket-map/1",
                     "tickets": {"<owner>/<repo>#<issue>": "T01"}}
    detector file   {"schema": "cadence.detectors/1",
                     "import_edges": [keys],
                     "families": [subset of guarded, missing-test, test]}
    judge pairs     JSON lines {"a", "b", "label": "same-root-cause|related|
                     different|other", "backend"}

Exit codes:
    report   0 ok; 1 incomplete (the file is still written); 2 bad input
    compare  0 the eval passes; 1 it does not; 2 bad input
    Internal errors exit 2.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import statistics
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

_TOOL_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    # Never leave tool/__pycache__ in the repo (the retro guard refuses it).
    sys.dont_write_bytecode = True


def _load_ladder() -> Any:
    name = "_cadence_metrics_ladder"
    if name in sys.modules:
        return sys.modules[name]
    path = _TOOL_DIR / "ladder.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        print(f"ERROR: could not load {path}", file=sys.stderr)
        sys.exit(2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ladder = _load_ladder()
LadderError = ladder.LadderError

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_NOT_PASSED = 1
EXIT_BAD_INPUT = 2

WILSON_MIN_OPPORTUNITIES = 30
COMPLETENESS_MIN = 0.95
JUDGE_LABELS = ("same-root-cause", "related", "different", "other")
_Z95 = 1.959963984540054


# --- Numbers ------------------------------------------------------------------------


def _r(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _rate(num: int, den: int) -> float | None:
    return _r(num / den) if den else None


def wilson(k: int, n: int, z: float = _Z95) -> list[float] | None:
    """Wilson score interval for k successes in n trials."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [_r(max(0.0, centre - half)), _r(min(1.0, centre + half))]


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 1]."""
    if not values:
        return None
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


# --- Detector sets -----------------------------------------------------------------------


def detector_from_file(path: Path) -> Any:
    raw = ladder._read_json(path)
    if not isinstance(raw, dict) or raw.get("schema") != "cadence.detectors/1":
        raise LadderError(f"{path}: schema must be cadence.detectors/1")
    edges = raw.get("import_edges")
    families = raw.get("families")
    if not isinstance(edges, list) or not isinstance(families, list):
        raise LadderError(f"{path}: needs import_edges and families lists")
    for key in edges:
        if not (isinstance(key, str) and re.fullmatch(ladder.CLASS_KEY_RE, key) and ladder.parse_edge_key(key)):
            raise LadderError(f"{path}: {key!r} is not an import-edge key")
    for fam in families:
        if fam not in ladder.INTRINSIC_FAMILIES:
            raise LadderError(f"{path}: family {fam!r} is not one of {', '.join(ladder.INTRINSIC_FAMILIES)}")
    return ladder.DetectorSet("file", frozenset(edges), frozenset(families))


def current_detector(state: Any, lessons: Sequence[dict[str, Any]], rules: Sequence[Any]) -> Any:
    seeds = ladder.collect_seeds(
        state.observations, state.all_findings(), [l["class_key"] for l in lessons], rules
    )
    return ladder.DetectorSet("current", frozenset(seeds), frozenset(ladder.INTRINSIC_FAMILIES))


# --- Scoring ---------------------------------------------------------------------------------


def order_attempts(attempts: Iterable[Any], order: str) -> list[Any]:
    if order == "issue":
        return sorted(attempts, key=lambda a: (a.issue, a.completed_at, a.run))
    return sorted(attempts, key=lambda a: (a.completed_at, a.run))


@dataclass
class Score:
    ordered: list[Any]
    classes: dict[str, set[str]]
    opportunities: list[tuple[Any, str]]
    repeats: list[tuple[Any, str]]
    escapes: list[tuple[Any, str]]


def score(
    attempts: Iterable[Any],
    det: Any,
    *,
    order: str,
    window: int,
    mapping: dict[str, str] | None = None,
) -> Score:
    """O, R and E for ``attempts`` (docs/LEARNING.md, Metrics)."""
    ordered = order_attempts(attempts, order)
    raw = {a.run: set(ladder.attempt_classes(a.obs, det)) for a in ordered}

    def mapped(keys: Iterable[str]) -> set[str]:
        return {mapping.get(k, k) for k in keys} if mapping else set(keys)

    classes = {run: mapped(keys) for run, keys in raw.items()}
    opportunities: list[tuple[Any, str]] = []
    repeats: list[tuple[Any, str]] = []
    escapes: list[tuple[Any, str]] = []
    for idx, a in enumerate(ordered):
        earlier = [b for b in ordered[:idx] if b.issue != a.issue]
        if window > 0:
            earlier = earlier[-window:]
        prior_raw: set[str] = set()
        for b in earlier:
            prior_raw |= raw[b.run]
        exposed: set[str] = set()
        for k in prior_raw:
            if ladder.is_exposed(a.obs, k):
                exposed.add(mapping.get(k, k) if mapping else k)
        for k in sorted(exposed):
            opportunities.append((a, k))
            if k in classes[a.run]:
                repeats.append((a, k))
                if a.published:
                    escapes.append((a, k))
    return Score(ordered, classes, opportunities, repeats, escapes)


def _family(key: str) -> str:
    return key.split(":", 1)[0]


# --- Report ------------------------------------------------------------------------------------


def _since_filter(state: Any, since: datetime | None) -> tuple[list[Any], int, int]:
    observations = [o for o in state.observations if since is None or o.completed_at >= since]
    return ladder.dedupe_attempts(observations)


def _completeness(state: Any, since: datetime | None) -> dict[str, Any]:
    build_runs = {
        r["run_id"]
        for r in state.runs.values()
        if r["stage"] == "build"
        and r["outcome"] in ("success", "failure")
        and (since is None or r["recorded"] >= since)
    }
    observed_ids = {o.run_id for o in state.observations}
    observed = len(build_runs & observed_ids)
    ratio = (observed / len(build_runs)) if build_runs else 1.0
    return {"build_runs": len(build_runs), "observed": observed, "ratio": _r(ratio)}


def _promotions(state: Any, lessons: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """lesson id -> {key, rung, at (date), issues} for every promotion we know of."""
    out: dict[str, dict[str, Any]] = {}
    for decision in state.decisions:
        if not decision["merged"] or not decision["plan_sha"]:
            continue
        plan = state.plans.get(decision["plan_sha"]) or {}
        planned = {t.get("class_key"): t for t in plan.get("transitions", []) if isinstance(t, dict)}
        for t in decision["transitions"]:
            if not t["landed"] or t["to"] not in ("pattern", "check"):
                continue
            lid = ladder.lesson_id(t["class_key"])
            issues = planned.get(t["class_key"], {}).get("issues", [])
            out[lid] = {
                "key": t["class_key"],
                "rung": t["to"],
                "at": decision["closed"].date(),
                "issues": {i for i in issues if isinstance(i, int)},
            }
    for lesson in lessons:
        promoted = [h for h in lesson.get("history", []) if h.get("rung") in ("pattern", "check")]
        if not promoted and lesson["rung"] not in ("pattern", "check"):
            continue
        last = promoted[-1] if promoted else {"rung": lesson["rung"], "on": lesson["since"]}
        at = ladder.parse_date(last["on"])
        if at is None:
            continue
        out[lesson["id"]] = {
            "key": lesson["class_key"],
            "rung": last["rung"],
            "at": at,
            "issues": set(lesson.get("issues", [])),
        }
    return out


def _post_pr(state: Any, order: str, window: int) -> dict[str, Any]:
    prs = [h for h in state.harvest.values() if h["status"] == "ok"]
    classes: dict[int, set[str]] = defaultdict(set)
    for stem, items in state.findings.items():
        if not stem.startswith("pr-"):
            continue
        number = stem[3:].split("-", 1)[0]
        if not number.isdigit():
            continue
        for f in items:
            if f.family in ladder.POST_PR_FAMILIES:
                classes[int(number)].add(f.class_key)
    if order == "issue":
        prs.sort(key=lambda h: (h["issue"] or 0, h["closed"], h["pr"]))
    else:
        prs.sort(key=lambda h: (h["closed"], h["pr"]))
    opportunities = repeats = 0
    for idx, pr in enumerate(prs):
        earlier = [p for p in prs[:idx] if p["issue"] != pr["issue"]]
        if window > 0:
            earlier = earlier[-window:]
        prior: set[str] = set()
        for p in earlier:
            prior |= classes.get(p["pr"], set())
        opportunities += len(prior)
        repeats += len(prior & classes.get(pr["pr"], set()))
    return {
        "prs_harvested": len(prs),
        "opportunities": opportunities,
        "repeats": repeats,
        "rate": _rate(repeats, opportunities),
    }


def _lessons_cited(sc: Score) -> dict[str, Any]:
    """Informational: the lessons the approved specs cited, and whether each
    cited lesson's class still occurs among the attempt's classes C(a).

    A lesson's id is ``lesson_id`` of its class key, so "present" needs no
    lookup: some class k of the attempt has lesson_id(k) == the cited id.
    Unknown attempts (lessons_cited null or absent) are counted as unknown,
    never as absent. Nothing here feeds RR, ER or learned_check_catches."""
    with_citation = unknown = 0
    absent: dict[str, int] = {}
    present: dict[str, int] = {}
    for a in sc.ordered:
        cited = a.obs.lessons_cited
        if cited is None:
            unknown += 1
            continue
        if cited:
            with_citation += 1
        ids = {ladder.lesson_id(k) for k in sc.classes[a.run]}
        for lid in cited:
            bucket = present if lid in ids else absent
            bucket[lid] = bucket.get(lid, 0) + 1
    return {
        "attempts_with_citation": with_citation,
        "attempts_unknown": unknown,
        "cited_and_absent": {"count": sum(absent.values()), "by_lesson": dict(sorted(absent.items()))},
        "cited_and_present": {"count": sum(present.values()), "by_lesson": dict(sorted(present.items()))},
    }


def _merge_rate(state: Any, now: datetime) -> float | None:
    published: dict[int, datetime] = {}
    for record in state.prs:
        if record["pr"] not in published or record["recorded"] < published[record["pr"]]:
            published[record["pr"]] = record["recorded"]
    eligible = merged = 0
    for pr, at in published.items():
        harvest = state.harvest.get(pr)
        if harvest is None and now - at < timedelta(days=30):
            continue
        eligible += 1
        if harvest is not None and harvest["merged"] and harvest["closed"] - at <= timedelta(days=30):
            merged += 1
    return _rate(merged, eligible)


def _cost(state: Any, attempts: Sequence[Any], since: datetime | None) -> dict[str, Any]:
    per_attempt = []
    for a in attempts:
        records = [state.runs[r] for r in a.runs if r in state.runs]
        if records:
            per_attempt.append(sum(r["booked_usd"] for r in records))
    total = sum(r["booked_usd"] for r in state.runs.values() if since is None or r["recorded"] >= since)
    merged = sum(1 for h in state.harvest.values() if h["merged"])
    return {
        "per_attempt_median": _r(statistics.median(per_attempt)) if per_attempt else None,
        "per_attempt_mean": _r(total / len(attempts)) if attempts else None,
        "per_merged_pr": _r(total / merged) if merged else None,
    }


def _judge_mapping(path: Path) -> tuple[dict[str, str], int]:
    """class -> representative for same-root-cause pairs (union-find)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = 0
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LadderError(f"could not read --judge-pairs {path}: {exc}") from exc
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise LadderError(f"{path}: a line is not JSON") from exc
        a, b, label = (item.get(k) if isinstance(item, dict) else None for k in ("a", "b", "label"))
        if label not in JUDGE_LABELS:
            raise LadderError(f"{path}: label {label!r} is not one of {', '.join(JUDGE_LABELS)}")
        valid = all(isinstance(k, str) and re.fullmatch(ladder.CLASS_KEY_RE, k) for k in (a, b))
        if not valid:
            continue
        pairs += 1
        if label == "same-root-cause":
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    mapping = {k: find(k) for k in list(parent)}
    return {k: v for k, v in mapping.items() if k != v}, pairs


def build_report(
    state: Any,
    settings: Any,
    det: Any,
    *,
    order: str,
    window: int,
    since: datetime | None,
    lessons: Sequence[dict[str, Any]],
    judge_pairs: Path | None,
    now: float,
    repo: str | None = None,
) -> dict[str, Any]:
    if since is None and state.observations:
        since = min(o.completed_at for o in state.observations)
    attempts, ops, deduped = _since_filter(state, since)
    sc = score(attempts, det, order=order, window=window)
    n_o, n_r, n_e = len(sc.opportunities), len(sc.repeats), len(sc.escapes)
    completeness = _completeness(state, since)
    if completeness["ratio"] < COMPLETENESS_MIN:
        status = "incomplete"
    elif n_o >= WILSON_MIN_OPPORTUNITIES:
        status = "ok"
    else:
        status = "insufficient"
    ci_ok = n_o >= WILSON_MIN_OPPORTUNITIES

    by_family: dict[str, dict[str, Any]] = {}
    for fam in ladder.HEADLINE_FAMILIES:
        o = sum(1 for _, k in sc.opportunities if _family(k) == fam)
        r = sum(1 for _, k in sc.repeats if _family(k) == fam)
        e = sum(1 for _, k in sc.escapes if _family(k) == fam)
        by_family[fam] = {"opportunities": o, "repeats": r, "escapes": e, "rr": _rate(r, o), "er": _rate(e, o)}

    seen: set[str] = set()
    new_classes = 0
    for a in sc.ordered:
        new_classes += len(sc.classes[a.run] - seen)
        seen |= sc.classes[a.run]

    promotions = _promotions(state, lessons)
    catches: dict[str, int] = {}
    no_repeat = 0
    for lid, promo in sorted(promotions.items()):
        for a in sc.ordered:
            if a.completed_at.date() < promo["at"] or a.issue in promo["issues"]:
                continue
            if promo["rung"] == "check" and any(h.rule_id == lid for h in a.obs.rule_hits):
                catches[lid] = catches.get(lid, 0) + 1
            if ladder.is_exposed(a.obs, promo["key"]) and promo["key"] not in sc.classes[a.run]:
                no_repeat += 1

    first: dict[int, Any] = {}
    for a in sc.ordered:
        first.setdefault(a.issue, a)
    first_pass = _rate(sum(1 for a in first.values() if a.obs.verify_result == "success"), len(first))
    roots = tuple(getattr(settings, "test_roots", ("tests", "test")))
    tampered = sum(1 for a in sc.ordered if tampered_with_tests(a.obs, roots))

    rungs = {l["class_key"]: l["rung"] for l in lessons}
    per_class: dict[str, dict[str, Any]] = {}
    for a in sc.ordered:
        for key in sc.classes[a.run]:
            entry = per_class.setdefault(key, {"issues": set(), "occurrences": 0, "escaped": 0, "last": a.completed_at})
            entry["issues"].add(a.issue)
            entry["occurrences"] += 1
            entry["escaped"] += 1 if a.published else 0
            entry["last"] = max(entry["last"], a.completed_at)
    by_class = [
        {
            "key": key,
            "rung": rungs.get(key, "note"),
            "issues": len(v["issues"]),
            "occurrences": v["occurrences"],
            "escaped": v["escaped"],
            "last_seen": ladder.iso_utc(v["last"]),
        }
        for key, v in sorted(per_class.items(), key=lambda kv: (-kv[1]["occurrences"], kv[0]))
    ][:500]

    judge = None
    if judge_pairs is not None:
        mapping, pairs = _judge_mapping(judge_pairs)
        shadow = score(attempts, det, order=order, window=window, mapping=mapping)
        so, sr, se = len(shadow.opportunities), len(shadow.repeats), len(shadow.escapes)
        judge = {
            "pairs": pairs,
            "merged_keys": len(mapping),
            "repeat": {"opportunities": so, "repeats": sr, "rate": _rate(sr, so)},
            "escape": {"escapes": se, "rate": _rate(se, so)},
        }

    if repo is None:
        repos = Counter(o.repo for o in state.observations)
        repo = repos.most_common(1)[0][0] if repos else None
    if repo is None:
        env_repo = os.environ.get("GITHUB_REPOSITORY", "")
        repo = env_repo if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", env_repo) else "unknown/unknown"

    scored = len(sc.ordered)
    return {
        "schema": "cadence.metrics/1",
        "generated_at": ladder.iso_utc(ladder.to_utc(now)),
        "repo": repo,
        "detector_set": {"kind": det.kind, "sha256": det.sha256()},
        "metrics_code_sha256": metrics_code_sha256(),
        "order": order,
        "window": window,
        "since": ladder.iso_utc(since) if since is not None else None,
        "completeness": completeness,
        "attempts": {"scored": scored, "ops": ops, "deduped": deduped},
        "repeat": {
            "opportunities": n_o,
            "repeats": n_r,
            "rate": _rate(n_r, n_o),
            "ci95": wilson(n_r, n_o) if ci_ok else None,
            "status": status,
        },
        "escape": {
            "escapes": n_e,
            "rate": _rate(n_e, n_o),
            "share": _rate(n_e, n_r),
            "per_10_attempts": _r(10 * n_e / scored) if scored else None,
            "ci95": wilson(n_e, n_o) if ci_ok else None,
        },
        "by_family": by_family,
        "post_pr": _post_pr(state, order, window),
        "new_class_rate": _r(new_classes / scored) if scored else None,
        "learned_check_catches": {"count": sum(catches.values()), "by_lesson": catches},
        "post_promotion_exposed_no_repeat": no_repeat,
        "lessons_cited": _lessons_cited(sc),
        "first_pass_verify": first_pass,
        "test_tampering_rate": _rate(tampered, scored),
        "merge_rate_30d": _merge_rate(state, ladder.to_utc(now)),
        "cost": _cost(state, sc.ordered, since),
        "by_class": by_class,
        "judge_shadow": judge,
        "unreadable": state.unreadable,
    }


def metrics_code_sha256() -> str:
    digest = hashlib.sha256()
    for name in ("metrics.py", "ladder.py"):
        digest.update((_TOOL_DIR / name).read_bytes())
    return digest.hexdigest()


# --- Compare ------------------------------------------------------------------------------------


def load_ticket_map(path: Path) -> dict[str, str]:
    raw = ladder._read_json(path)
    tickets = raw.get("tickets") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or raw.get("schema") != "cadence.ticket-map/1" or not isinstance(tickets, dict):
        raise LadderError(f"{path}: needs schema cadence.ticket-map/1 and a tickets object")
    out = {}
    for key, ticket in tickets.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}#[1-9][0-9]{0,9}", key):
            raise LadderError(f"{path}: {key!r} is not <owner>/<repo>#<issue>")
        if not (isinstance(ticket, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", ticket)):
            raise LadderError(f"{path}: ticket id for {key} must be 1-64 letters, digits, '.', '_' or '-'")
        out[key] = ticket
    return out


@dataclass
class TicketTotals:
    o: int = 0
    r: int = 0
    e: int = 0
    attempts: int = 0
    tampered: int = 0


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def tampered_with_tests(obs: Any, test_roots: Sequence[str]) -> bool:
    """The attempt modified or deleted an existing file under a test root.

    Test roots may be nested (server/tests): a guarded operation counts when
    its root is a test root or lies under one, or its path lies under one,
    so it does not matter which guarded path observe named it after."""
    return any(
        g.op in ("modify", "delete")
        and any(_within(g.root, r) or g.path.startswith(r + "/") for r in test_roots)
        for g in obs.guarded
    )


def arm_totals(
    states: Sequence[Any],
    det: Any,
    tickets: dict[str, str],
    *,
    order: str,
    window: int,
    test_roots: Iterable[str],
) -> tuple[dict[str, TicketTotals], list[float]]:
    roots = tuple(test_roots)
    per_ticket: dict[str, TicketTotals] = defaultdict(TicketTotals)
    costs: list[float] = []
    for state in states:
        sc = score(state.attempts, det, order=order, window=window)
        unmapped = 0
        for a in sc.ordered:
            ticket = tickets.get(f"{a.repo}#{a.issue}")
            if ticket is None:
                unmapped += 1
                continue
            totals = per_ticket[ticket]
            totals.attempts += 1
            if tampered_with_tests(a.obs, roots):
                totals.tampered += 1
            records = [state.runs[r] for r in a.runs if r in state.runs]
            if records:
                costs.append(sum(r["booked_usd"] for r in records))
        for bucket, attr in ((sc.opportunities, "o"), (sc.repeats, "r"), (sc.escapes, "e")):
            for a, _ in bucket:
                ticket = tickets.get(f"{a.repo}#{a.issue}")
                if ticket is not None:
                    setattr(per_ticket[ticket], attr, getattr(per_ticket[ticket], attr) + 1)
        if unmapped:
            print(f"WARN: {unmapped} attempt(s) have no ticket in the map and are not scored", file=sys.stderr)
    return dict(per_ticket), costs


def _pooled(per_ticket: dict[str, TicketTotals], tickets: Iterable[str]) -> tuple[float | None, float | None]:
    o = r = e = 0
    for t in tickets:
        totals = per_ticket.get(t)
        if totals is not None:
            o, r, e = o + totals.o, r + totals.r, e + totals.e
    return (r / o if o else None), (e / o if o else None)


def compare(
    on_states: Sequence[Any],
    frozen_states: Sequence[Any],
    tickets: dict[str, str],
    det: Any,
    *,
    order: str,
    window: int,
    resamples: int,
    seed: int,
    test_roots: Iterable[str],
) -> dict[str, Any]:
    roots = tuple(test_roots)
    on, on_costs = arm_totals(on_states, det, tickets, order=order, window=window, test_roots=roots)
    frozen, frozen_costs = arm_totals(frozen_states, det, tickets, order=order, window=window, test_roots=roots)

    def summary(per_ticket: dict[str, TicketTotals], costs: list[float]) -> dict[str, Any]:
        rr, er = _pooled(per_ticket, per_ticket.keys())
        attempts = sum(t.attempts for t in per_ticket.values())
        tampered = sum(t.tampered for t in per_ticket.values())
        return {
            "rr": rr,
            "er": er,
            "tampering": (tampered / attempts) if attempts else None,
            "cost_median": statistics.median(costs) if costs else None,
        }

    raw = {"on": summary(on, on_costs), "frozen": summary(frozen, frozen_costs)}
    arms = {arm: {k: _r(v) for k, v in values.items()} for arm, values in raw.items()}
    ids = sorted(set(on) | set(frozen))
    rng = random.Random(seed)
    rr_diffs: list[float] = []
    er_diffs: list[float] = []
    for _ in range(resamples):
        draw = [rng.choice(ids) for _ in ids] if ids else []
        rr_on, er_on = _pooled(on, draw)
        rr_fr, er_fr = _pooled(frozen, draw)
        if rr_on is None or rr_fr is None:
            continue
        rr_diffs.append(rr_fr - rr_on)
        er_diffs.append(er_fr - er_on)

    def interval(values: list[float], lo: float, hi: float) -> list[float] | None:
        if not values:
            return None
        return [_r(percentile(values, lo)), _r(percentile(values, hi))]

    def diff(name: str, values: list[float]) -> dict[str, Any]:
        a, b = raw["frozen"][name], raw["on"][name]
        return {
            "point": _r(a - b) if a is not None and b is not None else None,
            "ci90": interval(values, 0.05, 0.95),
            "ci95": interval(values, 0.025, 0.975),
        }

    result_diff = {"rr": diff("rr", rr_diffs), "er": diff("er", er_diffs)}
    reasons = []
    ci90 = result_diff["rr"]["ci90"]
    if ci90 is None or not ci90[0] > 0:
        reasons.append("the 90% interval of RR_frozen - RR_on does not lie above 0")
    er_on, er_fr = raw["on"]["er"], raw["frozen"]["er"]
    if er_on is None or er_fr is None or not er_on < er_fr:
        reasons.append("ER_on is not below ER_frozen")
    t_on, t_fr = raw["on"]["tampering"], raw["frozen"]["tampering"]
    if t_on is None or t_fr is None or not t_on <= t_fr:
        reasons.append("the on arm's test-tampering rate is higher (or unknown)")
    c_on, c_fr = raw["on"]["cost_median"], raw["frozen"]["cost_median"]
    if c_on is None or c_fr is None or not c_on <= 1.25 * c_fr:
        reasons.append("the on arm's median cost exceeds 1.25 x the frozen arm's (or is unknown)")
    return {
        "schema": "cadence.eval-compare/1",
        "arms": arms,
        "diff": result_diff,
        "resamples": resamples,
        "seed": seed,
        "pass": not reasons,
        "reasons": reasons,
    }


# --- CLI -----------------------------------------------------------------------------------------


def _finite(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be finite")
    return value


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    rep = sub.add_parser("report", help="repeat and escape rates for one state branch")
    rep.add_argument("--state-dir", type=Path, required=True)
    rep.add_argument("--repo-root", type=Path)
    rep.add_argument("--config", type=Path)
    rep.add_argument("--detector-set", default="current")
    rep.add_argument("--order", choices=("time", "issue"), default="time")
    rep.add_argument("--window", type=_count)
    rep.add_argument("--since")
    rep.add_argument("--judge-pairs", type=Path)
    rep.add_argument("--schema-dir", type=Path)
    rep.add_argument("--now", type=_finite)
    rep.add_argument("--out", type=Path, required=True)

    cmp_ = sub.add_parser("compare", help="rules-on vs rules-frozen eval")
    cmp_.add_argument("--on", type=Path, action="append", required=True)
    cmp_.add_argument("--frozen", type=Path, action="append", required=True)
    cmp_.add_argument("--ticket-map", type=Path, required=True)
    cmp_.add_argument("--detector-set", default="union")
    cmp_.add_argument("--order", choices=("issue", "time"), default="issue")
    cmp_.add_argument("--window", type=_count, default=0)
    cmp_.add_argument("--resamples", type=_count, default=2000)
    cmp_.add_argument("--seed", type=int, default=13)
    cmp_.add_argument("--config", type=Path)
    cmp_.add_argument("--schema-dir", type=Path)
    cmp_.add_argument("--out", type=Path, required=True)
    return parser


def _cmd_report(args: argparse.Namespace, now: float) -> int:
    root = args.repo_root.resolve() if args.repo_root else None
    schemas = ladder.Schemas(root, args.schema_dir)
    if args.config is not None:
        settings = ladder.load_settings(args.config, explicit=True)
    elif root is not None:
        settings = ladder.load_settings(root / ".cadence" / "factory.yaml")
    else:
        settings = ladder.Settings()
    state = ladder.read_state(args.state_dir, schemas)
    lessons = ladder.load_lessons(root, schemas) if root is not None else []
    rules = ladder.load_rules(root) if root is not None else []
    if args.detector_set == "current":
        det = current_detector(state, lessons, rules)
    else:
        det = detector_from_file(Path(args.detector_set))
    since = None
    if args.since:
        since = ladder.parse_ts(args.since)
        if since is None:
            raise LadderError(f"--since {args.since!r} is not a timestamp")
    window = args.window if args.window is not None else settings.repeat_window
    report = build_report(
        state,
        settings,
        det,
        order=args.order,
        window=window,
        since=since,
        lessons=lessons,
        judge_pairs=args.judge_pairs,
        now=now,
    )
    problems = schemas.errors("metrics.schema.json", report, required=False)
    if problems:
        raise LadderError("the report fails metrics.schema.json: " + "; ".join(problems[:5]))
    ladder._write_json(args.out, report)
    rep = report["repeat"]
    print(
        json.dumps(
            {
                "status": rep["status"],
                "repeat_rate": rep["rate"],
                "escape_rate": report["escape"]["rate"],
                "opportunities": rep["opportunities"],
                "completeness": report["completeness"]["ratio"],
            }
        )
    )
    return EXIT_INCOMPLETE if rep["status"] == "incomplete" else EXIT_OK


def _cmd_compare(args: argparse.Namespace) -> int:
    schemas = ladder.Schemas(None, args.schema_dir)
    settings = ladder.load_settings(args.config, explicit=True) if args.config else ladder.Settings()
    tickets = load_ticket_map(args.ticket_map)
    on_states = [ladder.read_state(d, schemas) for d in args.on]
    frozen_states = [ladder.read_state(d, schemas) for d in args.frozen]
    if args.detector_set == "union":
        seeds: set[str] = set()
        for state in [*on_states, *frozen_states]:
            seeds |= ladder.collect_seeds(state.observations, state.all_findings())
        det = ladder.DetectorSet("union", frozenset(seeds), frozenset(ladder.INTRINSIC_FAMILIES))
    else:
        det = detector_from_file(Path(args.detector_set))
    result = compare(
        on_states,
        frozen_states,
        tickets,
        det,
        order=args.order,
        window=args.window,
        resamples=args.resamples,
        seed=args.seed,
        test_roots=settings.test_roots,
    )
    result["detector_set"] = {"kind": det.kind, "sha256": det.sha256()}
    ladder._write_json(args.out, result)
    print(json.dumps({"pass": result["pass"], "reasons": result["reasons"]}))
    return EXIT_OK if result["pass"] else EXIT_NOT_PASSED


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "report":
            now = args.now if args.now is not None else time.time()
            return _cmd_report(args, now)
        return _cmd_compare(args)
    except LadderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except Exception:  # noqa: BLE001 - exit 1 means "incomplete" or "not passed"
        traceback.print_exc()
        print("ERROR: internal error in metrics.py (see traceback)", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
