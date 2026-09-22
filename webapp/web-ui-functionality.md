# Web UI functionality

Captures what the current web app *does* (functional behavior) and *how it
works* (technical mechanics) as a reference for a rewrite. Deliberately
excludes visual specifics (colors, layout, exact copy, button placement) -
those are free to change. Stack stays FastAPI + htmx.

## Purpose

A small admin web UI, served by the Pi itself, for configuring and
monitoring the AP without SSH. Single-user, local-network-only tool - not
multi-tenant, not internet-facing.

## Pages / routes

- **`GET /login`** - password form. No auth required (obviously).
- **`POST /login`** - checks the submitted password against `PSK`
  (constant-time compare). Success: sets a session flag and redirects to
  `/`. Failure: re-renders the login form with an error, HTTP 401.
- **`POST /logout`** - clears the session, redirects to `/login`.
- **`GET /`** (home) - requires login. Shows the AP's SSID and, via a
  fragment loaded on page load, host info (hostname + local IP - see
  `/api/hostinfo`). Links to WireGuard status, AP status, and Manage AP.
- **`GET /api/hostinfo`** - requires login. Returns an HTML fragment (not
  a full page) with hostname and the Pi's local IP address, for the home
  page to load asynchronously. IP is determined by opening a UDP socket
  toward an arbitrary external address and reading back the local
  endpoint (no packet actually sent) - works even without a default
  route, falling back to "unknown" on failure.
- **`GET /wireguard`** - requires login. Runs `view_wireguard_status.sh`
  (shows `wg0`'s peers: endpoint, handshake time, transfer, allowed IPs -
  never prints keys) and displays its raw stdout/stderr. Has a manual
  "Refresh" control that re-runs the script and swaps just the output
  region, without a full page reload.
- **`GET /clients`** - requires login. Same pattern as `/wireguard`, but
  runs `view_currently_associated_clients.sh` (associated Wi-Fi devices,
  default routes, and - optionally - active SSH sessions).
- **`GET /manage`** - requires login. The AP configuration page:
  - **Configure AP mode** form: radio choice of `dual` (both radios
    broadcast this AP, 2.4 + 5 GHz) or `uplink` (one radio is this AP,
    the other joins an upstream network as a client); in uplink mode, a
    band selector (5 GHz default, or 2.4) picks which radio hosts the AP.
    The band field is only meaningful/enabled in uplink mode. Submitting
    runs `setup_ap.sh <mode> [<band>]` as a background job and streams
    its live output into the page (see "Long-running jobs" below). The
    submit button disables itself while the job runs.
  - **Join upstream Wi-Fi** form: SSID (required) + password (optional -
    blank means an open network). Only meaningful in `uplink` mode, after
    `wlan0` is free to act as a client. Submitting runs
    `uplink_wifi.sh <ssid> [<password>]` as a background job, streamed the
    same way. Password is sent only in the POST body, never in a URL or
    stored/logged.
  - **Danger zone**: a confirm-gated "shut down the Pi" action (must
    confirm in a dialog before it fires) that calls `POST /shutdown`.
- **`POST /run/setup_ap`** - requires login. Form fields `mode`
  (`dual`|`uplink`, required) and `band` (`5`|`2.4`, default `5`,
  meaningful for uplink only). Validates both against a fixed allow-list
  (400 on anything else). Starts `setup_ap.sh` as a background job,
  returns an HTML fragment wired to stream its output (see below).
- **`POST /run/uplink_wifi`** - requires login. Form fields `ssid`
  (required, non-blank) and `password` (optional). Starts
  `uplink_wifi.sh` as a background job, same streaming pattern.
- **`GET /run/stream/{job_id}`** - requires login. Server-Sent Events
  stream of a background job's output (see below).
- **`POST /shutdown`** - requires login. Fires `shutdown_pi.sh`
  (`systemctl poweroff`) as a fire-and-forget background task so the HTTP
  response reaches the browser before the Pi actually powers off. Returns
  a short status message.

## Long-running jobs / live output streaming

`setup_ap.sh` and `uplink_wifi.sh` can each take a while and produce
incremental output. The pattern:

1. The POST handler starts the script as an async subprocess
   (stdout+stderr merged into one stream), generates a random job id, and
   stores the running process in an in-memory dict keyed by that id.
2. It immediately returns an HTML fragment containing a `<pre>` output
   element pre-wired (via htmx SSE extension) to open an
   `EventSource`-style connection to `/run/stream/{job_id}`.
3. The stream endpoint reads the subprocess's output line by line as it
   arrives and emits each line as an SSE `message` event (HTML-escaped).
   Each event is appended to the output element (not replacing prior
   lines). When the process exits, it emits the exit code, then a `done`
   event that closes the stream client-side.
4. If a client asks for a job id that isn't tracked (already finished, or
   never existed), the stream immediately reports that and closes.

Design constraints worth preserving:
- Jobs are tracked in a plain in-memory dict, deliberately - this is a
  single-user tool on one process, not a durable job queue. A job whose
  stream is never opened leaks its dict entry until the process exits,
  but nothing runs longer than the script itself does.
