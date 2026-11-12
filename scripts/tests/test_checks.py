import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("project_checks", Path(__file__).parents[1] / "check.py")
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class PublicFileChecks(unittest.TestCase):
    def test_realistic_credential_is_flagged_without_echoing_its_value(self):
        value = "aB3_" * 12
        findings = checks.credential_findings("CARTO_" + "API_KEY=" + value)
        self.assertEqual(findings, ["credential assignment"])
        self.assertNotIn(value, str(findings))

    def test_documented_empty_and_environment_references_are_allowed(self):
        for value in ("", "${CARTO_API_KEY:-}", "your_api_key_here", "review-sentinel"):
            self.assertEqual(checks.credential_findings("CARTO_" + "API_KEY=" + value), [])

    def test_private_key_header_is_flagged(self):
        self.assertEqual(checks.credential_findings("-----BEGIN " + "OPENSSH PRIVATE KEY-----\nbody"), ["private key"])

    def test_public_example_text_does_not_trigger_secret_detection(self):
        self.assertEqual(checks.credential_findings("NWS_USER_AGENT=UtahWildfireTracker/1.0 (contact@example.com)"), [])

    def test_ignored_file_that_is_already_tracked_fails_the_public_file_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True, capture_output=True)
            (root / ".env").write_text("local-only configuration")
            subprocess.run(["git", "add", ".env"], cwd=root, check=True, capture_output=True)
            (root / ".gitignore").write_text(".env\n")
            with patch.object(checks, "ROOT", root), self.assertRaisesRegex(RuntimeError, "tracked despite .gitignore"):
                checks.privacy_check()


if __name__ == "__main__":
    unittest.main()
