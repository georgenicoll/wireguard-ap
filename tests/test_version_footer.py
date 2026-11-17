"""The version line at the foot of every page: which wireguard-ap commit and
which simple-metrics are running. Local tier (a version file the test writes,
and the real smweb); the Pi tier checks what wga really deployed.
"""
import json
import re
import subprocess

import pytest

from support import LOCAL_PSK, REPO, Web

FULL = "0123456789abcdef0123456789abcdef01234567"
REPO_URL = "https://github.com/georgenicoll/wireguard-ap"


@pytest.fixture
def web(local_server) -> Web:
    web = Web(local_server.base_url, LOCAL_PSK)
    assert web.login().status_code == 303
    return web


@pytest.fixture
def version_file(local_server):
    path = local_server.state_dir / "wireguard-ap.version"
    yield path
    path.unlink(missing_ok=True)


def footer(html: str) -> str:
    return re.search(r'<footer id="version-footer">(.*?)</footer>', html, re.S).group(1)


@pytest.mark.local
class TestVersionFooter:
    def write(self, path, **info):
        path.write_text(json.dumps({"commit": FULL, "date": "2026-11-17", "dirty": False,
                                    "url": REPO_URL, **info}))

    def test_the_commit_and_date_link_to_github(self, web, version_file):
        self.write(version_file)
        text = footer(web.get("/").text)
        assert f'href="{REPO_URL}/commit/{FULL}"' in text
        assert ">0123456 (2026-11-17)</a>" in text

    def test_simple_metrics_shows_the_installed_version_and_links_to_its_release(self, web, version_file):
        self.write(version_file)
        text = footer(web.get("/").text)
        m = re.search(r'simple-metrics <a href="https://github.com/georgenicoll/simple-metrics/'
                      r'releases/tag/v([\d.]+)"[^>]*>([\d.]+)</a>', text)
        assert m and m.group(1) == m.group(2), text

    def test_a_dirty_deploy_says_so_and_does_not_link_to_a_commit_it_isnt(self, web, version_file):
        self.write(version_file, dirty=True)
        text = footer(web.get("/").text)
        assert "0123456-dirty (2026-11-17)" in text and "/commit/" not in text

    @pytest.mark.parametrize("content", [None, "", "not json", "{}", '{"commit": ""}'])
    def test_a_missing_or_unusable_file_says_unknown_and_the_page_still_works(self, web, version_file, content):
        if content is None:
            version_file.unlink(missing_ok=True)
        else:
            version_file.write_text(content)
        r = web.get("/")
        assert r.status_code == 200 and "wireguard-ap unknown" in footer(r.text)

    def test_a_repo_url_that_is_not_https_is_not_linked(self, web, version_file):
        self.write(version_file, url="javascript:alert(1)")
        assert "href=" not in footer(web.get("/").text).split("simple-metrics")[0]

    def test_nothing_in_the_file_can_inject_markup(self, web, version_file):
        self.write(version_file, date="<script>alert(1)</script>", commit="abcdef1")
        text = footer(web.get("/").text)
        assert "<script>" not in text and "&lt;script&gt;" in text

    @pytest.mark.parametrize("path", ["/", "/diagnostics", "/metrics", "/manage"])
    def test_it_is_on_every_page(self, web, version_file, path):
        self.write(version_file)
        assert "0123456" in footer(web.get(path).text)

    def test_it_is_on_the_login_page_too(self, local_server, version_file):
        self.write(version_file)
        r = Web(local_server.base_url, LOCAL_PSK).get("/login")
        assert "0123456" in footer(r.text)


@pytest.mark.usefixtures("target")
class TestVersionFooterInTheBrowser:
    @pytest.mark.parametrize("size", [(1280, 900), (390, 500), (320, 300)])
    def test_it_is_always_at_the_bottom_of_the_window_and_small(self, page, target, size):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.goto(target.base_url + "/diagnostics")   # a long page
        page.wait_for_selector("#version-footer")
        box = page.locator("#version-footer").bounding_box()
        assert abs(box["y"] + box["height"] - size[1]) < 1.5, "flush with the bottom"
        assert box["height"] <= 60, "takes little room"
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(200)
        box = page.locator("#version-footer").bounding_box()
        assert abs(box["y"] + box["height"] - size[1]) < 1.5, "and stays there when scrolled"

    def test_nothing_is_hidden_behind_it_at_the_end_of_the_page(self, page, target):
        page.set_viewport_size({"width": 390, "height": 500})
        page.goto(target.base_url + "/diagnostics")
        page.wait_for_selector("#version-footer")
        page.mouse.wheel(0, 20000)
        page.wait_for_timeout(200)
        footer_top = page.locator("#version-footer").bounding_box()["y"]
        main = page.locator("main").bounding_box()
        assert main["y"] + main["height"] <= footer_top + 1


@pytest.mark.smoke
class TestPiVersionFooter:
    def test_the_pi_says_which_commit_was_deployed(self, pi_web):
        text = footer(pi_web.get("/").text)
        assert re.search(r"wireguard-ap (<a [^>]*>)?[0-9a-f]{7}(-dirty)? \(\d{4}-\d\d-\d\d\)", text), text
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=7", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if head:
            assert head in text or "-dirty" in text, f"deployed from a different commit than HEAD ({head})"

    def test_the_pi_says_which_simple_metrics_is_installed(self, pi_web):
        text = footer(pi_web.get("/").text)
        pinned = re.search(r"SIMPLE_METRICS_VERSION=v([\d.]+)", (REPO / "simple-metrics.pin").read_text())
        assert pinned and f"releases/tag/v{pinned.group(1)}" in text, text
