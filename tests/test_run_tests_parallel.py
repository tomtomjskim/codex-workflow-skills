import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tests.script_loading import load_script

ROOT = Path(__file__).resolve().parents[1]
RUNNER = load_script("run_tests_parallel_under_test", ROOT / "scripts" / "run_tests_parallel.py")

PROBE = textwrap.dedent(
    """
    import os
    import unittest


    class Passes(unittest.TestCase):
        def test_one(self):
            pass

        def test_two(self):
            pass


    class SkipsItself(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            raise unittest.SkipTest("not configured")

        def test_hidden(self):
            pass


    class Fails(unittest.TestCase):
        def test_fails(self):
            if os.environ.get("PROBE_FAIL") == "1":
                self.fail("probe failure")
    """
)

EXITS = textwrap.dedent(
    """
    import os
    import unittest


    class ExitsEarly(unittest.TestCase):
        def test_a_exits(self):
            os._exit(0)

        def test_b_never_runs(self):
            pass
    """
)

PROBE_CLASSES = {
    "probe_module.Passes": ["probe_module.Passes.test_one", "probe_module.Passes.test_two"],
    "probe_module.SkipsItself": ["probe_module.SkipsItself.test_hidden"],
    "probe_module.Fails": ["probe_module.Fails.test_fails"],
}


class RunTestsParallelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        (path / "probe_module.py").write_text(PROBE, encoding="utf-8")
        (path / "exits_module.py").write_text(EXITS, encoding="utf-8")
        environment = mock.patch.dict(
            os.environ, {"PYTHONPATH": directory.name, "PYTHONDONTWRITEBYTECODE": "1"}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def run_module(self, name):
        module, code, _output, report, _seconds = RUNNER.run_group(name)
        return RUNNER.evaluate(module, PROBE_CLASSES, code, report), report

    def test_a_class_skipped_in_setupclass_is_not_counted_as_missing(self):
        (ran, expected, failure), report = self.run_module("probe_module")

        self.assertEqual(["probe_module.SkipsItself"], report["skipped_classes"])
        self.assertEqual((3, 3, None), (ran, expected, failure))

    def test_a_failing_test_fails_the_module(self):
        with mock.patch.dict(os.environ, {"PROBE_FAIL": "1"}):
            (_ran, _expected, failure), _report = self.run_module("probe_module")

        self.assertEqual("probe_module failed", failure)

    def test_a_module_that_exits_without_a_result_fails(self):
        module, code, _output, report, _seconds = RUNNER.run_group("exits_module")
        _ran, _expected, failure = RUNNER.evaluate(module, {}, code, report)

        self.assertIsNone(report)
        self.assertEqual("exits_module exited 0 without a result", failure)

    def test_fewer_tests_than_discovered_fails(self):
        report = {"ran": 2, "ok": True, "skipped_classes": []}

        _ran, expected, failure = RUNNER.evaluate("probe_module", PROBE_CLASSES, 0, report)

        self.assertEqual(4, expected)
        self.assertEqual("probe_module ran 2 tests, discovery found 4", failure)


if __name__ == "__main__":
    unittest.main()
