# TODO

Nothing outstanding for the Metrics page, the `smq` client or the local
collector setup: all built, deployed and confirmed on the Pi.

## Known, not fixed here

- Two smoke tests (`test_home_shows_real_values`, `test_wireguard_details_page`)
  fail because the WireGuard peer (138.68.133.166) never handshakes; the cloud
  router is probably down. Expect these two failures until it is back.

## Ideas

- Split the web app out of `ap_deploy` so a web-only change doesn't re-run
  `setup_ap.sh`. Not needed for now (a full re-apply is acceptable).
