"""OPT-IN: the Restart button, end to end. Never part of the default run.
Tier: reboot.
"""
import subprocess
import time

import httpx
import pytest

from support import pi_health_problems


@pytest.mark.reboot
def test_restart_button_reboots_the_pi_and_everything_comes_back(pi, browser):
    before = pi.out("cat /proc/sys/kernel/random/boot_id").strip()
    assert before
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
