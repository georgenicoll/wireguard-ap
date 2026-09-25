"""The Diagnostics page in a browser, against a throwaway local server (tier
local) and the real Pi (tier ui).
"""
import time

import pytest


def visible_cards(page):
    return page.locator("article[data-command]:visible")


@pytest.mark.usefixtures("target")
class TestDiagnosticsUI:
    def test_titles_and_nav_order(self, page, target):
        assert page.title() == "mnh-ap - Diagnostics"
        nav = page.locator("nav a").all_inner_texts()
        assert nav.index("Diagnostics") < nav.index("Manage")
        page.goto(target.base_url + "/")
        assert page.title() == "mnh-ap"

    def test_starts_with_a_placeholder_and_no_card(self, page):
        sel = page.locator("#diagnostic-select")
        assert "Choose a diagnostic" in sel.evaluate("s => s.options[s.selectedIndex].text")
        assert sel.evaluate("s => s.options[0].hidden") is True  # not offered in the list
        assert visible_cards(page).count() == 0

    def test_picker_sits_next_to_its_label_and_is_only_as_wide_as_needed(self, page):
        sel = page.locator("#diagnostic-select").bounding_box()
        label = page.locator("label[for=diagnostic-select]").bounding_box()
        assert abs(sel["y"] - label["y"]) < 20 and sel["width"] < 400

    def test_choosing_shows_only_that_card_with_description_and_no_title(self, page):
        page.locator("#diagnostic-select").select_option("ping")
        cards = visible_cards(page)
        assert cards.count() == 1 and cards.first.get_attribute("data-command") == "ping"
        assert "Ping a host" in cards.first.inner_text()
        assert cards.first.locator("header").count() == 0

    def test_run_button_is_at_the_right_of_the_card(self, page):
        page.locator("#diagnostic-select").select_option("ping")
        card = visible_cards(page).first.bounding_box()
        btn = visible_cards(page).first.locator("button").bounding_box()
        assert (card["x"] + card["width"]) - (btn["x"] + btn["width"]) < 60 and btn["width"] < 200

    def test_a_required_field_must_be_filled_before_anything_runs(self, page):
        page.locator("#diagnostic-select").select_option("ping")
        visible_cards(page).first.locator("button").click()
        assert not page.locator("#output-dialog").evaluate("d => d.open")

    def test_optional_and_choice_fields_render_correctly(self, page):
        sel = page.locator("#diagnostic-select")
        sel.select_option("dig")
        dig = visible_cards(page).first
        assert dig.locator("input[required]").count() == 1
        assert dig.locator("input[type=text]:not([required])").count() == 1
        assert dig.locator("select option").first.get_attribute("value") == ""
        assert dig.inner_text().count("(optional)") == 2
        sel.select_option("logs")
        assert visible_cards(page).first.locator("select[required]").count() == 2

    @pytest.mark.parametrize("close", ["button", "escape"])
    def test_ping_runs_until_the_dialog_is_closed_then_stops(self, page, target, close):
        from playwright.sync_api import expect

        page.locator("#diagnostic-select").select_option("ping")
        card = visible_cards(page).first
        card.locator("input[type=text]").fill("127.0.0.1")
        card.locator("button").click()
        expect(page.locator("#output-dialog")).to_be_visible()
        assert page.locator("#output-heading").inner_text() == "ping output"
        expect(page.locator("#job-output")).to_contain_text("bytes from", timeout=15000)
        seen = page.locator("#job-output").inner_text().count("bytes from")
        time.sleep(3)
        assert page.locator("#job-output").inner_text().count("bytes from") > seen
        assert target.ping_running()
        if close == "button":
            page.click("#output-dialog footer a")
        else:
            page.keyboard.press("Escape")
        deadline = time.time() + 10
        while time.time() < deadline and target.ping_running():
            time.sleep(1)
        assert not target.ping_running()
        assert page.locator("#job-output").inner_text().strip() == ""
        # ...and it can be run again straight away.
        card.locator("button").click()
        expect(page.locator("#job-output")).to_contain_text("bytes from", timeout=15000)
        page.keyboard.press("Escape")

    def test_long_output_follows_at_the_bottom(self, page):
        from playwright.sync_api import expect

        page.locator("#diagnostic-select").select_option("ip-addr-list")
        visible_cards(page).first.locator("button").click()
        expect(page.locator("#job-output")).to_contain_text("[exit code", timeout=15000)
        d = page.locator("#job-output").evaluate(
            "e => ({top: e.scrollTop, h: e.scrollHeight, c: e.clientHeight})")
        assert d["h"] - d["top"] - d["c"] <= 4, d  # at the bottom (whether or not it overflowed)

    def test_reload_keeps_the_cards_in_step_with_the_picker(self, page):
        page.locator("#diagnostic-select").select_option("traceroute")
        page.reload()
        chosen = page.locator("#diagnostic-select").evaluate("s => s.value")
        assert visible_cards(page).count() == (1 if chosen else 0)
