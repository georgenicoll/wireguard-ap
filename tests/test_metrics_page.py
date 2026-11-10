"""The Metrics page, its data endpoint and its assets, against the local web
app talking to the real smweb (the pinned release where this machine can run
it), which reads a fake collector (support.FakeCollector) - so what is asked of
the collector can be checked exactly. Tier: local.

smweb writes the chart and answers for its data; the web app's part is the
login, the page around the chart, and passing smweb's answers on.
"""
import re
import time

import pytest

from support import LOCAL_PSK, Web

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600}


@pytest.fixture
def web(local_server) -> Web:
    web = Web(local_server.base_url, LOCAL_PSK)
    assert web.login().status_code == 303
    return web


@pytest.mark.local
class TestMetricsPage:
    @pytest.mark.parametrize("path", ["/metrics", "/metrics/data?metric=cpu_percent&range=1h",
                                      "/metrics/assets/metrics.js"])
    def test_it_needs_a_login(self, local_server, path):
        r = Web(local_server.base_url, LOCAL_PSK).get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"

    def test_the_page_offers_every_metric_the_collector_has(self, web, collector):
        r = web.get("/metrics")
        assert r.status_code == 200
        values = re.findall(r'<option value="([^"]+)"', r.text)
        assert [m["name"] for m in collector.METRICS] == [v for v in values if v not in RANGES]

    def test_the_metrics_are_grouped_and_labelled_with_their_units(self, web, collector):
        text = web.get("/metrics").text
        assert '<optgroup label="System">' in text and '<optgroup label="Network">' in text
        assert "CPU (%)" in text and "eth0 received (bytes/s)" in text
        assert "Load (1 min)" in text and "(1 min))" not in text, "no unit for a plain number"

    def test_the_page_offers_the_time_ranges_with_an_hour_chosen(self, web, collector):
        text = web.get("/metrics").text
        for r in RANGES:
            assert f'<option value="{r}"' in text
        assert '<option value="1h" selected>' in text

    def test_the_page_loads_the_chart_library_from_the_pi_not_a_cdn(self, web, collector):
        text = web.get("/metrics").text
        assert "/metrics/assets/uPlot.iife.min.js" in text and "/metrics/assets/metrics.js" in text
        assert "/metrics/assets/uPlot.min.css" in text
        assert 'data-url="/metrics/data"' in text, "where the chart fetches its data from"
        assert "http://" not in text and "https://" not in text

    def test_it_is_the_sites_own_page_around_smwebs_chart(self, web, collector):
        text = web.get("/metrics").text
        assert text.count("<html") == 1 and "<title>mnh-ap - Metrics</title>" in text
        assert "<nav>" in text and 'id="chart"' in text, "the site's nav, smweb's chart"
        assert "htmx.min.js" in text, "every page with the nav needs htmx for Log out to work"

    def test_the_nav_links_to_it_and_marks_it_current(self, web, collector):
        text = web.get("/metrics").text
        assert re.search(r'aria-disabled="true">Metrics</a>', text)
        assert 'href="/metrics"' in web.get("/").text

    def test_a_missing_collector_gives_a_helpful_page_not_an_error(self, web, collector):
        collector.mode = "down"
        r = web.get("/metrics")
        assert r.status_code == 200
        assert "No metrics available" in r.text and "The metrics collector" in r.text
        assert "simple-metrics service" in r.text
        assert "metric-select" not in r.text

    def test_a_refusing_collector_gives_the_same_page(self, web, collector):
        collector.mode = "error"
        r = web.get("/metrics")
        assert r.status_code == 200 and "No metrics available" in r.text


@pytest.mark.local
class TestMetricsData:
    def get(self, web, metric="cpu_percent", range="1h"):
        return web.get(f"/metrics/data?metric={metric}&range={range}")

    def test_it_returns_a_chart_ready_series(self, web, collector):
        r = self.get(web)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["metric"] == {"name": "cpu_percent", "label": "CPU", "unit": "%"}
        assert data["range"] == "1h"
        n = len(data["timestamps"])
        assert n > 10
        for key in ("avg", "min", "max"):
            assert len(data[key]) == n, key
        assert data["step_ms"] > 0
        assert None in data["avg"], "gaps are passed through as null"
        assert r.headers["cache-control"] == "no-store"

    def test_it_asks_the_collector_for_one_downsampled_metric_over_the_range(self, web, collector):
        before = int(time.time() * 1000)
        self.get(web, "load1", "6h")
        after = int(time.time() * 1000)
        reads = [r for r in collector.requests if r["op"] == "read"]
        assert len(reads) == 1
        read = reads[0]
        assert read["metrics"] == ["load1"], "only the one metric"
        assert read["max_points"] == 600 and read["extremes"] is True
        assert before - RANGES["6h"] * 1000 <= read["from"] <= after - RANGES["6h"] * 1000
        assert "to" not in read and "step_ms" not in read

    @pytest.mark.parametrize("name", RANGES)
    def test_each_range_asks_for_that_much_history(self, web, collector, name):
        self.get(web, range=name)
        read = [r for r in collector.requests if r["op"] == "read"][0]
        asked = time.time() * 1000 - read["from"]
        assert abs(asked - RANGES[name] * 1000) < 5000

    def test_it_checks_the_metric_against_the_collectors_own_list(self, web, collector):
        r = self.get(web, "not_a_real_metric")
        assert r.status_code == 404 and r.json() == {"error": "no such metric"}
        assert not [q for q in collector.requests if q["op"] == "read"], "never asked for it"

    @pytest.mark.parametrize("bad", [
        "", "..%2F..%2Fetc%2Fpasswd", "a%20b", "x%3Bid", "%22", "a%0Ab", "cpu_percent%22%2C%22op%22%3A%22x",
        "%24%28id%29", "a" * 65, "%C3%A9",
    ])
    def test_a_badly_formed_metric_never_reaches_the_collector(self, web, collector, bad):
        r = self.get(web, bad)
        assert r.status_code == 400, f"{bad!r}: {r.status_code} {r.text}"
        assert collector.requests == []

    @pytest.mark.parametrize("bad", ["", "2h", "1H", "7", "forever", "1h%3Bid", "-1h", "0"])
    def test_a_range_that_is_not_offered_is_refused(self, web, collector, bad):
        r = self.get(web, range=bad)
        assert r.status_code == 400 and "range must be one of" in r.json()["error"]
        assert collector.requests == []

    def test_a_missing_parameter_is_refused_cleanly(self, web, collector):
        assert web.get("/metrics/data").status_code == 400
        assert web.get("/metrics/data?metric=cpu_percent").status_code == 200  # the range defaults to 1h

    def test_a_down_collector_is_a_503_with_a_readable_message(self, web, collector):
        collector.mode = "down"
        r = self.get(web)
        assert r.status_code == 503
        assert r.json()["error"].endswith(".") and "collector" in r.json()["error"]

    def test_a_refusing_collector_is_a_503_that_does_not_leak_its_message(self, web, collector):
        collector.mode = "error"
        r = self.get(web)
        assert r.status_code == 503
        assert "boom" not in r.text, "the collector's own error stays in the log"

    def test_the_collector_can_come_back(self, web, collector):
        collector.mode = "down"
        assert self.get(web).status_code == 503
        collector.mode = "ok"
        assert self.get(web).status_code == 200


