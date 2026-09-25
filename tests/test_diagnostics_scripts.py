"""The diagnostics scripts on their own: argument handling (including
injection attempts), the registry protocol and the generated sudoers file.
Tier: local.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from support import SCRIPTS


def script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPTS / "diagnostics.sh"), *args],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.local
class TestDiagnosticsScript:
    def test_show_commands_protocol(self):
        r = script("--show-commands")
        assert r.returncode == 0
        commands = {}
        for line in r.stdout.splitlines():
            sig, _, desc = line.partition("|")
            words = sig.split()
            commands[words[0]] = words[1:]
            assert desc.strip(), f"no description: {line!r}"
        expected = {"ping", "traceroute", "dig", "ip-addr-list", "ip-route-list",
                    "ip-route-get", "ap-clients", "service-status", "logs",
                    "uname", "free", "os-release", "wg-status"}  # wg-status comes from diagnostics_sudo.sh
        assert expected <= set(commands), expected - set(commands)
        assert commands["dig"][0] == "name" and commands["dig"][1] == "server?"
        assert commands["dig"][2].startswith("type?=A,")
        assert commands["ping"] == ["host"] and commands["ip-addr-list"] == []
        assert commands["uname"] == [] and commands["free"] == []
        assert commands["os-release"] == []

    def test_free_shows_memory_and_swap_in_human_readable_units(self):
        r = script("--run-command", "free")
        assert r.returncode == 0
        assert r.stdout.split()[:2] == ["total", "used"] and "Mem:" in r.stdout and "Swap:" in r.stdout

    def test_os_release_prints_the_os_release_file(self):
        r = script("--run-command", "os-release")
        assert r.returncode == 0
        assert r.stdout == Path("/etc/os-release").read_text()

    def test_uname_prints_the_full_uname_a_line(self):
        r = script("--run-command", "uname")
        assert r.returncode == 0
        assert r.stdout.strip() == subprocess.run(
            ["uname", "-a"], capture_output=True, text=True).stdout.strip()

    def test_root_only_commands_are_not_defined_in_diagnostics_sh(self):
        assert "wg-status" not in (SCRIPTS / "diagnostics.sh").read_text().replace(
            "wg-status, ap-clients", "")
        r = subprocess.run([str(SCRIPTS / "diagnostics_sudo.sh"), "--show-commands"],
                           capture_output=True, text=True)
        assert [l.split()[0] for l in r.stdout.splitlines()] == ["wg-status"]

    @pytest.mark.parametrize("bad", [
        "127.0.0.1;touch {m}", "$(touch {m})", "`touch {m}`", "127.0.0.1\ntouch {m}",
        "127.0.0.1 && touch {m}", "127.0.0.1|touch {m}", "-f", "--help", "-c1",
        "127.0.0.1 -c1", "", " ",
    ])
    @pytest.mark.parametrize("cmd", ["ping", "traceroute", "ip-route-get"])
    def test_host_params_reject_injection(self, cmd, bad, tmp_path):
        marker = tmp_path / "pwned"
        value = bad.format(m=marker)
        r = script("--run-command", cmd, value)
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert not marker.exists()

    @pytest.mark.parametrize("name,server,type_", [
        ("example.com", "-x", ""), ("example.com", "1.1.1.1 +tcp", ""),
        ("-x", "", ""), ("example.com;id", "", ""), ("example.com", "$(id)", ""),
        ("example.com", "", "BOGUS"), ("example.com", "", "A,MX"),
        ("example.com", "", "A;id"),
    ])
    def test_dig_params_are_validated(self, name, server, type_):
        r = script("--run-command", "dig", name, server, type_)
        assert r.returncode == 2, (r.returncode, r.stderr)

    def test_dig_needs_a_name_but_not_the_optionals(self):
        assert "name is required" in script("--run-command", "dig", "", "", "").stderr
        r = script("--run-command", "dig", "example.com", "", "")
        assert r.returncode in (0, 127)  # 127 = dig not installed here

    @pytest.mark.parametrize("args", [
        ["nope", ], ["ping$(touch x)", "127.0.0.1"], ["x[$(id)]"], ["PARAMS[a]"], [""],
    ])
    def test_unknown_command_names_are_refused(self, args):
        assert script("--run-command", *args).returncode == 2

    @pytest.mark.parametrize("unit,lines", [
        ("ssh", "50"), ("dnsmasq", "7"), ("dnsmasq,wg-quick@wg0", "50"),
        ("dnsmasq", "50,100"), ("", "50"), ("dnsmasq", ""),
        ("mnh-ap-nat,mnh-ap-webapp", "50"), ("*", "50"),
    ])
    def test_choice_params_only_accept_listed_values(self, unit, lines):
        assert script("--run-command", "logs", unit, lines).returncode == 2

    @pytest.mark.parametrize("args", [
        ("ping",), ("ping", "127.0.0.1", "extra"), ("ip-addr-list", "x"),
        ("logs", "dnsmasq"),
    ])
    def test_wrong_argument_count_is_refused(self, args):
        assert script("--run-command", *args).returncode == 2

    def test_sudo_script_only_knows_its_own_commands(self):
        for args in (["ping", "127.0.0.1"], ["nope"], ["wg-status$(id)"]):
            r = subprocess.run([str(SCRIPTS / "diagnostics_sudo.sh"), "--run-command", *args],
                               capture_output=True, text=True)
            assert r.returncode == 2 and "unknown command" in r.stderr

    @pytest.mark.parametrize("name", sorted(p.name for p in SCRIPTS.glob("*.sh")))
    def test_script_syntax(self, name):
        assert subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode == 0

    def test_generated_sudoers_for_the_web_user_is_valid(self, tmp_path):
        visudo = shutil.which("visudo")
        if not visudo:
            pytest.skip("visudo not installed")
        src = (SCRIPTS / "setup_webapp.sh").read_text()
        body = re.search(r'cat >"\$SUDOERS_TMP" <<EOF\n(.*?)\nEOF', src, re.S).group(1)
        rendered = body.replace("${WEB_USER}", "mnh-web").replace("${SCRIPTS_DIR}", "/home/x")
        f = tmp_path / "sudoers"
        f.write_text(rendered + "\n")
        r = subprocess.run([visudo, "-cf", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        # Fixed-argument rules must stay fixed: nothing broader than intended.
        assert 'setup_ap.sh dual' in rendered and 'setup_ap.sh uplink 5' in rendered
        assert 'setup_ap.sh *' not in rendered and 'ALL=(ALL)' not in rendered
        assert rendered.index("SETENV:") > rendered.index("uplink_wifi.sh")
