#!/usr/bin/env python3
"""Drive scripts/workflow end to end in a disposable Git repository."""

import argparse
import importlib.machinery
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SKILL_DIR.parents[2]
CLI = REPO_DIR / "scripts" / "workflow"
PLAN = REPO_DIR / "tests" / "fixtures" / "coordination" / "approved-plan.json"
DEFAULT_BASE = Path(os.environ.get("VERIFY_WORKFLOW_BASE", tempfile.gettempdir()))
RUN_MARKER = ".verify-workflow-cli-run"


def run_paths(base, run_id):
    run_dir = base / "verify-workflow-cli" / run_id
    return {
        "run": run_dir,
        "repo": run_dir / "repo",
        "coord": run_dir / "coordination",
        "evidence": base / "verify-workflow-cli-evidence" / run_id,
    }


def record(paths, step, command, result):
    paths["evidence"].mkdir(parents=True, exist_ok=True)
    entry = {
        "step": step,
        "command": [str(part) for part in command],
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    (paths["evidence"] / "{}.json".format(step)).write_text(
        json.dumps(entry, indent=2), encoding="utf-8"
    )
    print("{}: exit {}".format(step, result.returncode))
    return result


def run(paths, step, command, cwd=None):
    result = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    return record(paths, step, command, result)


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=verify", "-c", "user.email=verify@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def doctor(_paths):
    problems = []
    if not os.access(CLI, os.X_OK):
        problems.append("scripts/workflow is missing or not executable")
    if not PLAN.is_file():
        problems.append("fixture plan is missing")
    version = subprocess.run(
        [str(CLI), "--version"], text=True, capture_output=True, check=False
    )
    print("cli: {}".format(CLI.relative_to(REPO_DIR)))
    print("cli --version: {}".format((version.stdout or version.stderr).strip()))
    print("repo HEAD: {}".format(git(REPO_DIR, "rev-parse", "--short", "HEAD")))
    print("python: {}".format(sys.version.split()[0]))
    if version.returncode != 0:
        problems.append("scripts/workflow --version failed")
    for problem in problems:
        print("doctor: FAIL {}".format(problem))
    if not problems:
        print("doctor: OK")
    return 1 if problems else 0


def launch(paths):
    if paths["run"].exists():
        print("launch: run directory already exists, pick a new --run-id", file=sys.stderr)
        return 1
    (paths["repo"] / "src" / "ui").mkdir(parents=True)
    (paths["repo"] / "src" / "api").mkdir(parents=True)
    (paths["run"] / RUN_MARKER).write_text("owned by verify-workflow-cli\n", encoding="utf-8")
    git(paths["repo"], "init", "-q")
    git(paths["repo"], "commit", "--allow-empty", "-qm", "verify fixture")
    print("launch: ready run={} repo={}".format(paths["run"], paths["repo"]))
    return 0


def prepare(paths):
    result = run(
        paths,
        "prepare",
        [CLI, "prepare-coordination", "--repo-root", paths["repo"], "--plan", PLAN,
         "--out-dir", paths["coord"], "--json"],
    )
    return result.returncode


def _load_cli_module():
    return importlib.machinery.SourceFileLoader("verify_workflow_cli_module", str(CLI)).load_module()


def write_contract(paths):
    sys.path.insert(0, str(REPO_DIR))
    from scripts.workflow_coordination.canonical_json import sha256_id
    from scripts.workflow_coordination.derive import derive_coordination

    module = _load_cli_module()
    prepared = json.loads(json.loads((paths["evidence"] / "prepare.json").read_text())["stdout"])
    routing = module.load_reviewer_routing()
    derived = derive_coordination(
        prepared["manifest"],
        prepared["inventory"],
        module.build_trigger_matrix(routing),
        normalized_paths={
            ws["id"]: ws["exclusive_write_paths"] for ws in prepared["manifest"]["workstreams"]
        },
    )
    profile = {
        "shared_interface": derived.profiles.shared_interface,
        "path_overlap": derived.profiles.path_overlap,
        "integration_dependency": derived.profiles.integration_dependency,
    }
    core = {
        "schema_version": 1,
        "manifest_hash": prepared["manifest_hash"],
        "inventory_hash": prepared["inventory_hash"],
        "revision": 1,
        "parent_contract_core_hash": None,
        "contract_owner": "architect",
        "integration_owner": "integration-owner",
        "derived_profile": profile,
        "extension_requirements": {
            "interface_contract": {"status": "approved"} if profile["shared_interface"] else {},
            "path_ownership": {"status": "approved"} if profile["path_overlap"] else {},
            "integration": {"status": "approved"} if profile["integration_dependency"] else {},
        },
    }
    core_hash = sha256_id(core)
    owners = {ws["id"]: ws["owner"] for ws in prepared["manifest"]["workstreams"]}
    records = [("handoff", list(edge), owners[edge[0]]) for edge in derived.required_handoffs]
    records += [("checkpoint", list(edge), owners[edge[0]]) for edge in derived.required_checkpoints]
    records += [("acknowledgement", ws, owners[ws]) for ws in derived.required_acknowledgements]
    tree_hash = git(paths["repo"], "rev-parse", "HEAD^{tree}")
    entries = []
    previous_hash = sha256_id(None)
    for index, (record_type, subject_id, producer_id) in enumerate(records):
        body = {
            "contract_core_hash": core_hash,
            "previous_entry_hash": previous_hash,
            "checkout_tree_hash": tree_hash,
            "producer_id": producer_id,
            "command_or_scenario_id": "verify-record-{}".format(index),
            "artifact_digest": "sha256:" + format(index + 1, "064x"),
            "run_id": "verify-entry-{}".format(index),
            "recorded_at": "2026-01-01T00:00:00Z",
            "record_type": record_type,
            "subject_id": subject_id,
            "status": "completed",
        }
        entry = dict(body, entry_hash=sha256_id(body))
        entries.append(entry)
        previous_hash = entry["entry_hash"]
    registry = []
    for reviewer in derived.required_reviewers:
        lens = next(name for name, agent in routing.lens_agents.items() if agent == reviewer)
        registry.append({
            "lens": lens,
            "canonical_agent": reviewer,
            "required": True,
            "contract_core_hash": core_hash,
            "status": "completed",
            "dispatch_evidence": {"run_id": "dispatch-{}".format(reviewer)},
            "completion_evidence": {"run_id": "complete-{}".format(reviewer)},
            "defer_receipt": None,
        })
    contract = {
        "contract_core": core,
        "execution_ledger": {
            "contract_core_hash": core_hash,
            "status": "frozen",
            "entries": entries,
            "ledger_hash": sha256_id([entry["entry_hash"] for entry in entries]),
            "integration_gate": {"status": "open"},
            "reviewer_registry": registry,
        },
    }
    (paths["coord"] / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
    shutil.copy(paths["coord"] / "contract.json", paths["evidence"] / "contract.json")
    print("contract: written reviewers={}".format(sorted(derived.required_reviewers)))
    return 0


def coord_args(paths):
    coord = paths["coord"]
    return ["--repo-root", paths["repo"], "--manifest", coord / "manifest.json",
            "--inventory", coord / "inventory.json", "--contract", coord / "contract.json"]


def validate(paths):
    result = run(paths, "validate", [CLI, "validate-coordination", *coord_args(paths), "--json"])
    if result.returncode == 0:
        (paths["coord"] / "receipt.json").write_text(result.stdout, encoding="utf-8")
    return result.returncode


def handoff(paths, workstream, changed_path, step):
    command = [CLI, "validate-handoff", *coord_args(paths),
               "--receipt", paths["coord"] / "receipt.json",
               "--workstream-id", workstream, "--changed-path", changed_path, "--json"]
    return run(paths, step, command).returncode


def cleanup(paths):
    if not (paths["run"] / RUN_MARKER).is_file():
        print("cleanup: no owned run at {}".format(paths["run"]))
        return 0
    shutil.rmtree(paths["run"])
    print("cleanup: removed {}".format(paths["run"]))
    print("cleanup: evidence kept at {}".format(paths["evidence"]))
    return 0


def journey(paths):
    failures = []

    def expect(label, condition):
        print("{} {}".format("PASS" if condition else "FAIL", label))
        if not condition:
            failures.append(label)

    expect("launch ready", launch(paths) == 0)
    if failures:
        return 1
    try:
        expect("prepare-coordination exit 0", prepare(paths) == 0)
        expect("manifest.json and inventory.json published",
               (paths["coord"] / "manifest.json").is_file()
               and (paths["coord"] / "inventory.json").is_file())
        run(paths, "validate_without_contract",
            [CLI, "validate-coordination", *coord_args(paths)[:6], "--json"])
        no_contract = json.loads((paths["evidence"] / "validate_without_contract.json").read_text())
        expect("validate without contract is blocked with sequential fallback",
               no_contract["exit_code"] != 0
               and json.loads(no_contract["stdout"] or no_contract["stderr"])["fallback"]["execution"] == "sequential")
        write_contract(paths)
        expect("validate-coordination exit 0", validate(paths) == 0)
        receipt = json.loads((paths["coord"] / "receipt.json").read_text())
        expect("receipt names api-reviewer and ux-reviewer",
               {"api-reviewer", "ux-reviewer"} <= set(receipt["required_sets"]["required_reviewers"]))
        expect("handoff inside frontend scope is valid",
               handoff(paths, "frontend", "src/ui/settings.py", "handoff_inside") == 0
               and json.loads(json.loads((paths["evidence"] / "handoff_inside.json").read_text())["stdout"])["status"] == "valid")
        expect("handoff outside frontend scope is rejected",
               handoff(paths, "frontend", "src/api/settings.py", "handoff_outside") != 0)
    finally:
        cleanup(paths)
    expect("evidence survives cleanup", (paths["evidence"] / "handoff_outside.json").is_file())
    print("journey: {}".format("OK" if not failures else "FAILED {}".format(failures)))
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "launch", "journey", "cleanup"])
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:12])
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    args = parser.parse_args()
    paths = run_paths(args.base.resolve(), args.run_id)
    print("run-id: {}".format(args.run_id))
    commands = {"doctor": doctor, "launch": launch, "journey": journey, "cleanup": cleanup}
    return commands[args.command](paths)


if __name__ == "__main__":
    sys.exit(main())
