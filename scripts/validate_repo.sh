#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKILL_VALIDATOR="${SKILL_VALIDATOR:-$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py}"
PLUGIN_VALIDATOR="${PLUGIN_VALIDATOR:-$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py}"

if ! command -v rg >/dev/null 2>&1; then
  printf 'error: ripgrep (rg) is required for repository validation\n' >&2
  exit 1
fi

run() {
  printf '==> %s\n' "$*"
  "$@"
}

require_file() {
  if [ ! -f "$1" ]; then
    printf 'error: required file missing: %s\n' "$1" >&2
    exit 1
  fi
}

require_match() {
  local pattern="$1"
  local file="$2"
  local label="$3"

  if ! rg -q "$pattern" "$file"; then
    printf 'error: missing %s in %s\n' "$label" "$file" >&2
    exit 1
  fi
}

require_file README.md
require_file CHANGELOG.md
require_file .codex-plugin/plugin.json
require_file skills/workflow/SKILL.md
require_file skills/workflow-intake/SKILL.md
require_file skills/adversarial-review-loop/SKILL.md
require_file docs/sample-workflow-intake.md
require_file docs/sample-adversarial-review.md
require_file tests/acceptance-scenarios.md

if [ -f "$SKILL_VALIDATOR" ]; then
  run python3 "$SKILL_VALIDATOR" skills/workflow
  run python3 "$SKILL_VALIDATOR" skills/workflow-intake
  run python3 "$SKILL_VALIDATOR" skills/adversarial-review-loop
else
  printf 'skip: skill validator not found at %s\n' "$SKILL_VALIDATOR"
fi

if [ -f "$PLUGIN_VALIDATOR" ]; then
  run python3 "$PLUGIN_VALIDATOR" .
else
  printf 'skip: plugin validator not found at %s\n' "$PLUGIN_VALIDATOR"
fi

run git diff --check

require_match '\[sample-workflow-intake\.md\]\(docs/sample-workflow-intake\.md\)' README.md 'workflow intake sample link'
require_match '\[sample-adversarial-review\.md\]\(docs/sample-adversarial-review\.md\)' README.md 'adversarial review sample link'
require_match '\[CHANGELOG\.md\]\(CHANGELOG\.md\)' README.md 'changelog link'

manifest_version="$(
  python3 - <<'PY'
import json
with open(".codex-plugin/plugin.json", encoding="utf-8") as f:
    print(json.load(f)["version"])
PY
)"

require_match "^## \\[$manifest_version\\]" CHANGELOG.md "changelog entry for version $manifest_version"

local_path_pattern='/U''sers/'
tbd_pattern='TB''D'
todo_pattern='TO''DO'
fixme_pattern='FIX''ME'
placeholder_pattern='PLACE''HOLDER'
public_hygiene_pattern="gh[opsu]_[A-Za-z0-9_]+|sk-[A-Za-z0-9]{20,}|${local_path_pattern}|BEGIN (RSA|OPENSSH|PRIVATE)|${tbd_pattern}|${todo_pattern}|${fixme_pattern}|${placeholder_pattern}|\\?\\?\\?"

if rg -n "$public_hygiene_pattern" README.md CHANGELOG.md docs skills tests .codex-plugin scripts; then
  printf 'error: public hygiene scan matched sensitive or placeholder pattern\n' >&2
  exit 1
fi

printf 'ok: repository validation passed\n'