- The submit button for a job-starting form disables itself while the
  job is in flight (prevents double-submission).
- Output panes auto-scroll/grow but are capped in height with internal
  scrolling (kept as a UX expectation, not a spec of exact CSS).

## Auth / sessions

- Single shared password: the AP's own Wi-Fi password (`PSK`), not a
  separate credential to manage.
- Session state is a signed cookie (not server-side session storage).
  The signing secret is generated once on first run and persisted to
  disk (a dotfile next to the app) specifically so that restarting the
  process (e.g. after a redeploy) doesn't invalidate everyone's existing
  login.
- Every route except `GET/POST /login` requires an authenticated session;
  unauthenticated requests get redirected to `/login` (303).
- Cookie is restricted to HTTPS-only transport.

## Configuration / environment

- `SSID` and `PSK` are read from process environment variables at
  startup, sourced (on the Pi) from the same env file the shell scripts
  use - no separate config surface. `SSID` is used for display only
  (page titles/headings); `PSK` doubles as the login password.
- The app locates the operational shell scripts (`setup_ap.sh`,
  `uplink_wifi.sh`, `view_wireguard_status.sh`,
  `view_currently_associated_clients.sh`, `shutdown_pi.sh`) in a fixed
  directory alongside where the scripts get deployed on the Pi (uploaded
  flat, next to the webapp folder) - not bundled inside the webapp
  itself. This lets the same script set serve both direct SSH use and
  the web UI.

## Script execution model

- All scripts the web UI invokes self-elevate to root internally (they
  check their own EUID and re-exec themselves via `sudo` if needed, using
  passwordless sudoers entries scoped to their exact paths) rather than
  being invoked with `sudo` by the app. This matters because the web UI
  has no TTY to satisfy an interactive sudo password prompt.
- Two execution modes are used depending on whether output needs to
  stream live:
  - **Synchronous, capture-and-return** (`/api hostinfo` is computed
    in-process; `/wireguard` and `/clients` run their script
    synchronously with a timeout, capturing stdout/stderr and rendering
    it as plain preformatted text after the request completes). A
    failure to even launch the script (e.g. missing binary) is caught
    and shown as an inline error rather than a hard 500.
  - **Asynchronous, streamed** (`setup_ap.sh`, `uplink_wifi.sh`) - see
    "Long-running jobs" above.
- Script inputs from form fields are validated against fixed allow-lists
  server-side before being passed as command-line arguments (mode, band);
  free-text fields (SSID, uplink password) are passed through as
  arguments, not interpolated into a shell string, avoiding shell
  injection.

## Output rendering / escaping

- Script output is untrusted-ish (could contain arbitrary text from
  system tools) and is always rendered as escaped plain text inside
  `<pre>` blocks - both for the synchronous status pages and for each
  streamed line - never interpreted as HTML.
- Small dynamic HTML fragments the app constructs itself (e.g. the host
  info fragment) are built through an escaping-by-default component
  API rather than hand-assembled/interpolated HTML strings, so any
  dynamic value (hostname, IP) is automatically escaped.

## Networking / TLS

- Served over HTTPS only, on the standard HTTPS port, reachable from all
  of the Pi's networks (wired uplink, the AP's own Wi-Fi, and the
  WireGuard tunnel) - i.e. bound to all interfaces, not just one.
- Uses a self-signed certificate (generated once, on first run, and left
  alone thereafter so redeploys don't force browsers to re-trust it
  every time) since the Pi has no real hostname to get a CA-signed cert
  for. Browser cert warnings on first visit are expected, not a bug.
- Runs as an unprivileged user but is still able to bind the low
  (privileged) HTTPS port via a narrowly granted OS capability, rather
  than running the whole process as root.

## Dependencies / runtime

- Python, single-file app, dependencies declared inline in the script
  itself (PEP 723-style) rather than a separate requirements file/venv -
  a dependency-aware runner resolves and caches an environment for them
  automatically. Only needs internet access the first time (or when the
  dependency list changes).
- Frontend has no build step: server-rendered HTML (Jinja-style
  templates) progressively enhanced with htmx for partial-page
  updates/polling-free live output, plus a small SSE extension for the
  streaming job output. No SPA framework, no bundler.
- Third-party frontend JS (htmx core + its SSE extension) is vendored
  into the app rather than loaded from a CDN, because the AP may have no
  internet uplink at all when the UI is used (e.g. before an uplink has
  been configured, or in dual mode). Pinned to a version of htmx that
  still exposes the classic extension API the SSE extension needs (a
  later major version changed that API).

## Non-functional constraints to keep in mind for the rewrite

- Must keep working with zero internet access on the Pi (no CDN
  dependencies at runtime).
- Must not require a TTY/interactive password anywhere in the request
  path (all privileged operations go through passwordless, path-scoped
  sudo in the underlying scripts - the web app itself never needs root).
- Single-user assumption is load-bearing in a few places (in-memory job
  tracking, no concurrency/locking around jobs) - fine to keep, but
  worth calling out explicitly if the rewrite changes that assumption.
- Secrets handling: Wi-Fi/login password never appears in a URL, query
  string, or browser history; only ever in a POST body or the signed
  session cookie.