@pytest.mark.local
class TestMetricsAssets:
    """The chart's script, styles and library, which smweb carries and the web
    app passes on behind the login."""

    @pytest.mark.parametrize("name, kind", [
        ("metrics.js", "javascript"), ("uPlot.iife.min.js", "javascript"),
        ("metrics.css", "css"), ("uPlot.min.css", "css"),
    ])
    def test_they_are_served_with_their_type_and_can_be_cached(self, web, name, kind):
        r = web.get(f"/metrics/assets/{name}?v=1")
        assert r.status_code == 200
        assert kind in r.headers["content-type"]
        assert "max-age" in r.headers["cache-control"], "a versioned URL: safe to keep"
        assert len(r.content) > 500

    def test_the_chart_code_reads_where_to_fetch_from_the_page(self, web):
        assert "dataset.url" in web.get("/metrics/assets/metrics.js").text

    @pytest.mark.parametrize("name", ["nope.js", "metrics.js.map", "page.css.bak", ".", ".hidden"])
    def test_an_asset_that_is_not_there_is_a_404(self, web, name):
        assert web.get(f"/metrics/assets/{name}").status_code == 404

    @pytest.mark.parametrize("name", ["..%2F..%2Fetc%2Fpasswd", "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                                      "a%20b", "x%0d%0aHost:%20evil", "a" * 65, "%C3%A9", "a%00b"])
    def test_an_odd_name_is_refused_before_it_goes_anywhere(self, web, name):
        r = web.get(f"/metrics/assets/{name}")
        assert r.status_code in (404, 422), f"{name!r}: {r.status_code}"
        assert "root:" not in r.text


@pytest.mark.local
class TestMetricsWebService:
    """smweb itself being down - not the collector, which smweb reports on."""

    def test_the_page_says_which_services_to_check(self, web, smweb_service):
        smweb_service.stop()
        r = web.get("/metrics")
        assert r.status_code == 200, "a helpful page, not an error"
        assert "No metrics available" in r.text
        assert "mnh-ap-metrics-web" in r.text and "mnh-ap-metrics" in r.text
        assert "metric-select" not in r.text and "<nav>" in r.text

    @pytest.mark.parametrize("path", ["/metrics/data?metric=cpu_percent&range=1h",
                                      "/metrics/assets/metrics.js"])
    def test_its_other_routes_are_a_503_with_a_readable_message(self, web, smweb_service, path):
        smweb_service.stop()
        r = web.get(path)
        assert r.status_code == 503
        assert r.json()["error"].endswith(".") and "web service" in r.json()["error"]
        assert r.headers["cache-control"] == "no-store"

    def test_a_service_that_comes_back_is_used_again(self, web, smweb_service, collector):
        smweb_service.stop()
        assert web.get("/metrics/data?metric=cpu_percent").status_code == 503
        smweb_service.start()
        assert web.get("/metrics/data?metric=cpu_percent").status_code == 200
        assert 'id="chart"' in web.get("/metrics").text


@pytest.mark.local
class TestWhatReachesSmweb:
    """The web app builds smweb's request itself, from two query values."""

    @pytest.mark.parametrize("bad", ["x%0d%0aX-Evil:%201", "a%20HTTP/1.1%0d%0a%0d%0aGET%20/", "%00", "a+b"])
    def test_nothing_in_the_query_can_add_to_the_request(self, web, collector, bad):
        r = web.get(f"/metrics/data?metric={bad}&range=1h")
        assert r.status_code == 400
        assert collector.requests == []

    def test_other_parameters_are_dropped(self, web, collector):
        r = web.get("/metrics/data?metric=cpu_percent&range=1h&op=shutdown&from=0")
        assert r.status_code == 200
        reads = [q for q in collector.requests if q["op"] == "read"]
        assert len(reads) == 1 and reads[0]["from"] > 1_000_000_000_000, "its own range, not the one asked for"
