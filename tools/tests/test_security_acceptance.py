"""17번: 실제 스캔 보고서 4개가 모두 유효해야 T01-h를 통과한다."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import security_acceptance  # noqa: E402 - 저장소 tools 모듈을 직접 시험한다


def trivy_report(artifact_type="filesystem", severities=()):
    return {"SchemaVersion": 2, "ArtifactName": "synthetic", "ArtifactType": artifact_type,
            "Results": [{"Target": "synthetic/package-lock.json", "Class": "lang-pkgs", "Type": "npm",
                         "Vulnerabilities": [
                {"VulnerabilityID": f"SYNTHETIC-{index}", "Severity": severity}
                for index, severity in enumerate(severities)]}]}


def zap_report(riskcode="1"):
    return {"site": [{"@name": "http://synthetic.test", "alerts": [
        {"pluginid": "synthetic", "riskcode": riskcode}]}]}


class TrivyReportTests(unittest.TestCase):
    def test_clean_and_lower_risk_reports_pass(self):
        for severities in ((), ("LOW", "MEDIUM")):
            with self.subTest(severities=severities):
                self.assertTrue(security_acceptance.assess_trivy(trivy_report(severities=severities),
                                                                "filesystem")[0])

    def test_omitted_vulnerabilities_means_no_findings(self):
        doc = trivy_report()
        del doc["Results"][0]["Vulnerabilities"]
        self.assertTrue(security_acceptance.assess_trivy(doc, "filesystem")[0])

    def test_high_and_critical_fail_in_any_result(self):
        for severity in ("HIGH", "CRITICAL"):
            doc = trivy_report()
            doc["Results"].extend(trivy_report(severities=(severity,))["Results"])
            with self.subTest(severity=severity):
                self.assertFalse(security_acceptance.assess_trivy(doc, "filesystem")[0])

    def test_unknown_severity_and_malformed_vulnerabilities_fail(self):
        for severity in ("UNKNOWN", "high", "", None, 3, [], {}):
            with self.subTest(severity=severity):
                self.assertFalse(security_acceptance.assess_trivy(trivy_report(severities=(severity,)),
                                                                 "filesystem")[0])
        for vulnerabilities in (None, {}, "none", [None], ["HIGH"]):
            doc = trivy_report()
            doc["Results"][0]["Vulnerabilities"] = vulnerabilities
            with self.subTest(vulnerabilities=vulnerabilities):
                self.assertFalse(security_acceptance.assess_trivy(doc, "filesystem")[0])

    def test_schema_empty_results_and_wrong_target_type_fail(self):
        docs = [{}, trivy_report("container_image")]
        for schema in (1, "2", None, 2.0):
            docs.append({**trivy_report(), "SchemaVersion": schema})
        for results in (None, [], {}, [None], [{}], [{"Target": ""}]):
            docs.append({**trivy_report(), "Results": results})
        docs.append({**trivy_report(), "ArtifactName": ""})
        for fields in ({"Class": "secret"}, {"Class": None}, {"Class": []}, {"Class": {}},
                       {"Type": ""}, {"Type": None}):
            doc = trivy_report()
            doc["Results"][0].update(fields)
            docs.append(doc)
        for doc in docs:
            with self.subTest(doc=doc):
                self.assertFalse(security_acceptance.assess_trivy(doc, "filesystem")[0])


class SecurityAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "results.jsonl"
        self.paths = {kind: self.root / f"{kind}.json" for kind in (
            "trivy_image", "trivy_source", "zap_api", "zap_web")}
        self.docs = {"trivy_image": trivy_report("container_image"), "trivy_source": trivy_report(),
                     "zap_api": zap_report(), "zap_web": zap_report()}
        for kind, doc in self.docs.items():
            self.write(kind, doc)

    def write(self, kind, doc):
        self.paths[kind].write_text(json.dumps(doc), encoding="utf-8")

    def invoke(self):
        args = []
        for kind, path in self.paths.items():
            args.extend(["--" + kind.replace("_", "-"), str(path)])
        with contextlib.redirect_stdout(io.StringIO()):
            return security_acceptance.main([*args, "--out", str(self.out)])

    def last_result(self):
        return json.loads(self.out.read_text(encoding="utf-8").splitlines()[-1])

    def test_all_four_reports_pass_and_include_content_hashes(self):
        self.assertEqual(self.invoke(), 0)
        row = self.last_result()
        self.assertEqual(row["covers"], ["T01-h"])
        self.assertEqual(row["outcome"], "passed")
        self.assertEqual(row["evidence_source"], "LOCAL_SECURITY_SCAN_REPORTS")
        self.assertIn("GitHub Actions", row["log"])
        self.assertEqual(set(row["reports"]), set(self.paths))
        for kind, path in self.paths.items():
            self.assertEqual(row["reports"][kind]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_each_missing_report_records_failure(self):
        for kind, path in self.paths.items():
            with self.subTest(kind=kind):
                raw = path.read_bytes()
                path.unlink()
                try:
                    self.assertEqual(self.invoke(), 1)
                    self.assertEqual(self.last_result()["outcome"], "failed")
                finally:
                    path.write_bytes(raw)

    def test_malformed_json_non_object_and_invalid_encoding_record_failure(self):
        path = self.paths["trivy_source"]
        for raw in (b"{", b"[]", b"null", b"\xff"):
            path.write_bytes(raw)
            with self.subTest(raw=raw):
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.last_result()["outcome"], "failed")

    def test_high_risk_in_each_report_records_failure(self):
        for kind in self.paths:
            with self.subTest(kind=kind):
                doc = (zap_report("3") if kind.startswith("zap_") else trivy_report(
                    "container_image" if kind == "trivy_image" else "filesystem", ("HIGH",)))
                self.write(kind, doc)
                try:
                    self.assertEqual(self.invoke(), 1)
                    self.assertEqual(self.last_result()["outcome"], "failed")
                finally:
                    self.write(kind, self.docs[kind])

    def test_unknown_zap_risk_and_empty_trivy_results_record_failure(self):
        self.write("zap_api", zap_report("UNKNOWN"))
        self.assertEqual(self.invoke(), 1)
        self.write("zap_api", self.docs["zap_api"])
        self.write("trivy_image", {**trivy_report("container_image"), "Results": []})
        self.assertEqual(self.invoke(), 1)

    def test_results_append_without_overwriting_existing_acceptance(self):
        previous = {"nodeid": "synthetic", "covers": ["T01-a"], "outcome": "passed"}
        self.out.write_text(json.dumps(previous) + "\n", encoding="utf-8")
        self.assertEqual(self.invoke(), 0)
        rows = [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0], previous)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
