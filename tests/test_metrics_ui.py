"""The Metrics page in a browser: against a throwaway local server with a fake
collector (tier local) and the real Pi with its real collector (tier ui).
"""
import re

import pytest


def open_metrics(page, target):
    """Goes to the Metrics page and waits for its first chart. Returns the
    JavaScript errors the page raises (a list that fills as they happen)."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(target.base_url + "/metrics")
    page.wait_for_selector("#chart canvas")
    return errors


def status(page) -> str:
    return page.locator("#chart-status").inner_text()


def choose(page, selector: str, value: str) -> None:
    """Picks an option and waits for the chart data that follows."""
    with page.expect_response(re.compile(r"/metrics/data\?")):
        page.locator(selector).select_option(value)


@pytest.mark.usefixtures("target")
class TestMetricsUI:
    def test_title_and_nav(self, page, target):
        open_metrics(page, target)
        assert page.title() == "mnh-ap - Metrics"
        nav = page.locator("nav a").all_inner_texts()
        assert nav.index("Diagnostics") < nav.index("Metrics") < nav.index("Manage")
        assert page.locator("nav a", has_text="Metrics").get_attribute("aria-disabled") == "true"

    def test_a_chart_is_drawn_with_no_script_errors(self, page, target):
        errors = open_metrics(page, target)
        page.wait_for_function("document.getElementById('chart-status').textContent.includes('points')")
        assert page.locator("#chart canvas").count() >= 1
        assert "Updated" in status(page)
        assert errors == []

    def test_the_chart_fits_its_container(self, page, target):
        open_metrics(page, target)
        box = page.locator("#chart").bounding_box()
        plot = page.locator("#chart .uplot").bounding_box()
        assert 0 < plot["width"] <= box["width"] + 1

    def test_the_chart_follows_the_window_width(self, page, target):
        open_metrics(page, target)
        page.set_viewport_size({"width": 500, "height": 900})
        page.wait_for_function(
            "document.querySelector('#chart .uplot').getBoundingClientRect().width <= 500")
        assert page.evaluate("document.documentElement.scrollWidth") <= 500

    def test_the_pickers_sit_next_to_their_labels(self, page, target):
        open_metrics(page, target)
        for select, label in (("#metric-select", "metric-select"), ("#range-select", "range-select")):
            s = page.locator(select).bounding_box()
            l = page.locator(f"label[for={label}]").bounding_box()
            assert abs(s["y"] - l["y"]) < 20 and s["width"] < 500

    def test_metrics_are_grouped_and_the_ranges_offered(self, page, target):
        open_metrics(page, target)
        assert page.locator("#metric-select optgroup").count() >= 1
        assert page.locator("#range-select option").all_inner_texts() == ["1h", "6h", "24h", "7d"]
        assert page.locator("#range-select").input_value() == "1h"

    def test_choosing_another_metric_loads_that_metric(self, page, target):
        open_metrics(page, target)
        options = page.locator("#metric-select option").evaluate_all("o => o.map(x => x.value)")
        other = next(v for v in options if v != page.locator("#metric-select").input_value())
        with page.expect_response(re.compile(r"/metrics/data\?")) as info:
            page.locator("#metric-select").select_option(other)
        assert f"metric={other}" in info.value.url
        assert info.value.status == 200
        page.wait_for_function("document.getElementById('chart-status').textContent.includes('points')")

    def test_choosing_a_range_loads_that_range(self, page, target):
        open_metrics(page, target)
        with page.expect_response(re.compile(r"/metrics/data\?")) as info:
            page.locator("#range-select").select_option("6h")
        assert "range=6h" in info.value.url
        assert page.locator("#chart canvas").count() >= 1

    def test_the_choice_is_remembered_after_a_reload(self, page, target):
        open_metrics(page, target)
        options = page.locator("#metric-select option").evaluate_all("o => o.map(x => x.value)")
        other = options[-1]
        choose(page, "#metric-select", other)
        choose(page, "#range-select", "24h")
        page.reload()
        page.wait_for_selector("#chart canvas")
        assert page.locator("#metric-select").input_value() == other
        assert page.locator("#range-select").input_value() == "24h"

    def test_the_chart_refreshes_by_itself(self, page, target):
        open_metrics(page, target)
        with page.expect_response(re.compile(r"/metrics/data\?"), timeout=15000) as info:
            pass  # nothing is done: the page asks again on its own
        assert info.value.status == 200

    @pytest.mark.parametrize("path", ["/", "/diagnostics", "/metrics", "/manage"])
    def test_log_out_works_from_every_page(self, page, target, path):
        page.goto(target.base_url + path)
        page.get_by_role("button", name="Log out").click()
        page.wait_for_url(re.compile(r"/login$"))
        # And really logged out: the page is now refused.
        page.goto(target.base_url + "/metrics")
        assert page.url.endswith("/login")


@pytest.mark.usefixtures("target")
class TestMetricsUIWithAFakeCollector:
    """Needs a collector that can be made to misbehave, so local only."""

    @pytest.fixture(autouse=True)
    def only_local(self, target, collector):
        if target.name != "local":
            pytest.skip("needs the fake collector")

    def test_the_page_says_so_when_there_is_no_collector(self, page, target, collector):
        collector.mode = "down"
        page.goto(target.base_url + "/metrics")
        assert "No metrics available" in page.locator("main").inner_text()
        assert page.locator("#chart").count() == 0
        # The navigation still works, including logging out.
        page.get_by_role("button", name="Log out").click()
        page.wait_for_url(re.compile(r"/login$"))

    def test_a_collector_failing_while_watching_is_reported_and_recovers(self, page, target, collector):
        open_metrics(page, target)
        collector.mode = "error"
        choose(page, "#range-select", "6h")
        page.wait_for_function("document.getElementById('chart-status').classList.contains('error')")
        assert status(page) != ""
        collector.mode = "ok"
        choose(page, "#range-select", "1h")
        page.wait_for_function("!document.getElementById('chart-status').classList.contains('error')")
        assert "points" in status(page)

    def test_a_gap_in_the_data_does_not_break_the_chart(self, page, target, collector):
        errors = open_metrics(page, target)  # the fake data has one gap
        assert errors == []
        assert page.locator("#chart canvas").count() >= 1

    def test_the_page_asks_for_at_most_600_points_with_extremes(self, page, target, collector):
        open_metrics(page, target)
        asked = [r for r in collector.requests if r.get("op") == "read"]
        assert asked and asked[-1]["max_points"] == 600 and asked[-1]["extremes"] is True
