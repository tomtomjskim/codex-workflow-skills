#!/usr/bin/env python3
"""Bounded exhaustive check of validate_handoff path ownership.

P1 (no false accept): if validate_handoff accepts a list of changed paths,
every path's real filesystem location lies inside the real location of one
of the workstream's owned paths.
P2 (no hiding): validate_handoff accepts a list exactly when it accepts each
path alone, so adding a path can never turn a rejection into an acceptance.

The check runs the real function on every owned set below, every single
changed path in ALPHABET, and every unordered pair of them, inside one fixed
repository layout. Then it runs the same check on known-broken variants of
validate.py, built in memory, and fails unless each variant is caught.
"""

import argparse
import importlib.util
import itertools
import os
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[4]
VALIDATE_PY = REPO_DIR / "scripts" / "workflow_coordination" / "validate.py"
sys.path.insert(0, str(REPO_DIR))

from scripts.workflow_coordination.receipts import ValidationReceipt  # noqa: E402

WORKSTREAM = "frontend"

OWNED_SETS = (
    ("src/ui",),
    ("src/ui", "docs"),
    ("src/ui/sub",),
    ("src/new",),
    ("src/ui/link",),
)

ALPHABET = (
    "src/ui",
    "src/ui/",
    "src/ui/a.py",
    "./src/ui/a.py",
    "src//ui/a.py",
    "src/ui/./a.py",
    "src/ui/sub/a.py",
    "src/ui/link",
    "src/ui/link/a.py",
    "src/ui/esc",
    "src/ui/esc/a.py",
    "src/ui/self/a.py",
    "src/uix/a.py",
    "src/u",
    "src",
    "src/api/a.py",
    "src/new",
    "src/new/a.py",
    "docs/a.md",
    "SRC/ui/a.py",
    "src/UI/a.py",
    "src/ui/../api/a.py",
    "/etc/passwd",
    "C:/x.py",
    "src\\ui\\a.py",
    "src/ui/*.py",
    "src/ui/e\u0301.py",
    "src/ui/\u00e9.py",
    ".",
    "",
)

MUTANTS = {
    "string-prefix-ownership": (
        "normalized == owned or owned in normalized.parents",
        "str(normalized).startswith(str(owned))",
    ),
    "skip-symlink-resolution": (
        "    return canonical\n",
        "    return normalized\n",
    ),
}


def build_layout(base):
    """Create the fixed repository layout; return (repo_root, outside_dir)."""
    repo = base / "repo"
    outside = base / "outside"
    for directory in ("src/ui/sub", "src/api", "src/uix", "docs"):
        (repo / directory).mkdir(parents=True)
    outside.mkdir()
    (repo / "src/ui/a.py").write_text("ui\n")
    (repo / "src/ui/link").symlink_to("../api")
    (repo / "src/ui/esc").symlink_to(outside)
    (repo / "src/ui/self").symlink_to(".")
    return repo.resolve(), outside.resolve()


def really_inside(repo, changed, owned_paths):
    """Independent oracle based on os.path.realpath, not on validate.py."""
    real_changed = os.path.realpath(os.path.join(str(repo), changed))
    for owned in owned_paths:
        real_owned = os.path.realpath(os.path.join(str(repo), owned))
        if os.path.commonpath([real_changed, real_owned]) == real_owned:
            return True
    return False


def load_module(mutant=None):
    source = VALIDATE_PY.read_text(encoding="utf-8")
    name = "scripts.workflow_coordination.validate"
    if mutant is not None:
        old, new = MUTANTS[mutant]
        if source.count(old) != 1:
            raise SystemExit(
                "mutant {!r} no longer applies to validate.py; update MUTANTS".format(mutant)
            )
        source = source.replace(old, new)
        name = "scripts.workflow_coordination._mutant_" + mutant.replace("-", "_")
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "scripts.workflow_coordination"
    module.__file__ = str(VALIDATE_PY)
    exec(compile(source, str(VALIDATE_PY), "exec"), module.__dict__)
    return module


def receipt_for(owned):
    return ValidationReceipt(
        schema_version=1,
        manifest_hash="sha256:" + "0" * 64,
        inventory_hash="sha256:" + "0" * 64,
        contract_core_hash=None,
        checkout_tree_hash="0" * 40,
        derived_route="independent",
        required_sets={},
        normalized_paths={WORKSTREAM: list(owned)},
        run_id="00000000-0000-4000-8000-000000000000",
        recorded_at="2026-01-01T00:00:00Z",
    )


def accepts(module, repo, receipt, changed):
    try:
        module.validate_handoff(repo, receipt, WORKSTREAM, list(changed))
    except module.ValidationError:
        return False
    return True


def check(module, repo):
    """Return (cases, violations); a violation is (property, owned, changed)."""
    violations = []
    cases = 0
    for owned in OWNED_SETS:
        receipt = receipt_for(owned)
        single = {}
        for changed in ALPHABET:
            cases += 1
            ok = accepts(module, repo, receipt, [changed])
            single[changed] = ok
            if ok and not really_inside(repo, changed, owned):
                violations.append(("P1", owned, (changed,)))
        for pair in itertools.combinations(ALPHABET, 2):
            cases += 1
            ok = accepts(module, repo, receipt, list(pair))
            if ok != (single[pair[0]] and single[pair[1]]):
                violations.append(("P2", owned, pair))
            if ok and not all(really_inside(repo, c, owned) for c in pair):
                violations.append(("P1", owned, pair))
    return cases, violations


def describe(violation, repo):
    prop, owned, changed = violation
    if prop == "P1":
        bad = [
            c for c in changed if not really_inside(repo, c, owned)
        ]
        return (
            "P1 broken. Workstream {!r} owns {}. validate_handoff accepted changed "
            "path(s) {}. On disk {} really points to {}, which is outside every owned path."
        ).format(
            WORKSTREAM,
            list(owned),
            list(changed),
            bad[0],
            os.path.relpath(os.path.realpath(os.path.join(str(repo), bad[0])), str(repo)),
        )
    return (
        "P2 broken. Workstream {!r} owns {}. Accepting {} together differs from "
        "accepting each path alone."
    ).format(WORKSTREAM, list(owned), list(changed))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mutant", choices=sorted(MUTANTS), help="check one broken variant only")
    parser.add_argument("--max-report", type=int, default=5)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="verify-handoff-ownership-") as tmp:
        repo, _outside = build_layout(Path(tmp))
        if args.mutant:
            cases, violations = check(load_module(args.mutant), repo)
            print("mutant {}: {} cases, {} violations".format(args.mutant, cases, len(violations)))
            for violation in violations[: args.max_report]:
                print("  " + describe(violation, repo))
            return 1 if violations else 0

        cases, violations = check(load_module(), repo)
        print("real code: {} cases, {} violations".format(cases, len(violations)))
        for violation in violations[: args.max_report]:
            print("  " + describe(violation, repo))
        survivors = []
        for mutant in sorted(MUTANTS):
            _cases, caught = check(load_module(mutant), repo)
            print("mutant {}: {}".format(mutant, "caught" if caught else "SURVIVED"))
            if not caught:
                survivors.append(mutant)
        if violations:
            print("RESULT: FINDING. The current code breaks the property.")
            return 1
        if survivors:
            print("RESULT: CHECK TOO WEAK. Mutants survived: {}".format(survivors))
            return 2
        print("RESULT: PASS within the stated bounds.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
