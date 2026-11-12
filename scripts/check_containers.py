"""Build and check isolated containers; always clean up only this run's resources.

Uses synthetic boundaries and a fake key. Does not load .env or query live feeds.
Requires a running Docker engine. Published ports bind only to localhost.
"""

import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from check_frontend_http import check

ROOT = Path(__file__).resolve().parents[1]


def command(*args, capture=False):
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def main():
    command("docker", "info", "--format", "{{.ServerVersion}}", capture=True)
    command("docker", "compose", "--env-file", ".env.example", "config", "--quiet")
    token = uuid.uuid4().hex[:12]
    network = f"wildfire-check-{token}"
    backend_image = f"wildfire-check-backend:{token}"
    frontend_image = f"wildfire-check-frontend:{token}"
    containers = []
    images = []
    network_created = False
    try:
        for image, folder in ((backend_image, "backend"), (frontend_image, "frontend")):
            # Record before building so partial success is cleaned up too.
            images.append(image)
            command("docker", "build", "--quiet", "-t", image, f"./{folder}")
        command(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{ROOT / 'backend'}:/tests:ro",
            "-w",
            "/tests",
            backend_image,
            "python",
            "-c",
            "import logging,unittest; logging.disable(logging.CRITICAL); "
            'r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover(".",pattern="test_*.py")); '
            "raise SystemExit(not r.wasSuccessful())",
        )
        command("docker", "network", "create", network, capture=True)
        network_created = True
        with tempfile.TemporaryDirectory(prefix="wildfire-check-") as directory:
            boundaries = {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"NAME": "Smoke Check City", "OBJECTID": 1, "FIPS": "00000"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[-111, 40], [-110.99, 40], [-110.99, 40.01], [-111, 40.01], [-111, 40]]],
                        },
                    }
                ],
            }
            Path(directory, "utah_municipal_boundaries.geojson").write_text(json.dumps(boundaries))
            backend_name = network + "-backend"
            containers.append(backend_name)
            command(
                "docker",
                "run",
                "-d",
                "--name",
                backend_name,
                "--network",
                network,
                "--network-alias",
                "backend",
                "-v",
                f"{directory}:/app/app/data:ro",
                backend_image,
                capture=True,
            )
            # Exercise both configuration branches without using or disclosing a real key.
            for enabled in (False, True):
                name = network + ("-carto" if enabled else "-osm")
                containers.append(name)
                command(
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    network,
                    "-p",
                    "127.0.0.1::80",
                    "-e",
                    "PRODUCTION_DOMAIN=_",
                    "-e",
                    "CARTO_API_KEY=" + ("review-sentinel" if enabled else ""),
                    frontend_image,
                    capture=True,
                )
                command("docker", "exec", name, "nginx", "-t")
                address = command("docker", "port", name, "80/tcp", capture=True).splitlines()[0]
                base = "http://" + address
                check(base, expected_carto=enabled)
                for attempt in range(30):
                    try:
                        with urlopen(base + "/api/health", timeout=2) as response:
                            assert json.load(response)["status"] == "ok"
                        break
                    except (URLError, TimeoutError):
                        if attempt == 29:
                            raise
                        time.sleep(0.2)
                print(f"API proxy and {'Carto' if enabled else 'OSM'} configuration checks passed.", flush=True)
        print("Container checks passed; cleaning up test resources.", flush=True)
    finally:
        cleanup_failed = False
        for name in reversed(containers):
            result = subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            cleanup_failed |= result.returncode != 0
        if network_created:
            cleanup_failed |= subprocess.run(["docker", "network", "rm", network], capture_output=True).returncode != 0
        for image in images:
            subprocess.run(["docker", "image", "rm", image], capture_output=True)
        if cleanup_failed:
            print(f"Some test resources could not be removed; inspect names beginning {network}.", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, AssertionError, subprocess.CalledProcessError) as exc:
        print(f"Container checks failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
