#!/usr/bin/env bash
# Cadence verify orchestrator (POSIX).
#
# Reads .cadence/cadence.yaml and runs:
#   1. commands.format    (each entry as a separate command)
#   2. commands.lint
#   3. tool/check_boundaries.py
#   4. commands.test
#
# Fails fast on first non-zero exit. Prints a Definition-of-Done-shaped
# summary at the end.
#
# Writes verify evidence for tool/compliance_report.py under .cadence/:
#   last_verify.log   this run's output, without colour codes
#   .last_verify_ok   present only after a fully green run
#   .last_verify_sha  the commit verified ("-dirty" if the tree had changes)
# Stale .last_verify_ok / .last_verify_sha are removed before every run, so
# a failed or interrupted run never leaves an old pass behind.
#
# Usage:  scripts/verify.sh
# Exit:   0 = all green; non-zero = first failing step's exit code.

set -uo pipefail
start_ts=$(date +%s)

CONFIG="${CADENCE_CONFIG:-.cadence/cadence.yaml}"
ROOT="${CADENCE_ROOT:-$(pwd)}"

EVIDENCE_DIR="$ROOT/.cadence"
OK_MARKER="$EVIDENCE_DIR/.last_verify_ok"
SHA_MARKER="$EVIDENCE_DIR/.last_verify_sha"
LOG_FILE="$EVIDENCE_DIR/last_verify.log"
rm -f "$OK_MARKER" "$SHA_MARKER"

color_cyan="\033[36m"
color_green="\033[32m"
color_red="\033[31m"
color_dim="\033[2m"
color_reset="\033[0m"

if [ ! -f "$ROOT/$CONFIG" ]; then
  echo -e "${color_red}FAIL${color_reset}: cadence config not found at $CONFIG" >&2
  echo "Hint: run /cadence-init to scaffold one." >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo -e "${color_red}FAIL${color_reset}: python3 (or python) is required" >&2
  exit 2
fi

PY=$(command -v python3 || command -v python)

# Read commands.<section> from cadence.yaml; print one shell command per line.
read_commands() {
  local section="$1"
  "$PY" - "$ROOT/$CONFIG" "$section" <<'PYEOF'
import sys
try:
    import yaml
except ImportError:
    print("__MISSING_PYYAML__")
    sys.exit(0)
path, section = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh) or {}
cmds = (cfg.get("commands") or {}).get(section) or []
if isinstance(cmds, str):
    cmds = [cmds]
for c in cmds:
    print(c)
PYEOF
}

run_step() {
  local label="$1"
  shift
  local commands="$@"
  echo
  echo -e "${color_cyan}==> ${label}${color_reset}"

  if [ -z "$commands" ]; then
    echo -e "${color_dim}(no commands configured for ${label}; skipping)${color_reset}"
    return 0
  fi

  while IFS= read -r cmd; do
    [ -z "$cmd" ] && continue
    if [ "$cmd" = "__MISSING_PYYAML__" ]; then
      echo -e "${color_red}FAIL${color_reset}: PyYAML not installed (pip install pyyaml)" >&2
      exit 2
    fi
    echo -e "${color_dim}\$ ${cmd}${color_reset}"
    bash -c "$cmd"
    local rc=$?
    if [ $rc -ne 0 ]; then
      echo -e "${color_red}FAIL: ${label} (exit ${rc})${color_reset}" >&2
      exit $rc
    fi
  done <<< "$commands"
  return 0
}

# Record which commit is being verified before anything runs, so files the
# run itself writes cannot mark the tree dirty.
verified_sha=""
if git -C "$ROOT" rev-parse --verify -q HEAD >/dev/null 2>&1; then
  verified_sha=$(git -C "$ROOT" rev-parse HEAD)
  if [ -n "$(git -C "$ROOT" status --porcelain -- . \
        ':(exclude).cadence/.last_verify_ok' \
        ':(exclude).cadence/.last_verify_sha' \
        ':(exclude).cadence/last_verify.log' 2>/dev/null)" ]; then
    verified_sha="${verified_sha}-dirty"
  fi
fi

# Runs in a subshell (it is piped to tee below), so `exit` in a step ends
# only this function and its status reaches PIPESTATUS.
main() {
  cd "$ROOT"

  run_step "format" "$(read_commands format)"
  run_step "lint"   "$(read_commands lint)"

  # Boundary check is built-in; not configured per project.
  if [ -f "$ROOT/tool/check_boundaries.py" ]; then
    echo
    echo -e "${color_cyan}==> boundaries${color_reset}"
    echo -e "${color_dim}\$ python tool/check_boundaries.py${color_reset}"
    "$PY" tool/check_boundaries.py --config "$CONFIG"
    rc=$?
    if [ $rc -ne 0 ]; then
      echo -e "${color_red}FAIL: boundaries (exit ${rc})${color_reset}" >&2
      exit $rc
    fi
  else
    echo
    echo -e "${color_dim}(tool/check_boundaries.py not found; skipping boundary check)${color_reset}"
  fi

  run_step "test" "$(read_commands test)"

  elapsed=$(( $(date +%s) - start_ts ))
  echo
  echo -e "${color_green}OK${color_reset} (${elapsed}s)"
  echo
  echo "Definition of Done (mechanical, auto-enforced):"
  echo "  ✓ format"
  echo "  ✓ lint"
  echo "  ✓ boundaries"
  echo "  ✓ test"
  echo
  echo "Manual DoD lines (Reviewer responsibility) — see docs/DEFINITION_OF_DONE.md."
}

mkdir -p "$EVIDENCE_DIR"
raw_log=$(mktemp)
main 2>&1 | tee "$raw_log"
rc=${PIPESTATUS[0]}

# The log is evidence for an auditor: strip terminal colour codes.
esc=$(printf '\033')
sed "s/${esc}\[[0-9;]*m//g" "$raw_log" > "$LOG_FILE"
rm -f "$raw_log"

if [ "$rc" -eq 0 ]; then
  echo "ok $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$OK_MARKER"
  if [ -n "$verified_sha" ]; then
    echo "$verified_sha" > "$SHA_MARKER"
  fi
fi
exit "$rc"
