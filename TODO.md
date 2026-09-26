# TODO

Nothing outstanding.

## Ideas

- Split the web app out of `ap_deploy` so a web-only change doesn't re-run
  `setup_ap.sh`. Not needed for now (a full re-apply is acceptable).
- Session limits are 1 hour idle and 4 hours in total (`app.py`). If
  wanted: different values, or logins that can be revoked on the server.
