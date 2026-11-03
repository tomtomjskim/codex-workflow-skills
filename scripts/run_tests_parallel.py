#!/usr/bin/env python3
"""Run every repository-owned test with unittest discovery, in parallel by module.

Discovery is the same as `python3 -m unittest discover -s tests`. Each test
module runs in its own Python process, and the run fails if any module fails or
runs a different number of tests than discovery found for it. A class that
skips itself in setUpClass counts as zero tests, as in unittest.
VALIDATE_TEST_JOBS sets the number of parallel processes; 1 runs the modules one
after another.
"""

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JOBS = 4
RESULT_MARKER = "@@run_tests_parallel "
CLASS_HOOK = re.compile(r"^setUpClass \(([\w.]+)\)$")


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def discover():
    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=str(ROOT))
    if loader.errors:
        return None, loader.errors
    groups = {}
    for test in iter_tests(suite):
        cls = type(test)
        module = groups.setdefault(cls.__module__, {})
        module.setdefault("{}.{}".format(cls.__module__, cls.__qualname__), []).append(test.id())
    return groups, []


def run_child(module):
    sys.path[0] = str(ROOT)
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    skipped_classes = []
    for holder, _reason in result.skipped:
        if not isinstance(holder, unittest.TestCase):
            match = CLASS_HOOK.match(holder.id())
            if match:
                skipped_classes.append(match.group(1))
    sys.stdout.flush()
    print(RESULT_MARKER + json.dumps({
        "ran": result.testsRun,
        "ok": result.wasSuccessful(),
        "skipped_classes": skipped_classes,
    }))
    return 0 if result.wasSuccessful() else 1


def run_group(name):
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", name],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    report = None
    output = []
    for line in completed.stdout.splitlines(True):
        if line.startswith(RESULT_MARKER):
            report = json.loads(line[len(RESULT_MARKER):])
        else:
            output.append(line)
    return name, completed.returncode, "".join(output), report, time.monotonic() - started


def evaluate(name, classes, code, report):
    """Return (ran, expected, failure) for one module; failure is None when it passed."""
    if report is None:
        return 0, 0, "{} exited {} without a result".format(name, code)
    skipped = set(report["skipped_classes"])
    expected = sum(len(ids) for cls, ids in classes.items() if cls not in skipped)
    if code != 0 or not report["ok"]:
        return report["ran"], expected, "{} failed".format(name)
    if report["ran"] != expected:
        return report["ran"], expected, "{} ran {} tests, discovery found {}".format(
            name, report["ran"], expected)
    return report["ran"], expected, None


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        return run_child(sys.argv[2])
    jobs = int(os.environ.get("VALIDATE_TEST_JOBS", DEFAULT_JOBS))
    if jobs < 1:
        print("error: VALIDATE_TEST_JOBS must be at least 1", file=sys.stderr)
        return 2
    started = time.monotonic()
    groups, errors = discover()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print("FAILED: test discovery reported {} error(s)".format(len(errors)))
        return 1
    discovered = sum(len(ids) for classes in groups.values() for ids in classes.values())
    order = sorted(groups, key=lambda name: (-sum(map(len, groups[name].values())), name))
    print("discovered {} tests in {} modules; running with {} process(es)".format(
        discovered, len(groups), jobs))
    sys.stdout.flush()

    failed = []
    total = 0
    expected = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for name, code, output, report, seconds in pool.map(run_group, order):
            sys.stdout.write(output)
            ran, want, failure = evaluate(name, groups[name], code, report)
            if failure:
                failed.append(failure)
            total += ran
            expected += want
            print("--- {} finished in {:.1f}s".format(name, seconds))
            sys.stdout.flush()

    print("=" * 70)
    print("Ran {} of {} expected tests ({} discovered) in {:.1f}s across {} modules".format(
        total, expected, discovered, time.monotonic() - started, len(groups)))
    if failed:
        for failure in failed:
            print("FAILED: " + failure)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
