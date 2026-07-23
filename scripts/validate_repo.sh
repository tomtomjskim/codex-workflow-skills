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
require_file skills/resume-multi-review/SKILL.md
require_file skills/resume-multi-review/references/source-precedence.md
require_file skills/resume-multi-review/references/review-contract.md
require_file skills/resume-multi-review/references/prompt-template.md
require_file docs/sample-workflow-intake.md
require_file docs/sample-adversarial-review.md
require_file docs/sample-resume-multi-review.md
require_file tests/acceptance-scenarios.md
require_file scripts/workflow
require_file scripts/install_agent_adapters.py
require_file scripts/validate_policy_contracts.py
require_file scripts/workflow_coordination/canonical_json.py
require_file scripts/workflow_coordination/reviewer_routing.py
require_file tests/test_canonical_json.py
require_file tests/test_policy_contracts.py
require_file tests/test_git_changes.py
require_file tests/test_workflow_cli.py
require_file tests/test_install_agent_adapters.py
require_file skills/workflow-intake/references/parallel-coordination.md
require_file skills/adversarial-review-loop/references/reviewer-routing.json
require_file scripts/run_live_eval.py
require_file scripts/live_eval/harness.py
require_file tests/live-eval-scenarios.json
require_file tests/test_live_eval_harness.py
require_file tests/test_live_eval_runner.py

if [ ! -x scripts/workflow ]; then
  printf 'error: workflow CLI is not executable: scripts/workflow\n' >&2
  exit 1
fi

if [ -f "$SKILL_VALIDATOR" ]; then
  run python3 "$SKILL_VALIDATOR" skills/workflow
  run python3 "$SKILL_VALIDATOR" skills/workflow-intake
  run python3 "$SKILL_VALIDATOR" skills/adversarial-review-loop
  run python3 "$SKILL_VALIDATOR" skills/resume-multi-review
else
  printf 'skip: skill validator not found at %s\n' "$SKILL_VALIDATOR"
fi

if [ -f "$PLUGIN_VALIDATOR" ]; then
  run python3 "$PLUGIN_VALIDATOR" .
else
  printf 'skip: plugin validator not found at %s\n' "$PLUGIN_VALIDATOR"
fi

run git diff --check
run python3 scripts/validate_policy_contracts.py --repo-root .
run python3 -m unittest tests.test_policy_contracts -v
run python3 -m unittest tests.test_git_changes -v
if [ -z "${SHARED_AGENTS_ROOT:-}" ]; then
  printf 'not_run: external shared-agent audit requires SHARED_AGENTS_ROOT\n'
fi
# Full discovery includes tests.test_workflow_cli and every other repository-owned test.
run python3 -m unittest discover -s tests -v

require_match '\[sample-workflow-intake\.md\]\(docs/sample-workflow-intake\.md\)' README.md 'workflow intake sample link'
require_match '\[sample-adversarial-review\.md\]\(docs/sample-adversarial-review\.md\)' README.md 'adversarial review sample link'
require_match '\[sample-resume-multi-review\.md\]\(docs/sample-resume-multi-review\.md\)' README.md 'resume multi-review sample link'
require_match '\[CHANGELOG\.md\]\(CHANGELOG\.md\)' README.md 'changelog link'
require_match 'name: resume-multi-review' skills/resume-multi-review/SKILL.md 'resume multi-review skill name'
require_match 'source_gap' skills/resume-multi-review/references/review-contract.md 'resume source-gap output state'

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
