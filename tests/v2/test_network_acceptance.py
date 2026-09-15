"""D25 denominators, real evidence negatives, independent offline recheck."""

from copy import deepcopy
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from agentcheck_biz.checks import load_json
from agentcheck_biz.reports import save_json
from agentcheck_biz.network_acceptance.profiles import profiles, plan, aggregate, support_matrix
from agentcheck_biz.network_acceptance.runner import execute, independent_check, make_negatives
from agentcheck_biz.network_acceptance.verify import hashes, inspect_run, accepted_entry, fresh_identity, ReadOnlyGuard


class PlanTests(unittest.TestCase):
    def complete(self):
        return [dict(s, state="completed", observed=deepcopy(s["expected"])) for s in plan()]

    def test_twenty_profiles_exactly_five_repetitions(self):
        slots = plan()
        self.assertEqual((len(profiles()), len(slots), len({s["slot"] for s in slots})), (20, 100, 100))
        for profile in profiles():
            self.assertEqual([s["repetition"] for s in slots if s["profile"] == profile["id"]], [1, 2, 3, 4, 5])

    def test_pending_slots_never_disappear(self):
        result = aggregate(plan())
        self.assertEqual((result["status"], result["totals"]["planned"], result["totals"]["not_started"]), ("FAIL", 100, 100))

    def test_expected_failure_is_accepted_but_not_business_pass(self):
        result = aggregate(self.complete())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["distributions"]["B_unsafe_loss_retry"]["business_counts"], {"FAIL": 5})

    def test_uncovered_has_its_denominator(self):
        result = aggregate(self.complete())
        self.assertEqual(result["totals"], dict(planned=100, executed=100, exceptions=0, not_started=0,
            accepted=100, covered=75, transparent=15, not_covered=10))

    def test_one_flaky_result_fails_entire_acceptance(self):
        slots = self.complete()
        slots[0]["observed"]["business_status"] = "ERROR"
        result = aggregate(slots)
        self.assertEqual((result["status"], result["totals"]["accepted"]), ("FAIL", 99))
        self.assertEqual(len(result["distributions"][slots[0]["profile"]]["signatures"]), 2)

    def test_setup_exception_keeps_slot_and_reason(self):
        slots = self.complete()
        slots[0] = dict(plan()[0], state="exception", error="startup")
        result = aggregate(slots)
        self.assertEqual((result["totals"]["exceptions"], result["totals"]["executed"], result["totals"]["planned"]), (1, 100, 100))
        self.assertEqual(result["status"], "FAIL")

    def test_empty_plan_cannot_pass(self):
        self.assertEqual(aggregate([])["status"], "FAIL")

    def test_gitea_limits_are_explicit(self):
        matrix = support_matrix()
        self.assertTrue(any("Gitea internal" in item for item in matrix["unsupported"]))
        self.assertTrue(any("D26-D30" in item for item in matrix["unsupported"]))
        self.assertEqual(matrix["model_requests"], 0)


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        selected = {p["id"]: p for p in profiles()}
        cls.fixed = Path(execute(selected["C_fixed_loss_retry"], cls.root / "runs")["run_dir"])
        cls.miss = Path(execute(selected["ticket_P04_miss"], cls.root / "runs")["run_dir"])
        cls.crash = Path(execute(selected["crash_after_commit_6"], cls.root / "runs")["run_dir"])
        cls.expected = selected["C_fixed_loss_retry"]["expected"]
        cls.baseline = hashes(cls.fixed)
        cls.negatives = make_negatives(cls.root / "negatives", cls.fixed, cls.miss)
        positive = [dict(id="fixed", kind="proxy", run_dir=str(cls.fixed), expected=cls.expected,
                         require_fresh=True, evidence_sha256=cls.baseline),
                    dict(id="crash", kind="crash", run_dir=str(cls.crash), expected=selected["crash_after_commit_6"]["expected"],
                         require_fresh=True, evidence_sha256=hashes(cls.crash))]
        cls.result = independent_check(cls.root, positive + cls.negatives)

    def test_real_second_process_is_offline_readonly_and_exited(self):
        result = self.result
        self.assertEqual(result["status"], "PASS", result)
        self.assertNotEqual(result["worker_pid"], os.getpid())
        self.assertEqual(result["worker_pid"], result["step"]["pid"])
        self.assertTrue(result["offline"] and result["read_only"] and result["step"]["exited"])
        self.assertEqual((result["planned"], result["executed"]), (7, 7))

    def negative(self, label):
        row = next(r for r in self.result["results"] if r["id"] == "negative:" + label)
        self.assertTrue(row["accepted"], row)
        self.assertNotEqual(row["observed"]["business_status"], "PASS")
        self.assertTrue(row["evidence_unchanged"])
        return row["observed"]

    def test_missing_proxy_log_refuses_pass(self):
        self.negative("missing_proxy_log")

    def test_wrong_run_id_refuses_pass(self):
        self.negative("wrong_run_id")

    def test_unreadable_database_is_not_empty_or_success(self):
        self.assertEqual(self.negative("unreadable_database")["business_status"], "ERROR")

    def test_matching_clocks_cannot_substitute_for_request_identity(self):
        self.assertNotEqual(self.negative("wrong_request_id")["evidence_status"], "PASS")

    def test_proxy_miss_remains_inconclusive_not_covered(self):
        result = self.negative("proxy_not_hit")
        self.assertEqual((result["business_status"], result["evidence_status"], result["coverage"]),
                         ("INCONCLUSIVE", "INCONCLUSIVE", "not_covered"))

    def test_mutations_and_recheck_preserve_original_evidence(self):
        self.assertEqual(hashes(self.fixed), self.baseline)

    def test_producer_pass_summary_cannot_hide_bad_evidence(self):
        source = Path(next(e for e in self.negatives if e["id"] == "negative:wrong_run_id")["run_dir"])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(shutil.copytree(source, Path(temp) / source.name))
            for name in ("checks.json", "proxy-checks.json"):
                save_json(directory / name, dict(status="PASS", coverage="covered"))
            self.assertNotEqual(inspect_run(directory, "proxy")["evidence_status"], "PASS")

    def test_hash_tampering_refuses_even_matching_verdict(self):
        entry = dict(expected={"business_status": "PASS"}, evidence_sha256={"a": "one"})
        self.assertFalse(accepted_entry(entry, {"business_status": "PASS"}, {"a": "two"}, {"a": "two"}))

    def test_fresh_ticket_and_crash_initial_scope(self):
        self.assertEqual(fresh_identity(self.fixed, "proxy")["run_id"], self.fixed.name)
        self.assertEqual(fresh_identity(self.crash, "crash")["run_id"], self.crash.name)

    def test_duplicate_environment_rejected_by_independent_process(self):
        entry = dict(id="first", kind="proxy", run_dir=str(self.fixed), expected=self.expected,
                     require_fresh=True, evidence_sha256=self.baseline)
        directory = self.root / "duplicate"
        directory.mkdir()
        result = independent_check(directory, [entry, dict(entry, id="second")])
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(result["results"][1]["accepted"])

    def test_unknown_evidence_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            inspect_run(self.fixed, "unknown")

    def test_audit_guard_blocks_network_processes_and_evidence_writes(self):
        guard = ReadOnlyGuard([self.fixed])
        for event, args in (("socket.connect", ()), ("socket.bind", ()), ("subprocess.Popen", ()),
                            ("open", (str(self.fixed / "new.txt"), "w", os.O_WRONLY)),
                            ("os.remove", (str(self.fixed / "run.json"),)),
                            ("sqlite3.connect", (str(self.fixed / "business.sqlite"),))):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                guard(event, args)
        guard("open", (str(self.fixed / "run.json"), "r", os.O_RDONLY))
        guard("sqlite3.connect", ((self.fixed / "business.sqlite").as_uri() + "?mode=ro",))


if __name__ == "__main__":
    unittest.main()
