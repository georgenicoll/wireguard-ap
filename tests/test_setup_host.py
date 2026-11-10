"""setup_host.sh runs on every apply, so it must not need the network - or
change what is installed - when the packages it wants are already there.
Checked against the real script with stand-in apt-get, systemctl, raspi-config
and rfkill, run as (fake) root in a user namespace so nothing on this machine
is touched. Tier: local.

What went wrong: the script ran a plain `apt-get install` of packages that
were already installed, which upgraded eight of them (hostapd, libssl,
openssl-provider-legacy, bind9...) in the middle of an unrelated apply, and
hung when one of the mirrors it then needed wasn't answering.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from support import SCRIPTS

SETUP = SCRIPTS / "setup_host.sh"

pytestmark = [
    pytest.mark.local,
    pytest.mark.skipif(shutil.which("unshare") is None, reason="needs unshare"),
]

STUB = """#!/usr/bin/env bash
# Records every call. For `apt-get -s install ...` it answers like apt would,
# as set by the test: STUB_PLAN is what it prints, STUB_PLAN_EXIT its status.
echo "$(basename "$0") $*" >> "$STUB_LOG"
if [[ $(basename "$0") == apt-get && " $* " == *" -s "* ]]; then
  printf '%s\\n' "${STUB_PLAN:-}"
  exit "${STUB_PLAN_EXIT:-0}"
fi
exit 0
"""


def run_setup(tmp_path: Path, plan: str = "", plan_exit: int = 0) -> list[str]:
    """Runs setup_host.sh and returns the commands it ran, in order."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("apt-get", "systemctl", "raspi-config", "rfkill"):
        (stubs / name).write_text(STUB)
        (stubs / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    script = tmp_path / "setup_host.sh"
    shutil.copy(SETUP, script)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "STUB_LOG": str(log),
           "STUB_PLAN": plan, "STUB_PLAN_EXIT": str(plan_exit)}
    r = subprocess.run(["unshare", "-r", str(script), "GB"], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return log.read_text().splitlines()


def apt_calls(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("apt-get")]


class TestPackages:
    def test_with_everything_installed_it_never_touches_the_network_or_upgrades_anything(self, tmp_path):
        calls = run_setup(tmp_path, plan="0 upgraded, 0 newly installed, 0 to remove.")
        assert [c for c in apt_calls(calls) if " -s " not in c] == [], \
            "no update and no install when nothing is missing"

    def test_the_check_is_a_simulation_that_does_not_upgrade(self, tmp_path):
        simulation = [c for c in apt_calls(run_setup(tmp_path)) if " -s " in c]
        assert len(simulation) == 1 and "--no-upgrade" in simulation[0]
        assert all(p in simulation[0] for p in ("hostapd", "dnsmasq", "wireguard-tools", "dnsutils"))

    def test_a_missing_package_is_installed_after_an_update_and_still_nothing_is_upgraded(self, tmp_path):
        calls = apt_calls(run_setup(tmp_path, plan="Inst traceroute (1.0 Debian:13 [arm64])"))
        real = [c for c in calls if " -s " not in c]
        assert len(real) == 2, real
        assert real[0].startswith("apt-get update")
        assert real[1].startswith("apt-get install") and "--no-upgrade" in real[1]

    def test_a_pi_that_cannot_even_work_out_a_plan_gets_the_packages(self, tmp_path):
        """A fresh Pi has no package indexes yet, so the simulation fails."""
        calls = apt_calls(run_setup(tmp_path, plan="E: Unable to locate package hostapd", plan_exit=100))
        real = [c for c in calls if " -s " not in c]
        assert [c.split()[1] for c in real] == ["update", "install"]

    def test_the_install_never_lacks_the_flag(self):
        text = SETUP.read_text()
        for line in text.splitlines():
            if "apt-get install" in line and not line.lstrip().startswith("#"):
                assert "--no-upgrade" in line, line


class TestTheRestStillRuns:
    def test_the_services_and_radio_settings_are_applied_either_way(self, tmp_path):
        for plan in ("", "Inst traceroute (1.0)"):
            sub = tmp_path / ("a" if plan else "b")
            sub.mkdir()
            calls = run_setup(sub, plan=plan)
            assert "systemctl enable dnsmasq" in calls
            assert "systemctl enable --now ssh" in calls
            assert "raspi-config nonint do_wifi_country GB" in calls
            assert "rfkill unblock wlan" in calls

    def test_it_is_executable_and_valid_bash(self):
        assert SETUP.stat().st_mode & stat.S_IXUSR
        assert subprocess.run(["bash", "-n", str(SETUP)]).returncode == 0
