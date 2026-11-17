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
    page.context.clear_cookies(name="sm_layout")   # the layout is remembered in a cookie
    page.goto(target.base_url + "/metrics")
    page.wait_for_selector(".graph .chart canvas")
    return errors


def updated(page) -> str:
    return page.locator("#updated").inner_text()


def graphs(page) -> int:
    return page.locator(".graph").count()


def choose(page, selector, value: str) -> None:
    """Picks an option and waits for the chart data that follows."""
    with page.expect_response(re.compile(r"/metrics/series\?")):
        selector.select_option(value)


def add_graph(page) -> None:
    before = graphs(page)
    with page.expect_response(re.compile(r"/metrics/series\?")):
        page.locator("#add-graph").click()
    page.wait_for_function(f"document.querySelectorAll('.graph .chart canvas').length > {before}")


def other_metric(page, index: int) -> str:
    options = page.locator(".metric-select").nth(index).locator("option").evaluate_all(
        "o => o.map(x => x.value)")
    current = page.locator(".metric-select").nth(index).input_value()
    return next(v for v in options if v != current)


def choose_other(page, index: int) -> None:
    page.locator(".metric-select").nth(index).select_option(other_metric(page, index))


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
        page.wait_for_function("document.getElementById('updated').textContent.includes('Updated')")
        assert page.locator(".graph .chart canvas").count() >= 1
        assert errors == []

    def test_the_chart_fits_its_container(self, page, target):
        open_metrics(page, target)
        box = page.locator(".graph .chart").first.bounding_box()
        plot = page.locator(".graph .chart .uplot").first.bounding_box()
        assert 0 < plot["width"] <= box["width"] + 1

    def test_the_chart_follows_the_window_width(self, page, target):
        open_metrics(page, target)
        page.set_viewport_size({"width": 500, "height": 900})
        page.wait_for_function(
            "document.querySelector('.graph .chart .uplot').getBoundingClientRect().width <= 500")
        assert page.evaluate("document.documentElement.scrollWidth") <= 500

    def test_the_range_picker_sits_next_to_its_label(self, page, target):
        open_metrics(page, target)
        s = page.locator("#range-select").bounding_box()
        l = page.locator("label[for=range-select]").bounding_box()
        assert abs(s["y"] - l["y"]) < 20 and s["width"] < 500

    def test_metrics_are_grouped_and_the_ranges_offered(self, page, target):
        open_metrics(page, target)
        assert page.locator(".graph .metric-select optgroup").count() >= 1
        assert page.locator("#range-select option").all_inner_texts() == ["1h", "6h", "24h", "7d"]
        assert page.locator("#range-select").input_value() == "1h"

    def test_there_is_one_graph_that_cannot_be_removed(self, page, target):
        open_metrics(page, target)
        assert graphs(page) == 1
        assert not page.locator(".graph-delete").first.is_visible()

    def test_graphs_can_be_added_and_removed(self, page, target):
        errors = open_metrics(page, target)
        add_graph(page)
        add_graph(page)
        assert graphs(page) == 3
        assert page.locator(".graph-delete:visible").count() == 3
        page.locator(".graph-delete").nth(1).click()
        assert graphs(page) == 2
        page.locator(".graph-delete").first.click()
        assert graphs(page) == 1
        assert not page.locator(".graph-delete").first.is_visible()
        assert errors == []

    def test_all_the_graphs_are_fetched_in_one_request(self, page, target):
        open_metrics(page, target)
        add_graph(page)
        with page.expect_response(re.compile(r"/metrics/series\?")) as info:
            choose_other(page, 1)
        assert info.value.status == 200
        assert info.value.url.count("metric=") == 2

    def test_choosing_another_metric_loads_that_metric(self, page, target):
        open_metrics(page, target)
        other = other_metric(page, 0)
        with page.expect_response(re.compile(r"/metrics/series\?")) as info:
            page.locator(".metric-select").first.select_option(other)
        assert f"metric={other}" in info.value.url
        assert info.value.status == 200
        page.wait_for_function("document.getElementById('updated').textContent.includes('Updated')")

    def test_choosing_a_range_loads_that_range(self, page, target):
        open_metrics(page, target)
        with page.expect_response(re.compile(r"/metrics/series\?")) as info:
            page.locator("#range-select").select_option("6h")
        assert "range=6h" in info.value.url
        assert page.locator(".graph .chart canvas").count() >= 1

    def test_the_layout_is_remembered_after_a_reload(self, page, target):
        open_metrics(page, target)
        add_graph(page)
        other = other_metric(page, 1)
        choose(page, page.locator(".metric-select").nth(1), other)
        choose(page, page.locator("#range-select"), "24h")
        page.reload()
        page.wait_for_selector(".graph .chart canvas")
        assert graphs(page) == 2
        assert page.locator(".metric-select").nth(1).input_value() == other
        assert page.locator("#range-select").input_value() == "24h"

    def test_the_charts_refresh_by_themselves(self, page, target):
        open_metrics(page, target)
        with page.expect_response(re.compile(r"/metrics/series\?"), timeout=15000) as info:
            pass  # nothing is done: the page asks again on its own
        assert info.value.status == 200

    def test_dragging_across_a_chart_selects_that_period_for_every_graph(self, page, target):
        errors = open_metrics(page, target)
        add_graph(page)
        box = page.locator(".graph .u-over").first.bounding_box()
        with page.expect_response(re.compile(r"/metrics/series\?.*from=\d+&to=\d+")):
            page.mouse.move(box["x"] + box["width"] * 0.3, box["y"] + 40)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + 40, steps=6)
            page.mouse.up()
        assert page.locator("#range-select").is_disabled()
        assert page.locator("#range-reset").is_visible()
        assert "Selected" in page.locator("#selection-note").inner_text()
        with page.expect_response(re.compile(r"/metrics/series\?.*range=1h")) as info:
            page.locator("#range-reset").click()
        assert info.value.status == 200
        assert page.locator("#range-select").is_enabled()
        assert not page.locator("#range-reset").is_visible()
        assert errors == []

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
        assert page.locator("#graphs").count() == 0
        # The navigation still works, including logging out.
        page.get_by_role("button", name="Log out").click()
        page.wait_for_url(re.compile(r"/login$"))

    def test_a_collector_failing_while_watching_is_reported_and_recovers(self, page, target, collector):
        open_metrics(page, target)
        collector.mode = "error"
        choose(page, page.locator("#range-select"), "6h")
        page.wait_for_function("document.querySelector('.graph .chart-status').classList.contains('error')")
        assert page.locator(".graph .chart-status").first.inner_text() != ""
        collector.mode = "ok"
        choose(page, page.locator("#range-select"), "1h")
        page.wait_for_function("!document.querySelector('.graph .chart-status').classList.contains('error')")
        assert "Updated" in updated(page)

    def test_a_gap_in_the_data_does_not_break_the_chart(self, page, target, collector):
        errors = open_metrics(page, target)  # the fake data has one gap
        assert errors == []
        assert page.locator(".graph .chart canvas").count() >= 1

    def test_the_page_asks_for_at_most_600_points_with_extremes(self, page, target, collector):
        open_metrics(page, target)
        asked = [r for r in collector.requests if r.get("op") == "read"]
        assert asked and asked[-1]["max_points"] == 600 and asked[-1]["extremes"] is True
