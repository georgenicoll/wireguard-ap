"""How the metrics collector gets onto the Pi, checked without a Pi: the fetch
script (download, verify, cache), the pin file, the systemd unit that
setup_metrics.sh writes, and the rule that upgrading the collector must never
re-run the access point's setup. Tier: local.
"""
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from support import REPO, SCRIPTS

FETCH = REPO / "tools" / "fetch_simple_metrics.sh"
SETUP = SCRIPTS / "setup_metrics.sh"
TARGET = "aarch64-unknown-linux-musl"
FAKE_BINARY = b"#!/bin/sh\necho 'simple-metrics 9.9.9'\n"


# --------------------------------------------------------------------------
# A fake GitHub release, served from a directory through file:// URLs
# --------------------------------------------------------------------------
class Release:
    """A directory laid out like GitHub's release downloads, plus a pin file
    and cache directory to go with it."""

    def __init__(self, root: Path, version: str = "v9.9.9"):
        self.root = root
        self.version = version
        self.asset = f"simple-metrics-{version}-{TARGET}.tar.gz"
        self.downloads = root / "downloads"
        self.cache = root / "cache"
        self.pin = root / "simple-metrics.pin"
        (self.downloads / version).mkdir(parents=True)
        self.publish(FAKE_BINARY)

    @property
    def archive_path(self) -> Path:
        return self.downloads / self.version / self.asset

    def publish(self, binary: bytes) -> str:
        """Writes the release archive and returns its SHA-256."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            member = tarfile.TarInfo(f"simple-metrics-{self.version}-{TARGET}/simple-metrics")
            member.size, member.mode = len(binary), 0o755
            tar.addfile(member, io.BytesIO(binary))
        data = buffer.getvalue()
        self.archive_path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def write_pin(self, sha256: str, version: str | None = None) -> None:
        self.pin.write_text(
            f"# a comment\nSIMPLE_METRICS_VERSION={version or self.version}\n"
            f"SIMPLE_METRICS_SHA256={sha256}\n")

    def run(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(FETCH)], capture_output=True, text=True, timeout=60,
            env={
                "PATH": os.environ["PATH"], "HOME": str(self.root),
                "SIMPLE_METRICS_PIN_FILE": str(self.pin),
                "SIMPLE_METRICS_CACHE_DIR": str(self.cache),
                "SIMPLE_METRICS_RELEASE_URL": f"file://{self.downloads}",
                **env,
            })


@pytest.fixture
def release(tmp_path) -> Release:
    release = Release(tmp_path)
    release.write_pin(hashlib.sha256(release.archive_path.read_bytes()).hexdigest())
    return release


@pytest.mark.local
class TestFetchScript:
    def test_downloads_verifies_and_prints_the_binary_path(self, release):
        r = release.run()
        assert r.returncode == 0, r.stderr
        binary = Path(r.stdout.strip())
        assert r.stdout.count("\n") == 1, "stdout is only the path"
        assert binary == release.cache / "v9.9.9" / "simple-metrics"
        assert binary.read_bytes() == FAKE_BINARY
        assert stat.S_IMODE(binary.stat().st_mode) == 0o755
        assert "downloading" in r.stderr

    def test_uses_the_cache_when_it_still_matches(self, release):
        assert release.run().returncode == 0
        shutil.rmtree(release.downloads)  # nothing to download from any more
        r = release.run()
        assert r.returncode == 0, r.stderr
        assert "downloading" not in r.stderr

    def test_refuses_a_download_that_does_not_match_the_pin(self, release):
        release.write_pin("0" * 64)
        r = release.run()
        assert r.returncode != 0
        assert "does not match the pinned checksum" in r.stderr
        assert "has NOT been used" in r.stderr
        assert r.stdout == ""
        # Nothing unverified is left where it could be picked up later.
        assert not (release.cache / "v9.9.9" / "simple-metrics").exists()
        assert not list((release.cache / "v9.9.9").glob("*.tar.gz"))
        assert not list((release.cache / "v9.9.9").glob(".download.*"))

    def test_a_release_altered_after_pinning_is_caught(self, release):
        # The pin was taken from the original; the "release" is then replaced.
        release.publish(b"#!/bin/sh\necho evil\n")
        r = release.run()
        assert r.returncode != 0 and "does not match" in r.stderr
        assert r.stdout == ""

    def test_a_damaged_cached_archive_is_fetched_again(self, release):
        assert release.run().returncode == 0
        cached = release.cache / "v9.9.9" / release.asset
        cached.write_bytes(b"corrupt")
        r = release.run()
        assert r.returncode == 0, r.stderr
        assert "downloading" in r.stderr
        assert Path(r.stdout.strip()).read_bytes() == FAKE_BINARY

    def test_a_missing_release_is_a_clear_error(self, release):
        release.write_pin(hashlib.sha256(b"x").hexdigest(), version="v1.0.0")
        r = release.run()
        assert r.returncode != 0
        assert "could not download" in r.stderr

    @pytest.mark.parametrize("line", [
        "SIMPLE_METRICS_VERSION=latest", "SIMPLE_METRICS_VERSION=v1.2", "SIMPLE_METRICS_VERSION=",
        "SIMPLE_METRICS_VERSION=v1.2.3/../../x", "SIMPLE_METRICS_VERSION=v1.2.3;id",
        "SIMPLE_METRICS_VERSION=$(id)",
    ])
    def test_a_malformed_version_is_refused_before_anything_is_fetched(self, release, line):
        release.pin.write_text(f"{line}\nSIMPLE_METRICS_SHA256={'a' * 64}\n")
        r = release.run()
        assert r.returncode != 0 and "SIMPLE_METRICS_VERSION" in r.stderr
        assert not release.cache.exists() or not any(release.cache.rglob("*.tar.gz"))

    @pytest.mark.parametrize("sha", ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
    def test_a_malformed_checksum_is_refused(self, release, sha):
        release.pin.write_text(f"SIMPLE_METRICS_VERSION=v9.9.9\nSIMPLE_METRICS_SHA256={sha}\n")
        r = release.run()
        assert r.returncode != 0 and "SIMPLE_METRICS_SHA256" in r.stderr

    def test_a_missing_pin_file_is_a_clear_error(self, release):
        release.pin.unlink()
        r = release.run()
        assert r.returncode != 0 and "no pin file" in r.stderr

    def test_a_local_binary_can_be_used_and_says_it_skips_the_pin(self, release, tmp_path):
        binary = tmp_path / "mine"
        binary.write_bytes(FAKE_BINARY)
        binary.chmod(0o755)
        release.pin.unlink()  # not even needed in this mode
        r = release.run(SIMPLE_METRICS_BIN=str(binary))
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(binary)
        assert "WARNING" in r.stderr and "not the pinned release" in r.stderr

    def test_a_local_binary_that_is_not_there_is_refused(self, release, tmp_path):
        r = release.run(SIMPLE_METRICS_BIN=str(tmp_path / "nope"))
        assert r.returncode != 0 and "not an executable file" in r.stderr


@pytest.mark.local
class TestPinFile:
    def test_the_real_pin_is_well_formed(self):
        text = (REPO / "simple-metrics.pin").read_text()
        assert re.search(r"^SIMPLE_METRICS_VERSION=v\d+\.\d+\.\d+$", text, re.M)
        assert re.search(r"^SIMPLE_METRICS_SHA256=[0-9a-f]{64}$", text, re.M)

    def test_wga_hands_the_fetched_path_to_tofu(self):
        wga = (REPO / "wga").read_text()
        assert "tools/fetch_simple_metrics.sh" in wga
        assert "TF_VAR_simple_metrics_binary" in wga


# --------------------------------------------------------------------------
# setup_metrics.sh: the unit it writes
# --------------------------------------------------------------------------
def print_unit(tmp_path: Path, if_24="wlan0", if_5="wlan1", br="br-ap") -> subprocess.CompletedProcess:
    """Runs setup_metrics.sh --print-unit next to a fake env file. (It needs
    neither root nor a Pi: it only prints.)"""
    shutil.copy(SETUP, tmp_path / "setup_metrics.sh")
    (tmp_path / "wireguard-ap.env").write_text(f"IF_24='{if_24}'\nIF_5='{if_5}'\nBR='{br}'\n")
    return subprocess.run([str(tmp_path / "setup_metrics.sh"), "--print-unit"],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.local
class TestSetupMetricsScript:
    def test_the_script_is_valid_bash(self):
        assert subprocess.run(["bash", "-n", str(SETUP)], capture_output=True).returncode == 0

    def test_it_is_executable(self):
        assert os.access(SETUP, os.X_OK)

    def test_it_runs_as_its_own_user_with_the_web_apps_group(self, tmp_path):
        unit = print_unit(tmp_path).stdout
        assert "\nUser=mnh-metrics\n" in unit
        assert "\nGroup=mnh-web\n" in unit

    def test_the_socket_directory_is_closed_to_everyone_else(self, tmp_path):
        unit = print_unit(tmp_path).stdout
        assert "\nRuntimeDirectory=simple-metrics\n" in unit
        assert "\nRuntimeDirectoryMode=0750\n" in unit
        assert "\nUMask=0007\n" in unit

    def test_the_collector_is_started_with_the_agreed_settings(self, tmp_path):
        exec_start = re.search(r"^ExecStart=(.*)$", print_unit(tmp_path).stdout, re.M).group(1)
        assert exec_start.startswith("/usr/local/bin/simple-metrics ")
        assert "--socket /run/simple-metrics/simple-metrics.sock" in exec_start
        assert "--interval 5s" in exec_start and "--retention 7d" in exec_start

    def test_the_interfaces_come_from_the_env_file(self, tmp_path):
        exec_start = re.search(r"^ExecStart=(.*)$", print_unit(tmp_path, "wlx1", "wlx2", "br0").stdout, re.M).group(1)
        interfaces = re.findall(r"--interface (\S+)", exec_start)
        assert interfaces == ["eth0", "wlx1", "wlx2", "br0", "wg0"]

    def test_an_interface_listed_twice_is_only_charted_once(self, tmp_path):
        exec_start = re.search(r"^ExecStart=(.*)$", print_unit(tmp_path, "wlan1", "wlan1").stdout, re.M).group(1)
        assert re.findall(r"--interface (\S+)", exec_start) == ["eth0", "wlan1", "br-ap", "wg0"]

    @pytest.mark.parametrize("bad", ["wl an", "a/b", "x;id", "", "$(id)", "waytoolonginterfacename", "wlan0\nExecStart=evil"])
    def test_a_bad_interface_name_stops_everything(self, tmp_path, bad):
        r = print_unit(tmp_path, if_24=bad)
        assert r.returncode != 0, "must not carry on and write a unit without the interfaces"
        assert r.stdout == ""
        assert "bad interface name" in r.stderr

    @pytest.mark.parametrize("directive", [
        "NoNewPrivileges=yes", "CapabilityBoundingSet=", "ProtectSystem=strict", "ProtectHome=yes",
        "PrivateTmp=yes", "PrivateDevices=yes", "MemoryDenyWriteExecute=yes",
        "RestrictAddressFamilies=AF_UNIX", "IPAddressDeny=any", "SystemCallFilter=@system-service",
        "MemoryMax=128M", "Restart=on-failure",
    ])
    def test_the_unit_is_locked_down(self, tmp_path, directive):
        assert f"\n{directive}\n" in print_unit(tmp_path).stdout

    def test_the_unit_does_not_take_away_what_the_collector_needs(self, tmp_path):
        # It reads /proc and /sys and sees the machine's real interfaces.
        unit = print_unit(tmp_path).stdout
        for breaks_it in ("ProcSubset=", "PrivateNetwork=", "InaccessiblePaths=", "ProtectProc="):
            assert breaks_it not in unit, breaks_it

    @pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not installed")
    def test_systemd_accepts_the_unit(self, tmp_path):
        unit = tmp_path / "mnh-ap-metrics.service"
        unit.write_text(print_unit(tmp_path).stdout)
        r = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
        # The binary isn't installed on this machine, and that is the only
        # thing allowed to be wrong: no unknown directives, no bad values.
        problems = [l for l in (r.stdout + r.stderr).splitlines()
                    if l.strip() and "is not executable" not in l]
        assert problems == []


# --------------------------------------------------------------------------
# main.tf: the collector must not drag the access point's setup along
# --------------------------------------------------------------------------
@pytest.mark.local
class TestMainTf:
    def main_tf(self) -> str:
        return (REPO / "main.tf").read_text()

    def test_the_collector_is_not_in_the_list_that_triggers_ap_deploy(self):
        scripts = re.search(r"scripts = \[(.*?)\]", self.main_tf(), re.S).group(1)
        assert "setup_metrics.sh" not in scripts

    def test_ap_deploy_is_not_triggered_by_the_collector(self):
        ap_deploy = re.search(r'resource "terraform_data" "ap_deploy" \{(.*?)\n\}\n', self.main_tf(), re.S).group(1)
        assert "metrics" not in ap_deploy and "simple_metrics" not in ap_deploy

    def test_the_collector_is_its_own_resource_that_runs_after_ap_deploy(self):
        text = self.main_tf()
        block = re.search(r'resource "terraform_data" "metrics_deploy" \{(.*?)\n\}\n', text, re.S).group(1)
        assert "depends_on = [terraform_data.ap_deploy]" in block
        assert "count      = local.metrics_enabled ? 1 : 0" in block
        assert "filesha256(var.simple_metrics_binary)" in block
        assert "setup_metrics.sh" in block

    def test_the_variable_defaults_to_off_so_tofu_without_wga_still_works(self):
        variables = (REPO / "variables.tf").read_text()
        block = re.search(r'variable "simple_metrics_binary" \{(.*?)\n\}\n', variables, re.S).group(1)
        assert 'default     = ""' in block

    def test_the_setup_script_is_covered_by_the_deployed_scripts_checks(self):
        from support import DEPLOYED_SCRIPTS
        assert "setup_metrics.sh" in DEPLOYED_SCRIPTS
