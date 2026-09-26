# TODO

## Metrics page (branch `metrics-page`, uncommitted)

Built and passing its 38 local tests: the `/metrics` page, `/metrics/data`,
the uPlot chart (vendored), the fake collector for tests. Still to do:

- [x] Browser tests (`tests/test_metrics_ui.py`) and Pi smoke tests for the
      page (`tests/test_pi_metrics.py`).
- [x] Log out did nothing on the Metrics page: it lacked htmx. Fixed, with a
      test that Log out works from every page.
- [ ] Merge simple-metrics PR #8 and tag `v0.3.0` (adds the `smq` CLI), then
      bump `simple-metrics.pin` to `v0.3.0` with its aarch64 archive's sha256
      from the release's `SHA256SUMS`. The Pi runs v0.1.0 and the page needs
      v0.2.0+ (range, subset, downsampling), so the bump and the apply go
      together. (v0.2.0's checksum was `28466fa6…37332`.)
- [ ] Decide how `~/smq` reaches the collector socket on the Pi: the deploy
      user is deliberately locked out. Documented: `sudo -g mnh-web ~/smq …`
      (password). Alternative: add the deploy user to `mnh-web`.
- [x] README: the Metrics page, `smq`, local running, tests table, layout;
      uPlot's licence added. (Re-check after the UI tests are written.)
- [ ] After deploying: `./wga test smoke` (includes the new `~/smq` check),
      and try `sudo -g mnh-web ~/smq latest` on the Pi.
- [ ] Run the full local tier (`uv run tests/integration.py local`), pyflakes,
      `tofu fmt` and `tofu validate`.
- [ ] Commit, push, open a PR, merge (when asked).
- [ ] Deploy: `./wga apply` (re-runs `setup_ap.sh` because the web app
      changed; check `iw dev` is quick afterwards and reboot with
      `~/shutdown_pi.sh reboot` if the radio wedges), then `./wga test smoke`.
- [ ] Confirm with the user that the page works on the Pi before calling it done.
- [ ] Update the assistant's memory notes (`simple-metrics-collector-plan`) with
      the v0.2.0 release and the page.

Done in the same branch: `run_local.sh` (in simple-metrics), `dev_webapp.sh`
pointing at the local collector, and the `smq` client with its deployment
(`metrics_cli` resource, `fetch_simple_metrics.sh --cli`).

## Known, not mine to fix here

- Two smoke tests (`test_home_shows_real_values`, `test_wireguard_details_page`)
  fail because the WireGuard peer (138.68.133.166) never handshakes; the cloud
  router is probably down. Expect these two failures until it is back.

## Ideas

- Split the web app out of `ap_deploy` so a web-only change doesn't re-run
  `setup_ap.sh`. Not needed for now (a full re-apply is acceptable).
