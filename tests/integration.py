# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pytest",
#   "httpx",
#   "itsdangerous",
#   "playwright",
# ]
# ///
"""Integration tests for wireguard-ap. Run them with ./wga test (see the
README's "Integration tests" section); this file is also a plain uv script:

    uv run tests/integration.py [tier ...] [pytest args ...]

Tiers (markers):
  local   no Pi needed: the diagnostics scripts' argument handling (incl.
          injection attempts) and the UI in a browser, against a throwaway
          copy of the web app on a spare port.
  smoke   the deployed Pi, over SSH and HTTPS: services, routing, file
          permissions, leftovers from the old naming, every read-only page and
          diagnostic, login/session security.
  ui      the same browser checks as the local tier, against the real Pi.
  reboot  OPT-IN: clicks Restart in the UI and checks the Pi comes back
          healthy. Never part of the default run.

No tier = local + smoke + ui. "all" = those plus reboot. The Pi tiers read
their settings (pi_host, pi_user, ssh_private_key_path, mode, psk) from the
tfvars file WGA_CONFIG points at - the same one ./wga uses. The password is
only ever held in memory: never printed, logged or written anywhere.
"""
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
TIERS = ("local", "smoke", "ui", "reboot")


def main() -> int:
    args = sys.argv[1:]
    chosen = [a for a in args if a in (*TIERS, "all")]
    rest = [a for a in args if a not in (*TIERS, "all")]
    if "all" in chosen:
        expr = " or ".join(TIERS)
    elif chosen:
        expr = " or ".join(chosen)
    else:
        expr = "local or smoke or ui"
    return pytest.main([
        str(TESTS), "-m", expr, "-q", "-ra", "--tb=short", "-p", "no:cacheprovider",
        "-W", "ignore::pytest.PytestUnknownMarkWarning",
        *rest,
    ])


if __name__ == "__main__":
    sys.exit(main())
