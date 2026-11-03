#!/usr/bin/env python3
"""Model-check the prepare-coordination atomic publish and tie the model to the code.

1. TLC checks model/AtomicPublish.tla with Variant = "real". Every invariant
   must hold in every interleaving of three preparers, one outside creator,
   write failures, and crashes.
2. TLC checks each broken variant. Each must violate the invariant it breaks,
   or the model is too weak to trust.
3. The real _publish_prepared_output in scripts/workflow runs against every
   outside target shape the model uses, and plain rename(2) is probed for the
   behavior the check_then_rename variant assumes. This is what catches a code
   change that the hand-written model would not see.
"""

import argparse
import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SKILL_DIR.parents[2]
MODEL_DIR = SKILL_DIR / "model"
WORKFLOW = REPO_DIR / "scripts" / "workflow"
DEFAULT_JAR = Path.home() / ".local" / "share" / "tlaplus" / "tla2tools-1.7.4.jar"

BROKEN_VARIANTS = {
    "check_then_rename": {"NoReplace", "ExternalPreserved"},
    "publish_then_write": {"Complete"},
    "no_cleanup": {"NoLeftover"},
}


def run_tlc(jar, variant, work):
    run_dir = work / variant
    run_dir.mkdir()
    shutil.copy(str(MODEL_DIR / "AtomicPublish.tla"), str(run_dir))
    cfg = (MODEL_DIR / "AtomicPublish.cfg").read_text(encoding="utf-8")
    cfg = cfg.replace('Variant = "real"', 'Variant = "{}"'.format(variant))
    (run_dir / "AtomicPublish.cfg").write_text(cfg, encoding="utf-8")
    result = subprocess.run(
        [
            "java", "-cp", str(jar), "tlc2.TLC",
            "-deadlock", "-workers", "auto",
            "-metadir", str(run_dir / "states"),
            "-config", "AtomicPublish.cfg", "AtomicPublish.tla",
        ],
        cwd=str(run_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    (run_dir / "tlc.out").write_text(result.stdout, encoding="utf-8")
    violated = set(re.findall(r"Invariant (\w+) is violated", result.stdout))
    passed = "Model checking completed. No error has been found." in result.stdout
    states = re.search(r"(\d+) distinct states found", result.stdout)
    return passed, violated, states.group(1) if states else "?", run_dir / "tlc.out"


def load_workflow():
    loader = importlib.machinery.SourceFileLoader("workflow_atomic_publish_check", str(WORKFLOW))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def conformance(work):
    """Return a list of failures, each a plain sentence."""
    workflow = load_workflow()
    failures = []
    parent = work / "conformance"
    parent.mkdir()
    elsewhere = parent / "elsewhere"
    elsewhere.mkdir()

    def make_empty(path):
        path.mkdir()

    def make_marker(path):
        path.mkdir()
        (path / "marker").write_text("keep", encoding="utf-8")

    def make_file(path):
        path.write_text("keep", encoding="utf-8")

    def make_link(path):
        path.symlink_to(elsewhere, target_is_directory=True)

    def snapshot(path):
        if path.is_symlink():
            return ("symlink", os.readlink(str(path)))
        if path.is_dir():
            return ("dir", sorted((p.name, p.read_bytes()) for p in path.iterdir()))
        return ("file", path.read_bytes())

    for name, make in (
        ("empty-dir", make_empty),
        ("dir-with-marker", make_marker),
        ("file", make_file),
        ("symlink", make_link),
    ):
        target = parent / name
        make(target)
        before = snapshot(target)
        try:
            workflow._publish_prepared_output(target, b"{}\n", b"{}\n")
        except workflow.ValidationError as error:
            if "must not already exist" not in str(error):
                failures.append("Outside {} was refused with an unexpected message: {}.".format(name, error))
        except Exception as error:
            failures.append(
                "Publishing over an outside {} raised {!r} instead of the "
                "'must not already exist' refusal.".format(name, error)
            )
        else:
            failures.append(
                "_publish_prepared_output replaced an outside {} at {}. The model's "
                "PublishNoReplace step assumes this cannot happen.".format(name, target.name)
            )
        if not os.path.lexists(str(target)) or snapshot(target) != before:
            failures.append("The outside {} changed after a refused publish.".format(name))
        leftovers = list(parent.glob(".{}.*".format(name)))
        if leftovers:
            failures.append("A refused publish to {} left staged siblings {}.".format(name, leftovers))

    fresh = parent / "fresh"
    workflow._publish_prepared_output(fresh, b'{"m":1}\n', b'{"i":1}\n')
    if not (
        (fresh / "manifest.json").read_bytes() == b'{"m":1}\n'
        and (fresh / "inventory.json").read_bytes() == b'{"i":1}\n'
    ):
        failures.append("A successful publish did not hold both files with the given bytes.")

    failures.extend(replay_check_then_rename(workflow, parent))
    return failures


def replay_check_then_rename(workflow, parent):
    """Replay the check_then_rename counterexample through the real function.

    Between the run seeing the path free and its rename, another process
    creates an empty folder. With a plain rename that folder must be replaced,
    which shows the broken variant's harm is real on this filesystem. With the
    real no-replace rename the publish must be refused and the folder kept.
    """
    failures = []

    def racing(rename):
        def publish(staged, destination):
            free = not os.path.lexists(str(destination))
            destination.mkdir()
            if rename is None:
                if not free:
                    raise FileExistsError(str(destination))
                os.rename(str(staged), str(destination))
            else:
                rename(staged, destination)
        return publish

    for label, rename, must_replace in (
        ("plain rename", None, True),
        ("real no-replace rename", workflow._rename_directory_noreplace, False),
    ):
        target = parent / "replay-{}".format(label.replace(" ", "-"))
        try:
            workflow._publish_prepared_output(target, b"{}\n", b"{}\n", publish=racing(rename))
            succeeded = True
        except workflow.ValidationError:
            succeeded = False
        except Exception as error:
            failures.append("Replay with {} raised {!r}.".format(label, error))
            continue
        replaced = succeeded and sorted(p.name for p in target.iterdir()) == ["inventory.json", "manifest.json"]
        if must_replace and not replaced:
            failures.append(
                "Replay with plain rename did not replace the other process's empty "
                "folder, so the check_then_rename variant models the wrong filesystem."
            )
        if not must_replace and (succeeded or list(target.iterdir())):
            failures.append(
                "Replay with the real no-replace rename replaced or changed the other "
                "process's empty folder created after the run saw the path free."
            )
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jar", type=Path, default=Path(os.environ.get("TLA2TOOLS_JAR", str(DEFAULT_JAR))))
    parser.add_argument("--keep", action="store_true", help="keep TLC output and traces")
    args = parser.parse_args()
    if not args.jar.is_file():
        print("tla2tools.jar not found at {}. See SKILL.md, Run it.".format(args.jar))
        return 3

    work = Path(tempfile.mkdtemp(prefix="verify-atomic-publish-"))
    try:
        passed, violated, states, out = run_tlc(args.jar, "real", work)
        verdict = "pass" if passed else ("violated " + ", ".join(sorted(violated)) if violated else "TLC error")
        print("model real: {} distinct states, {}".format(states, verdict))
        weak = []
        for variant, expected in sorted(BROKEN_VARIANTS.items()):
            v_passed, v_violated, _states, _out = run_tlc(args.jar, variant, work)
            caught = bool(v_violated & expected)
            print("variant {}: {}".format(variant, "caught by " + ", ".join(sorted(v_violated)) if caught else "NOT CAUGHT"))
            if not caught:
                weak.append(variant)
        failures = conformance(work)
        print("real code conformance: {}".format("pass" if not failures else "{} failure(s)".format(len(failures))))
        for failure in failures:
            print("  " + failure)

        if not passed:
            print("RESULT: FINDING. The model of the current design breaks an invariant. Trace in {}".format(out))
            args.keep = True
            return 1
        if failures:
            print("RESULT: FINDING. The real code no longer matches the model's assumptions.")
            return 1
        if weak:
            print("RESULT: CHECK TOO WEAK. Broken variants not caught: {}".format(weak))
            args.keep = True
            return 2
        print("RESULT: PASS within the stated bounds.")
        return 0
    finally:
        if args.keep:
            print("TLC output kept in {}".format(work))
        else:
            shutil.rmtree(str(work), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
