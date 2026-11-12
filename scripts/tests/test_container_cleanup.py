import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("container_checks", SCRIPTS / "check_containers.py")
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class ContainerCleanupTests(unittest.TestCase):
    def test_failed_http_check_cleans_only_resources_created_by_this_run(self):
        launched = []

        def command(*args, **kwargs):
            if args[:3] == ("docker", "run", "-d"):
                launched.append(args[args.index("--name") + 1])
            return "127.0.0.1:19000" if args[:2] == ("docker", "port") else "test-result"

        with (
            patch.object(checks, "command", side_effect=command),
            patch.object(checks, "check", side_effect=AssertionError("bad HTTP headers")),
            patch.object(checks.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as cleanup,
        ):
            with self.assertRaisesRegex(AssertionError, "bad HTTP headers"):
                checks.main()
        removed = [call.args[0][-1] for call in cleanup.call_args_list if call.args[0][:3] == ["docker", "rm", "-f"]]
        self.assertEqual(removed, list(reversed(launched)))
        self.assertEqual(len(removed), 2)
        self.assertTrue(all(name.startswith("wildfire-check-") for name in removed))
        self.assertTrue(any(call.args[0][:3] == ["docker", "network", "rm"] for call in cleanup.call_args_list))

    def test_failed_build_does_not_touch_existing_containers_or_networks(self):
        def command(*args, **kwargs):
            if args[:2] == ("docker", "build"):
                raise subprocess.CalledProcessError(1, args)
            return "test-result"

        with (
            patch.object(checks, "command", side_effect=command),
            patch.object(checks.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as cleanup,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                checks.main()
        self.assertEqual(len(cleanup.call_args_list), 1)
        self.assertEqual(cleanup.call_args.args[0][:3], ["docker", "image", "rm"])


if __name__ == "__main__":
    unittest.main()
