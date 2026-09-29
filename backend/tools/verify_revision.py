"""Small read-only deployment gate for .update; checks the live process, not served HTML."""
import argparse
import json
import re
import sys
from urllib.request import urlopen


def revision_at(base: str) -> str:
    with urlopen(base.rstrip("/") + "/api/health", timeout=6) as response:
        if response.status != 200:
            raise ValueError("health HTTP %s" % response.status)
        data = json.load(response)
    if not data.get("ok"):
        raise ValueError("health is not ready")
    return str(data.get("source_revision") or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--public", default="https://image.myil.top")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected) or not 1 <= args.port <= 65535:
        parser.error("expected must be a full Git SHA and port must be valid")
    for label, base in (("local", "http://127.0.0.1:%d" % args.port),
                        ("public", args.public)):
        try:
            revision = revision_at(base)
        except Exception as exc:  # diagnose unavailable endpoint without exposing URLs/secrets
            print("%s revision check failed: %s" % (label, type(exc).__name__))
            return 1
        if revision != args.expected:
            print("%s process revision mismatch: expected=%s actual=%s" %
                  (label, args.expected[:12], revision[:12] or "missing"))
            return 1
    print("DEPLOY_REVISION_OK %s local=matched public=matched" % args.expected[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
