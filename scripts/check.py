"""Run repeatable offline checks from any working directory. No .env is loaded."""

import argparse
import logging
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git_paths(*args):
    result = subprocess.run(["git", *args, "-z"], cwd=ROOT, check=True, capture_output=True)
    return [name for name in result.stdout.decode().split("\0") if name]


def credential_findings(body):
    """High-confidence checks; report labels only, never matched credentials."""
    patterns = {
        "private key": r"(?m)^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "GitHub token": r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
        "OpenAI project key": r"\bsk-proj-[A-Za-z0-9_-]{40,}\b",
        "credential assignment": (
            r"(?im)^\s*(?:CARTO_API_KEY|AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|GITHUB_TOKEN|PASSWORD)"
            r"""\s*[=:]\s*["']?([A-Za-z0-9_./+\-=]{12,})"""
        ),
    }
    findings = []
    for label, pattern in patterns.items():
        for match in re.finditer(pattern, body):
            if label == "credential assignment":
                value = match.group(1).lower()
                if any(word in value for word in ("example", "placeholder", "your_", "your-", "sentinel")):
                    continue
            findings.append(label)
            break
    return findings


def privacy_check():
    tracked = git_paths("ls-files", "--cached")
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=ROOT,
        input=("\0".join(tracked) + "\0").encode(),
        capture_output=True,
    )
    if ignored.returncode not in (0, 1):
        raise RuntimeError("Could not verify tracked files against .gitignore")
    problems = [
        f"{name}: tracked despite .gitignore (ignoring does not untrack files)"
        for name in ignored.stdout.decode().split("\0")
        if name
    ]
    candidates = sorted(set(git_paths("ls-files", "--cached", "--others", "--exclude-standard")))
    for name in candidates:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        try:
            body = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        problems.extend(f"{name}: possible {label} (value withheld)" for label in credential_findings(body))
    if problems:
        raise RuntimeError("Public-file check failed:\n" + "\n".join(problems))
    print(
        f"Public-file check passed ({len(candidates)} tracked/publishable files; ignored files not read).", flush=True
    )


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "backend", "frontend", "privacy"), default="all")
    args = parser.parse_args()
    if args.suite in ("all", "privacy"):
        privacy_check()
    if args.suite in ("all", "backend"):
        interpreter = ROOT / ".venv" / "bin" / "python"
        if interpreter.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
            run(str(interpreter), str(Path(__file__).resolve()), "--suite", "backend")
        else:
            try:
                __import__("fastapi")
                __import__("httpx")
                __import__("shapely")
            except ImportError as exc:
                raise RuntimeError("Backend dependencies missing. Run `make setup` first.") from exc
            sys.path.insert(0, str(ROOT / "backend"))
            logging.disable(logging.CRITICAL)
            tests = unittest.defaultTestLoader.discover(str(ROOT / "backend"), pattern="test_*.py")
            if not unittest.TextTestRunner(verbosity=1).run(tests).wasSuccessful():
                return 1
            try:
                __import__("ruff")
            except ImportError as exc:
                raise RuntimeError("Ruff missing. Run `make setup` first.") from exc
            run(sys.executable, "-m", "ruff", "check", "backend", "scripts")
            run(sys.executable, "-m", "ruff", "format", "--check", "backend", "scripts")
    if args.suite in ("all", "frontend"):
        run("node", "--check", "frontend/app.js")
        tests = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "frontend/tests").glob("*.test.cjs"))
        if not tests:
            raise RuntimeError("No frontend regression tests found")
        run("node", "--test", *tests)
        if not (ROOT / "frontend/node_modules/.bin/prettier").exists():
            raise RuntimeError("Prettier missing. Run `make setup` first.")
        run("npm", "--prefix", "frontend", "run", "--silent", "format:check")
    if args.suite == "all":
        tests = unittest.defaultTestLoader.discover(str(ROOT / "scripts/tests"), pattern="test_*.py")
        if not unittest.TextTestRunner(verbosity=1).run(tests).wasSuccessful():
            return 1
        run("git", "diff", "--check")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
