"""Check the real Nginx response, including headers lost by location inheritance.

Usage: python scripts/check_frontend_http.py http://127.0.0.1:18080
No live upstream feed is required.
"""
import argparse
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def check(base, expected_carto=None):
    # Container entrypoint generates its configuration before Nginx listens.
    for attempt in range(30):
        try:
            with urlopen(base + "/", timeout=2) as response:
                assert response.status == 200
            break
        except (URLError, TimeoutError):
            if attempt == 29:
                raise
            time.sleep(0.2)

    for route, content_type in (("/", "text/html"), ("/sources.html", "text/html"),
                                ("/map-config.js", "javascript"),
                                ("/app.js", "javascript"), ("/styles.css", "text/css")):
        with urlopen(base + route, timeout=3) as response:
            assert content_type in response.headers["Content-Type"], route
            assert response.headers.get("X-Content-Type-Options") == "nosniff", route
            assert response.headers.get("X-Frame-Options") == "DENY", route
            assert "default-src 'self'" in response.headers.get("Content-Security-Policy", ""), route
            assert "no-store" in response.headers.get("Cache-Control", ""), route
            body = response.read()
            assert body, route
            if route == "/map-config.js":
                assert body in (b"window.MAP_CONFIG = {cartoEnabled: false};",
                                b"window.MAP_CONFIG = {cartoEnabled: true};"), body
                if expected_carto is not None:
                    expected = b"true" if expected_carto else b"false"
                    assert body == b"window.MAP_CONFIG = {cartoEnabled: " + expected + b"};", "Wrong basemap configuration"

    for route in ("/missing.js", "/tiles/not-a-valid-tile.png"):
        try:
            urlopen(base + route, timeout=3).close()
        except HTTPError as exc:
            assert exc.code == 404, (route, exc.code)
            assert exc.headers.get("X-Content-Type-Options") == "nosniff", route
            exc.close()
        else:
            raise AssertionError(f"Missing asset returned success: {route}")
    print("Frontend HTTP checks passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--expected-carto", choices=("true", "false"))
    args = parser.parse_args()
    check(args.base_url.rstrip("/"), None if args.expected_carto is None else args.expected_carto == "true")
