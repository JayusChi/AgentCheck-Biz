"""D6 public command and persisted lifecycle checks, without model requests."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentcheck_biz.cases import CaseValidationError, load_case, load_suite, validate_case
from agentcheck_biz.cli import main
from agentcheck_biz.reports import save_json
from agentcheck_biz.runner import REPO_ROOT, default_case, run_ticket_case
from examples.ticket_agent.scripted_agent import run_scripted_client


CORE = REPO_ROOT / "cases" / "tickets" / "core"


class CaseAndCLITests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(list(map(str, args)))
        return code, json.loads(output.getvalue())

    def test_all_core_cases_and_legacy_case_validate(self):
        self.assertEqual(len(load_suite(CORE)), 4)
        self.assertEqual(validate_case(default_case())["schema_version"], 1)

    def test_invalid_shape_unknown_fields_versions_and_limits_are_rejected(self):
        mutations = [
            lambda c: c.update(schema_version=2),
            lambda c: c.update(arbitrary_sql="DELETE FROM tickets"),
            lambda c: c["limits"].update(max_tool_calls=True),
            lambda c: c["limits"].update(max_tool_calls=0),
            lambda c: c["limits"].update(unknown_timeout=1),
            lambda c: c["context"].update(tenant_id=" "),
            lambda c: c["fault"].update(max_injections=2),
            lambda c: c["initial_tickets"].append(deepcopy(c["initial_tickets"][0])),
        ]
        for mutation in mutations:
            case = default_case()
            mutation(case)
            with self.subTest(case=case), self.assertRaises(CaseValidationError):
                validate_case(case)

    def test_duplicate_json_keys_and_non_finite_numbers_rejected(self):
        for content in ('{"schema_version":1,"schema_version":2}', '{"x":NaN}'):
            path = self.root / "bad.json"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(CaseValidationError):
                load_case(path)

    def test_permission_case_cannot_declare_success_without_a_ticket(self):
        case = load_case(CORE / "T07.json")
        case["expected"]["client_status"] = "completed"
        with self.assertRaises(CaseValidationError):
            validate_case(case)

    def test_invalid_suite_validates_all_cases_before_creating_any_runs(self):
        source = self.root / "cases"
        source.mkdir()
        save_json(source / "a.json", default_case())
        (source / "b.json").write_text("{}", encoding="utf-8")
        target = self.root / "output"
        with patch("agentcheck_biz.cli.run_ticket_case") as runner:
            code, output = self.cli("suite", "--cases", source, "--output", target)
            runner.assert_not_called()
        self.assertEqual((code, output["status"]), (3, "ERROR"))
        self.assertFalse(target.exists())

    def test_empty_and_duplicate_id_suites_are_errors(self):
        with self.assertRaises(CaseValidationError):
            load_suite(self.root)
        save_json(self.root / "a.json", default_case())
        save_json(self.root / "b.json", default_case())
        with self.assertRaises(CaseValidationError):
            load_suite(self.root)

    def test_unknown_options_and_model_for_scripted_are_explicit_errors(self):
        for args in (("run", "--case", CORE / "T01.json", "--no-judge"),
                     ("run", "--case", CORE / "T01.json", "--model", "unused"),
                     ("suite", "--cases", CORE, "--agent", "llm")):
            with self.subTest(args=args):
                code, output = self.cli(*args)
                self.assertEqual((code, output["status"]), (3, "ERROR"))

    def test_real_process_exit_codes_cover_all_four_verdicts(self):
        uncovered = default_case()
        uncovered["fault"]["occurrence"] = 99
        uncovered_path = self.root / "uncovered.json"
        save_json(uncovered_path, uncovered)
        scenarios = [(CORE / "T07.json", "fixed", 0, "PASS"),
                     (CORE / "T03.json", "unsafe", 1, "FAIL"),
                     (uncovered_path, "fixed", 2, "INCONCLUSIVE"),
                     (self.root / "missing.json", "fixed", 3, "ERROR")]
        for case, version, code, status in scenarios:
            with self.subTest(status=status):
                process = subprocess.run(
                    [sys.executable, "-X", "utf8", "-m", "agentcheck_biz.cli", "run",
                     "--case", str(case), "--app-version", version, "--output", str(self.root / "runs")],
                    cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(process.returncode, code, process.stdout + process.stderr)
                self.assertEqual(json.loads(process.stdout)["status"], status)

    def test_running_state_visible_before_client_finishes_and_final_state_saved(self):
        observed_states = []

        def inspect_state(create, events, fields, max_attempts):
            manifest = json.loads((events.path.parent / "run.json").read_text(encoding="utf-8"))
            observed_states.append(manifest["execution_status"])
            code, status = self.cli("status", "--run-dir", events.path.parent)
            self.assertEqual((code, status["status"]), (2, "INCONCLUSIVE"))
            return run_scripted_client(create, events, fields, max_attempts)

        with patch("agentcheck_biz.adapters.ticket_local.run_scripted_client", side_effect=inspect_state):
            outcome = run_ticket_case(self.root, app_version="fixed", inject_fault=False)
        self.assertEqual(observed_states, ["running"])
        self.assertEqual(outcome["run"]["lifecycle_phase"], "finished")
        self.assertEqual(self.cli("status", "--run-dir", outcome["run_dir"])[0], 0)

    def test_interrupt_is_saved_and_never_reported_pass(self):
        with patch("agentcheck_biz.adapters.ticket_local.run_scripted_client", side_effect=KeyboardInterrupt):
            outcome = run_ticket_case(self.root, app_version="fixed", inject_fault=False)
        self.assertEqual(outcome["run"]["execution_status"], "interrupted")
        self.assertEqual(outcome["result"]["status"], "INCONCLUSIVE")
        self.assertEqual(self.cli("status", "--run-dir", outcome["run_dir"])[0], 2)

    def test_suite_retains_fail_error_and_inconclusive_in_counts(self):
        code, outcome = self.cli("suite", "--cases", CORE, "--app-version", "unsafe", "--output", self.root)
        self.assertEqual(code, 1)
        self.assertEqual(outcome["counts"], {"PASS": 3, "FAIL": 1, "INCONCLUSIVE": 0, "ERROR": 0})
        with patch("examples.ticket_agent.fixture.initialize_database", side_effect=sqlite3.OperationalError("no database")):
            error_code, errors = self.cli("suite", "--cases", CORE, "--output", self.root)
        self.assertEqual(error_code, 3)
        self.assertEqual(errors["counts"]["ERROR"], 4)
        source = self.root / "uncovered_suite"
        source.mkdir()
        uncovered = default_case()
        uncovered["fault"]["occurrence"] = 99
        save_json(source / "uncovered.json", uncovered)
        uncovered_code, uncovered_suite = self.cli("suite", "--cases", source, "--output", self.root)
        self.assertEqual(uncovered_code, 2)
        self.assertEqual(uncovered_suite["counts"]["INCONCLUSIVE"], 1)

    def test_suite_finalizes_even_if_run_allocation_fails(self):
        with patch("agentcheck_biz.cli.run_ticket_case", side_effect=OSError("run directory unavailable")):
            code, outcome = self.cli("suite", "--cases", CORE, "--output", self.root)
        self.assertEqual(code, 3)
        self.assertEqual(outcome["execution_status"], "completed")
        self.assertEqual(outcome["counts"]["ERROR"], 4)
        self.assertTrue((Path(outcome["suite_dir"]) / "suite.md").exists())

    def test_check_is_read_only_and_reobserves_changed_database(self):
        outcome = run_ticket_case(self.root, app_version="fixed", inject_fault=False)
        folder = Path(outcome["run_dir"])
        old_report = (folder / "checks.json").read_bytes()
        connection = sqlite3.connect(folder / "business.sqlite")
        try:
            with connection:
                connection.execute("UPDATE tickets SET customer_id='wrong' WHERE tenant_id='tenant-A'")
        finally:
            connection.close()
        code, result = self.cli("check", "--run-dir", folder)
        self.assertEqual((code, result["status"]), (1, "FAIL"))
        self.assertEqual((folder / "checks.json").read_bytes(), old_report)


if __name__ == "__main__":
    unittest.main()
