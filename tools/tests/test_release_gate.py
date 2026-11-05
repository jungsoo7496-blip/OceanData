"""17번: 누락·생략·오래된 결과가 릴리스를 허용하지 않는지 검사한다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import go_acceptance
import migration_check
import release_gate
import run_acceptance
import zap_check


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "var").mkdir()
        (self.root / "contracts").mkdir()
        (self.root / "contracts/openapi.yaml").write_text("info: {version: 1.0.0}\n", encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(release_gate, "ROOT", self.root).start()
        patch.dict(os.environ, {}, clear=True).start()

    def passing_run(self, name, args, *other, **kwargs):
        if name == "acceptance_report":
            (self.root / "var/acceptance.json").write_text(json.dumps({
                "items": [{"id": i, "status": "PASS"} for i in release_gate.MILESTONE_CASES["M1"]]}),
                encoding="utf-8")
        return {"check": name, "ok": True, "command": " ".join(args), "tail": ""}

    def gate(self, with_tests=True):
        valid = {"check": "contract_major_version", "ok": True}
        with patch.object(release_gate, "run", side_effect=self.passing_run), \
             patch.object(release_gate, "contract_major_ok", return_value=valid):
            return release_gate.gate("M1", None, with_tests)

    def test_unknown_milestone_is_rejected(self):
        self.assertFalse(release_gate.gate("UNKNOWN", None, True)["ok"])

    def test_no_tests_cannot_reuse_old_passing_acceptance(self):
        self.passing_run("acceptance_report", [])
        report = self.gate(with_tests=False)
        self.assertFalse(report["ok"])
        failed = {c["check"] for c in report["checks"] if not c["ok"]}
        self.assertTrue({"tests", "acceptance_M1"} <= failed)

    def test_missing_database_is_a_failure_not_a_skip(self):
        report = self.gate()
        self.assertFalse(report["ok"])
        failed = {c["check"] for c in report["checks"] if not c["ok"]}
        self.assertTrue({"migrations", "backend_tests", "dast"} <= failed)

    def test_all_required_checks_are_executed_and_licenses_are_strict(self):
        valid = {"check": "contract_major_version", "ok": True}
        with patch.dict(os.environ, {"TEST_DATABASE_URL": "test", "RELEASE_DAST_URL": "http://isolated.test",
                                     "RELEASE_DAST_WEB_URL": "http://isolated-web.test"}), \
             patch.object(release_gate, "run", side_effect=self.passing_run) as runner, \
             patch.object(release_gate, "contract_major_ok", return_value=valid):
            report = release_gate.gate("M1", None, True)
        self.assertTrue(report["ok"])
        names = {c["check"] for c in report["checks"]}
        self.assertTrue({"migrations", "backend_tests", "go_vet", "go_tests", "web_e2e", "dast", "licenses",
                         "sbom", "sbom_licenses", "image_vulnerabilities", "source_vulnerabilities",
                         "python_static_security", "python_vulnerabilities", "web_install", "dast_risk",
                         "web_dast", "web_dast_risk",
                         "web_vulnerabilities", "security_acceptance"} <= names)
        order = [call.args[0] for call in runner.call_args_list]
        self.assertLess(order.index("web_install"), order.index("web_build"))
        self.assertLess(order.index("web_install"), order.index("web_e2e"))
        for call in runner.call_args_list:
            if call.args[0] in ("licenses", "sbom_licenses"):
                self.assertIn("--strict", call.args[1])

    def test_build_failure_cannot_reuse_old_sbom(self):
        (self.root / "var/sbom.spdx.json").write_text("{}", encoding="utf-8")

        def run(name, args, *other, **kwargs):
            return {**self.passing_run(name, args), "ok": name != "runtime_image"}

        valid = {"check": "contract_major_version", "ok": True}
        with patch.object(release_gate, "run", side_effect=run), \
             patch.object(release_gate, "contract_major_ok", return_value=valid):
            report = release_gate.gate("M1", None, False)
        failed = {c["check"] for c in report["checks"] if not c["ok"]}
        self.assertTrue({"runtime_image", "sbom", "sbom_licenses", "image_vulnerabilities"} <= failed)

    def test_install_failure_cannot_test_old_node_modules(self):
        def run(name, args, *other, **kwargs):
            return {**self.passing_run(name, args), "ok": name != "web_install"}

        valid = {"check": "contract_major_version", "ok": True}
        with patch.object(release_gate, "run", side_effect=run) as runner, \
             patch.object(release_gate, "contract_major_ok", return_value=valid):
            report = release_gate.gate("M1", None, True)
        self.assertFalse(report["ok"])
        executed = {call.args[0] for call in runner.call_args_list}
        self.assertTrue({"web_build", "web_e2e", "web_vulnerabilities"}.isdisjoint(executed))

    def test_high_risk_json_blocks_even_if_zap_process_succeeds(self):
        def run(name, args, *other, **kwargs):
            return {**self.passing_run(name, args), "ok": name != "dast_risk"}

        valid = {"check": "contract_major_version", "ok": True}
        with patch.dict(os.environ, {"TEST_DATABASE_URL": "test", "RELEASE_DAST_URL": "http://isolated.test",
                                     "RELEASE_DAST_WEB_URL": "http://isolated-web.test"}), \
             patch.object(release_gate, "run", side_effect=run), \
             patch.object(release_gate, "contract_major_ok", return_value=valid):
            report = release_gate.gate("M1", None, True)
        self.assertFalse(report["ok"])
        self.assertFalse(next(c for c in report["checks"] if c["check"] == "dast_risk")["ok"])

    def test_old_zap_report_is_removed_before_scan(self):
        old = self.root / "var/release-checks/zap-report.json"
        old.parent.mkdir()
        old.write_text('{"site":[{"@name":"old","alerts":[]}]}', encoding="utf-8")

        def run(name, args, *other, **kwargs):
            if name == "dast":
                self.assertFalse(old.exists())
            return {**self.passing_run(name, args), "ok": name != "dast_risk"}

        valid = {"check": "contract_major_version", "ok": True}
        with patch.dict(os.environ, {"RELEASE_DAST_URL": "http://isolated.test"}), \
             patch.object(release_gate, "run", side_effect=run), \
             patch.object(release_gate, "contract_major_ok", return_value=valid):
            self.assertFalse(release_gate.gate("M1", None, False)["ok"])

    def test_missing_command_is_recorded(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            result = release_gate.run("missing", ["missing-tool"])
        self.assertFalse(result["ok"])
        self.assertTrue((self.root / result["log"]).exists())

    def test_log_redacts_database_credentials(self):
        r = subprocess.CompletedProcess([], 1, "postgresql+pg8000://secret:password@host/db", "")
        with patch.object(subprocess, "run", return_value=r):
            result = release_gate.run("db", ["command"])
        self.assertNotIn("password", result["tail"])
        self.assertNotIn("password", (self.root / result["log"]).read_text())

    def test_unknown_base_ref_cannot_hide_major_change(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 128, "", "")):
            self.assertFalse(release_gate.contract_major_ok("does-not-exist")["ok"])


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_acceptance_reports_even_when_pytest_collection_fails(self):
        statuses = [subprocess.CompletedProcess([], c) for c in (2, 0, 0, 0)]
        with patch.object(run_acceptance, "ROOT", self.root), \
             patch.dict(os.environ, {"TEST_DATABASE_URL": "test"}), \
             patch.object(subprocess, "run", side_effect=statuses) as runner:
            self.assertEqual(run_acceptance.main(), 1)
        self.assertIn("tools/acceptance_report.py", runner.call_args.args[0])
        self.assertEqual(runner.call_count, 4)

    def test_acceptance_requires_database_even_if_child_reports_pass(self):
        with patch.object(run_acceptance, "ROOT", self.root), patch.dict(os.environ, {}, clear=True), \
             patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(run_acceptance.main(), 1)

    def test_uncovered_go_failure_is_not_lost(self):
        source = self.root / "go.jsonl"
        source.write_text('\n'.join(json.dumps(e) for e in [
            {"Package": "module/internal", "Test": "TestCovered", "Action": "pass"},
            {"Package": "module/uncovered", "Action": "fail"}]), encoding="utf-8")
        args = ["go_acceptance.py", "--input", str(source), "--out", str(self.root / "out.jsonl")]
        with patch.object(sys, "argv", args), \
             patch.object(go_acceptance, "covers_map", return_value={("internal", "TestCovered"): ["T02-a"]}), \
             patch.object(go_acceptance, "module_name", return_value="module"):
            self.assertEqual(go_acceptance.main(), 1)
        self.assertEqual(json.loads((self.root / "out.jsonl").read_text())["outcome"], "passed")

    def test_migration_failure_drops_only_generated_database(self):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        with patch.dict(os.environ, {"TEST_DATABASE_URL": "postgresql+pg8000://owner:secret@host/existing"}), \
             patch.object(migration_check, "create_engine", return_value=engine), \
             patch.object(migration_check.secrets, "token_hex", return_value="a" * 16), \
             patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "alembic")):
            with self.assertRaises(subprocess.CalledProcessError):
                migration_check.check()
        statements = [str(c.args[0]) for c in connection.execute.call_args_list]
        self.assertEqual(statements, ['CREATE DATABASE "marine_gate_aaaaaaaaaaaaaaaa"',
                                      'DROP DATABASE "marine_gate_aaaaaaaaaaaaaaaa" WITH (FORCE)'])
        engine.dispose.assert_called_once()


class ZapRiskTests(unittest.TestCase):
    def report(self, riskcodes):
        return {"site": [{"@name": "http://isolated.test", "alerts": [
            {"riskcode": r, "pluginid": "synthetic"} for r in riskcodes]}]}

    def test_high_risk_blocks(self):
        self.assertFalse(zap_check.assess(self.report(["0", "3"]))[0])

    def test_lower_risks_and_no_alerts_satisfy_high_risk_policy(self):
        self.assertTrue(zap_check.assess(self.report(["0", "1", "2"]))[0])
        self.assertTrue(zap_check.assess(self.report([]))[0])

    def test_missing_malformed_or_unknown_risk_blocks(self):
        for doc in ({}, {"site": []}, {"site": [{"alerts": []}]}, self.report(["HIGH"]),
                    self.report([4]), self.report([None])):
            with self.subTest(doc=doc):
                self.assertFalse(zap_check.assess(doc)[0])


if __name__ == "__main__":
    unittest.main()
