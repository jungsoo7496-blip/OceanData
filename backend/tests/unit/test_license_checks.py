"""17번 공급망 게이트: 이름·SPDX 논리·추출 근거·미승인 예외를 합성 자료로 검증한다."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
licenses = importlib.import_module("check_licenses")
sbom = importlib.import_module("check_sbom_licenses")


def package(name: str, expr: str, *, ecosystem: str = "pypi", display_name: str | None = None) -> dict:
    return {"name": display_name or name, "versionInfo": "1.0", "licenseDeclared": expr,
            "licenseConcluded": "NOASSERTION", "externalRefs": [
                {"referenceType": "purl", "referenceLocator": f"pkg:{ecosystem}/{name}@1.0"}]}


class LicenseChecks(unittest.TestCase):
    def sbom_exit(self, packages: list[dict], *, strict: bool = False,
                  extracted: list[dict] | None = None, exceptions: list[dict] | None = None) -> int:
        doc = {"spdxVersion": "SPDX-2.3", "packages": packages,
               "hasExtractedLicensingInfos": extracted or []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sbom.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            with patch.object(sbom, "load_exceptions", return_value=exceptions or []), \
                    contextlib.redirect_stdout(io.StringIO()):
                return sbom.main(str(path), strict=strict)

    def runtime_exit(self, packages: list[dict], exceptions: list[dict], *, strict: bool = False) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            (root / "tools/license-exceptions.json").write_text(
                json.dumps({"exceptions": exceptions}), encoding="utf-8")
            with patch.object(licenses, "ROOT", root), \
                    patch.object(licenses, "python_packages", return_value=packages), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return licenses.main(["--web-lock", str(root / "no-web-lock.json"),
                                      *(["--strict"] if strict else [])])

    def test_pypi_name_equivalence(self):
        for name in ("typing_extensions", "Typing.Extensions", "typing---extensions", "TYPING_EXTENSIONS"):
            with self.subTest(name=name):
                self.assertEqual(licenses.package_name(name), "typing-extensions")

    def test_spdx_parentheses_and_precedence(self):
        cases = {
            "MIT AND (GPL-3.0-only OR Apache-2.0)": True,
            "(MIT OR GPL-3.0-only) AND GPL-2.0-only": False,
            "GPL-3.0-only AND (Apache-2.0 OR MIT)": False,
            "MIT OR GPL-3.0-only AND LGPL-2.1-only": True,
            "((MIT OR BSD-3-Clause) AND (ISC OR GPL-3.0-only))": True,
            "mIt AND apache-2.0": True,
        }
        for expr, expected in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(sbom.allowed(expr), expected)

    def test_with_is_not_dropped(self):
        self.assertFalse(sbom.allowed("MIT WITH LLVM-exception"))
        self.assertFalse(sbom.allowed("GPL-2.0-only WITH Classpath-exception-2.0"))
        self.assertTrue(sbom.allowed("GPL-2.0-only WITH Classpath-exception-2.0 OR Apache-2.0"))

    def test_invalid_and_unsupported_expressions_fail_release(self):
        for expr in ("MIT OR", "(MIT", "MIT)", "MIT / Apache-2.0", "MIT XOR Apache-2.0",
                     "MIT WITH", "MIT WITH (LLVM-exception)", "MIT or BSD-3-Clause", ""):
            with self.subTest(expr=expr):
                self.assertFalse(sbom.allowed(expr))
                self.assertEqual(self.sbom_exit([package("synthetic", expr)], strict=True), 1)

    def test_unknown_in_required_branch_fails_release(self):
        self.assertEqual(self.sbom_exit([package("synthetic", "MIT AND LicenseRef-Unknown")], strict=True), 1)
        self.assertEqual(self.sbom_exit([package("synthetic", "MIT OR LicenseRef-Unknown")], strict=True), 0)

    def test_text_aliases_are_exact(self):
        self.assertEqual(licenses.normalize_text("BSD 3-Clause License"), "BSD-3-Clause")
        self.assertEqual(licenses.normalize_text("Apache License 2.0"), "Apache-2.0")
        for text in ("BSD-3-Clause-Variant", "BSD 3-Clause with advertising", "MIT License with restrictions",
                     "Apache License 2.0 AND proprietary terms", "Dual License"):
            with self.subTest(text=text):
                self.assertIsNone(licenses.normalize_text(text))

    def test_actual_syft_extracted_aliases(self):
        extracted = [{"licenseId": "LicenseRef-BSD-3-Clause-License", "extractedText": "BSD 3-Clause License"},
                     {"licenseId": "LicenseRef-Apache-License-2.0", "extractedText": "Apache License 2.0"}]
        packages = [package("synthetic-bsd", "LicenseRef-BSD-3-Clause-License"),
                    package("synthetic-apache", "LicenseRef-Apache-License-2.0")]
        self.assertEqual(self.sbom_exit(packages, strict=True, extracted=extracted), 0)

    def test_ref_name_and_link_do_not_override_custom_terms(self):
        for extracted in ([], [{"licenseId": "LicenseRef-MIT", "name": "MIT",
                               "seeAlsos": ["https://spdx.org/licenses/MIT.html"],
                               "extractedText": "MIT with additional commercial restrictions"}]):
            with self.subTest(extracted=extracted):
                self.assertEqual(self.sbom_exit([package("synthetic", "LicenseRef-MIT")], strict=True,
                                               extracted=extracted), 1)

    def test_ambiguous_dual_license_stays_unknown(self):
        extracted = [{"licenseId": "LicenseRef-Dual-License", "extractedText": "Dual License"}]
        self.assertEqual(self.sbom_exit([package("synthetic", "LicenseRef-Dual-License")],
                                       extracted=extracted), 0)
        self.assertEqual(self.sbom_exit([package("synthetic", "LicenseRef-Dual-License")], strict=True,
                                       extracted=extracted), 1)

    def test_conflicting_and_cyclic_refs_fail_release(self):
        for extracted in ([{"licenseId": "LicenseRef-A", "extractedText": "MIT"},
                           {"licenseId": "LicenseRef-A", "extractedText": "GPL-3.0-only"}],
                          [{"licenseId": "LicenseRef-A", "extractedText": "LicenseRef-B"},
                           {"licenseId": "LicenseRef-B", "extractedText": "LicenseRef-A"}]):
            with self.subTest(extracted=extracted):
                self.assertEqual(self.sbom_exit([package("synthetic", "LicenseRef-A")], strict=True,
                                               extracted=extracted), 1)

    def test_spdx_disallowed_license_blocks_development_and_release(self):
        for strict in (False, True):
            self.assertEqual(self.sbom_exit([package("synthetic", "GPL-3.0-only")], strict=strict), 1)

    def test_unknown_and_empty_sbom_block_release(self):
        self.assertEqual(self.sbom_exit([package("synthetic", "NOASSERTION")]), 0)
        self.assertEqual(self.sbom_exit([package("synthetic", "NOASSERTION")], strict=True), 1)
        self.assertEqual(self.sbom_exit([], strict=True), 1)

    def test_pending_exception_name_normalization_and_release_block(self):
        entries = [{"package": "typing_extensions", "license": "PSF-2.0", "approved": False}]
        packages = [package("typing-extensions", "PSF-2.0")]
        self.assertEqual(self.sbom_exit(packages, exceptions=entries), 0)
        self.assertEqual(self.sbom_exit(packages, strict=True, exceptions=entries), 1)

    def test_approved_exception_requires_same_license_and_ecosystem(self):
        entries = [{"package": "typing_extensions", "license": "PSF-2.0", "approved": True}]
        self.assertEqual(self.sbom_exit([package("typing-extensions", "PSF-2.0")],
                                       strict=True, exceptions=entries), 0)
        self.assertEqual(self.sbom_exit([package("typing-extensions", "GPL-3.0-only")],
                                       strict=True, exceptions=entries), 1)
        self.assertEqual(self.sbom_exit([package("typing_extensions", "PSF-2.0", ecosystem="npm")],
                                       strict=True, exceptions=entries), 1)

    def test_display_name_cannot_bypass_exception(self):
        entries = [{"package": "typing_extensions", "license": "PSF-2.0", "approved": True}]
        self.assertEqual(self.sbom_exit([package("different-package", "PSF-2.0",
                                               display_name="typing_extensions")],
                                       strict=True, exceptions=entries), 1)

    def test_exception_expression_normalizes_order_without_changing_logic(self):
        entries = [{"package": "synthetic", "license": "MIT AND PSF-2.0", "approved": True}]
        self.assertIsNotNone(licenses.matching_exception("synthetic", "pypi", "(PSF-2.0 AND MIT)", entries))
        self.assertIsNone(licenses.matching_exception("synthetic", "pypi", "PSF-2.0 AND GPL-3.0-only", entries))

    def test_python_and_npm_use_same_parser(self):
        expr = "(MIT OR GPL-3.0-only) AND LGPL-2.1-only"
        self.assertFalse(licenses.classify_python({"expr": expr})[0])
        self.assertFalse(sbom.allowed(expr))

    def test_python_classifier_does_not_hide_unknown_additional_license(self):
        pkg = {"license": "", "classifiers": ["License :: OSI Approved :: MIT License",
                                                "License :: Other/Proprietary License"]}
        self.assertFalse(licenses.classify_python(pkg)[0])

    def test_runtime_pending_exception_and_unknown(self):
        entries = [{"package": "typing_extensions", "license": "PSF-2.0", "approved": False}]
        packages = [{"name": "typing-extensions", "version": "1.0", "expr": "PSF-2.0"}]
        self.assertEqual(self.runtime_exit(packages, entries), 0)
        self.assertEqual(self.runtime_exit(packages, entries, strict=True), 1)
        packages[0]["expr"] = "UNKNOWN"
        self.assertEqual(self.runtime_exit(packages, entries), 0)
        self.assertEqual(self.runtime_exit(packages, entries, strict=True), 1)
        packages[0]["expr"] = "GPL-3.0-only"
        self.assertEqual(self.runtime_exit(packages, entries), 1)


if __name__ == "__main__":
    unittest.main()
