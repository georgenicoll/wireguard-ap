"""OPT-IN: the Restart button, end to end. Never part of the default run.
Tier: reboot.
"""
import json
import subprocess
import time

import httpx
import pytest

from support import pi_health_problems


def latest_timestamp(pi) -> int:
    r = pi.ssh("smq --json latest")
    return int(json.loads(r.stdout)["timestamp"]) if r.returncode == 0 else 0


@pytest.mark.reboot
def test_restart_button_reboots_the_pi_and_everything_comes_back(pi, browser):
    before = pi.out("cat /proc/sys/kernel/random/boot_id").strip()
    assert before
    # The metrics history must come back too. It's only flushed to disk
    # every minute - simple-metrics doesn't force a flush on shutdown, by
    # design (see its persist.rs) - so the marker needs to be safely older
    # than that window. Bounded to records from this test run onwards
    # (--from start_ms), not simple-metrics' whole retained history: an
    # unbounded --points query downsamples across everything it's ever
    # kept, which can land the marker on a bucket from long before this
    # run - including a gap from a past test's own restart - and then
    # assert on data that was never going to be there.
    start_ms = latest_timestamp(pi) or int(time.time() * 1000)
    deadline = time.time() + 300
    marker = 0
    while time.time() < deadline:
        info = json.loads(
            pi.out(f"smq --json read --from {start_ms} --metric load1") or "{}"
        )
        stamps = info.get("timestamps") or []
        if stamps and stamps[-1] - stamps[0] >= 150_000:
            marker = stamps[0] + 10_000
            break
        time.sleep(10)
    assert marker, "the collector never had two minutes of history to keep"
    ctx = browser.new_context(ignore_https_errors=True)
    assert ctx.request.post(pi.base_url + "/login", form={"password": pi.psk},
                            max_redirects=0).status == 303
    pg = ctx.new_page()
    pg.goto(pi.base_url + "/manage")
    pg.click("text=Restart the Pi")
    pg.wait_for_selector("#restart-dialog[open]")
    pg.click('#restart-dialog a[hx-post="/restart"]')
    pg.wait_for_url("**/shutting-down?action=restart", timeout=15000)
    pg.wait_for_url("**/login", timeout=300000)  # the page polls until the Pi is back
    ctx.close()

    deadline = time.time() + 180
    after = ""
    while time.time() < deadline and (not after or after == before):
        try:
            r = pi.ssh("cat /proc/sys/kernel/random/boot_id", timeout=15)
            after = r.stdout.strip() if r.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            after = ""
        time.sleep(3)
    assert after and after != before, "the Pi never reported a new boot id"

    problems = ["not checked"]
    deadline = time.time() + 180  # boot-time services take a while to settle
    while time.time() < deadline:
        problems = pi_health_problems(pi)
        if not problems:
            break
        time.sleep(5)
    assert not problems, problems
    assert httpx.get(pi.base_url + "/healthz", verify=False, timeout=10).status_code == 200

    # The history from before the restart is still there (the newest minute may
    # not be), with the restart itself as a gap after it.
    r = pi.ssh(f"smq --json read --from {marker - 60_000} --to {marker + 60_000} --metric load1")
    assert r.returncode == 0, r.stderr
    kept = json.loads(r.stdout)["timestamps"]
    assert kept, "the metrics history did not survive the restart"
    assert min(kept) < marker + 60_000
