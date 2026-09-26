"""The Metrics page and its data endpoint, against the local web app talking
to a fake collector (support.FakeCollector) - so what the web app asks the
collector for can be checked exactly. Tier: local.
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
    @pytest.mark.parametrize("path", ["/metrics", "/metrics/data?metric=cpu_percent&range=1h"])
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

    def test_the_page_loads_the_vendored_chart_library_not_a_cdn(self, web, collector):
        text = web.get("/metrics").text
        assert "/static/uPlot.iife.min.js" in text and "/static/metrics.js" in text
        assert "http://" not in text and "https://" not in text

    def test_the_nav_links_to_it_and_marks_it_current(self, web, collector):
        text = web.get("/metrics").text
        assert re.search(r'aria-disabled="true">Metrics</a>', text)
        assert 'href="/metrics"' in web.get("/").text

    def test_a_missing_collector_gives_a_helpful_page_not_an_error(self, web, collector):
        collector.mode = "down"
        r = web.get("/metrics")
        assert r.status_code == 200
        assert "No metrics available" in r.text and "mnh-ap-metrics" in r.text
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
