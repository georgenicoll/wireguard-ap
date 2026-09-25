# wireguard-ap

OpenTofu configuration that turns an already-running Raspberry Pi into a
portable Wi-Fi access point, by uploading a handful of scripts over SSH and
running them.

This is meant to work alongside
[wireguard-router](https://github.com/georgenicoll/wireguard-router), a
separate OpenTofu project that stands up a WireGuard endpoint on a cloud
provider. The Pi also acts as a WireGuard peer of that endpoint, so devices
connected to this travel AP get routed through it and can reach the other
peers on that network — a phone on the AP can see home network resources the
same way a laptop connected directly by WireGuard would. The Pi's own
internet access is untouched: wireguard-router's peer routes are `"auto"`
(tunnel network plus other peers' LANs, not `0.0.0.0/0`), so bringing up the
tunnel doesn't hijack the Pi's default route.

Unlike wireguard-router, this project doesn't create any infrastructure — the
Pi already exists. `apply` just pushes config and scripts to it and (re-)runs
the setup; `destroy` forgets that OpenTofu did so, without touching the Pi
(see [Re-applying and destroying](#re-applying-and-destroying)).

## TL;DR: setting up your Pi

**1. Install the tools.**

```bash
tofu version   # https://opentofu.org/docs/intro/install/
```

The [ascii](https://github.com/georgenicoll/ascii) submodule (part of the
login banner - see below) is fetched automatically the first time you run
`./wga`, so no separate submodule step is needed when using it. Running
`tofu` directly instead, skipping `wga`, does need one manual step first:
`git submodule update --init` (or clone with `--recurse-submodules`).

**2. Prepare the Pi**, if you haven't already:

- Raspberry Pi OS (Bookworm-style), reachable over SSH on `eth0`.
- A user (set via `pi_user`, no default) with **passwordless sudo for this
  project's scripts** and your **SSH public key** already authorized
  (`~/.ssh/authorized_keys` on the Pi) — OpenTofu connects to an existing
  account, it doesn't create one.

  Being in the `sudo` group is **not** enough on its own - that still
  prompts for a password, which the SSH provisioner has no way to answer
  (no TTY).

  Confirm with `ssh <pi_user>@<pi_host> sudo -n true` (silent = fine, "a
  password is required" = not set up yet).

  If needed, grant `NOPASSWD` for just the scripts this project uploads and
  self-elevates with `sudo` internally (`setup_host.sh`,
  `setup_wireguard.sh`, `setup_forwarding_and_nat.sh`, `setup_ap.sh`,
  `uplink_wifi.sh`, `setup_webapp.sh`, `view_currently_associated_clients.sh`,
  `view_wireguard_status.sh`, `diagnostics_sudo.sh`, `shutdown_pi.sh`,
  `setup_metrics.sh`), not root access in general. (Already set this up before
  the metrics collector was added? Run the block again: it replaces the file,
  and `./wga apply` needs `setup_metrics.sh` in it.)
  ```bash
  PI_USER=changeme   # <-- OVERWRITE with your real pi_user before running this

  cat <<EOF | sudo tee "/etc/sudoers.d/010-${PI_USER}-wireguard-ap"
  ${PI_USER} ALL=(root) NOPASSWD: /home/${PI_USER}/setup_host.sh, /home/${PI_USER}/setup_wireguard.sh, /home/${PI_USER}/setup_forwarding_and_nat.sh, NOPASSWD:SETENV: /home/${PI_USER}/setup_ap.sh, NOPASSWD: /home/${PI_USER}/uplink_wifi.sh, NOPASSWD: /home/${PI_USER}/setup_webapp.sh, NOPASSWD: /home/${PI_USER}/view_currently_associated_clients.sh, NOPASSWD: /home/${PI_USER}/view_wireguard_status.sh, NOPASSWD: /home/${PI_USER}/diagnostics_sudo.sh, NOPASSWD: /home/${PI_USER}/shutdown_pi.sh, NOPASSWD: /home/${PI_USER}/setup_metrics.sh
  EOF
  sudo chmod 0440 "/etc/sudoers.d/010-${PI_USER}-wireguard-ap"
  sudo visudo -c   # validates syntax - a bad sudoers file can lock out sudo entirely
  ```
  A bare script path with no arguments listed permits *any* arguments to
  that script, so `dual`/`uplink`, the SSID/password, etc. all still work -
  sudo just won't run anything *else* as root for this account. `setup_ap.sh`
  additionally needs the `SETENV` tag: it re-execs itself as
  `sudo --preserve-env=SSH_CONNECTION` so that, once elevated, it can still
  tell whether the original SSH session came in over Wi-Fi (see its
  comments) - without `SETENV`, sudo refuses to preserve that variable at
  all, even though the script itself is otherwise permitted. The login
  banner needs no root at all (see below), so it's not in this list.
- The onboard radio (`wlan0`, driver `brcmfmac`) and, for dual-band or a
  5 GHz AP, a USB adapter that supports AP mode (`wlan1`, driver `mt7921u`
  in the reference setup).

Everything else — installing `hostapd`/`dnsmasq`/`nftables`/`iw`, disabling
the stock `hostapd` service, enabling `dnsmasq`/`ssh`, setting the Wi-Fi
country and unblocking radio — is done for you by `apply` (idempotent, so
it's safe on every run, including the first).

**3. Create your config file and point the tooling at it.**

```bash
cp wireguard-ap.example.tfvars ~/private/wireguard-ap.tfvars   # or the Dropbox wireguard folder
$EDITOR ~/private/wireguard-ap.tfvars

export WGA_CONFIG=~/private/wireguard-ap.tfvars
export WGA_STATE_DIR=~/private/wireguard-ap-state   # keeps secrets out of the repo
```

The values you must fill in: `pi_host`, `pi_user`, `ssid`, `psk`,
`wireguard_client_name`. Everything else has a working default — see
[variables.tf](variables.tf) or the comments in
[wireguard-ap.example.tfvars](wireguard-ap.example.tfvars).

Put the two `export` lines in your shell profile or a direnv `.envrc`, or you
will be re-typing them every session.

`wireguard_client_name` is this Pi's peer name in **wireguard-router**'s own
key store (register it there first with
`./scripts/wg-peer.sh add <name> <ip> --lan <ap_net>` if it doesn't exist
yet - see that project's README). `wga` fetches the actual client config
itself, via wireguard-router's `scripts/wg-peer.sh` - nothing else to set
here, and nothing from it is ever written to disk. This needs:

```bash
export WGR_CONFIG=~/private/wireguard-router.tfvars   # wireguard-router's own config
export WGR_REPO_DIR=~/code/wireguard-router            # defaults to ../wireguard-router
```

(or `WGR_KEYS` instead of `WGR_CONFIG` - see `wga`'s own comments and
wireguard-router's README). If you already work with wireguard-router from
the same shell, these are likely set already.

**4. Deploy.**

```bash
./wga init
./wga apply
```

**5. Verify.**

```bash
./wga test                   # about a minute; see "Integration tests" below
ssh <pi_user>@<pi_host> '~/view_currently_associated_clients.sh'      # add --ssh to also list SSH sessions
```

Or from your own device: join the SSID, then `ssh <pi_user>@<ap_ip>` (default
`10.1.1.1`, printed as the `ssh_hint` output).

**6. `uplink` mode only** — join an upstream network once you know which one.
This is deliberately **not** part of the tfvars config (you don't know the
network until you're there), so it's a manual step:

```bash
ssh <pi_user>@<pi_host> '~/uplink_wifi.sh "some-hotel-wifi" "password"'
```

**7. Changing settings later.** Edit your tfvars (or the scripts, if you're
changing behaviour) and re-apply — see
[Re-applying and destroying](#re-applying-and-destroying):

```bash
./wga apply
```

## Design

| Path | Role |
| --- | --- |
| `main.tf` | One `terraform_data` resource: opens an SSH connection, uploads the scripts, the `webapp/` app and a rendered env file, then runs the host, WireGuard, forwarding, AP and web UI setup scripts. |
| `templates/wireguard-ap.env.tftpl` | Renders your config into a `KEY='value'` file the scripts `source` on the Pi, instead of having their settings hardcoded. |
| `templates/motd.tftpl` + `ascii/monkeynuthead.txt` + `templates/AP.txt` | Combined into `~/.wireguard-ap-motd` and printed by a snippet appended to `pi_user`'s own `~/.bashrc` on every interactive login: the shared "monkey / nut / head" banner (from the [ascii](https://github.com/georgenicoll/ascii) submodule), "AP" underneath it in the same style but kept local to this repo since it's project-specific, then a summary of the available scripts plus the current SSID and subnet. Per-user rather than system-wide (`/etc/motd`), and needs no root at all. |
| `scripts/*.sh` | The Pi-side scripts, uploaded byte-for-byte (not passed through `templatefile()`, since they use bash `${VAR}` inside heredocs that would collide with OpenTofu's own templating). `setup_host.sh` (packages, stock services, Wi-Fi country, radio unblock - the one-time-ish setup that used to be inline `sudo` commands in `main.tf`), `setup_wireguard.sh`, `setup_ap.sh`, `setup_forwarding_and_nat.sh`, `uplink_wifi.sh`, `setup_webapp.sh`, `view_currently_associated_clients.sh` and `view_wireguard_status.sh` all self-elevate with `sudo` internally (`[[ $EUID -eq 0 ]] \|\| exec sudo "$SCRIPT" "$@"`) rather than being invoked with `sudo` at the call site, so sudoers can be scoped to exactly these script paths - see step 2 above. The latter two are also run non-interactively by the web UI (see below), which is the other reason they self-elevate the whole script rather than sudo-prefixing individual commands: no TTY means no password prompt, so every sudo call needs its own NOPASSWD coverage otherwise. |
| `webapp/` | A small FastAPI + [htmx](https://htmx.org) web UI (`app.py`, `templates/`, `static/`), uploaded as-is and run by `setup_webapp.sh` via [uv](https://docs.astral.sh/uv/) - see "Web UI" below. |
| `variables.tf` / `outputs.tf` / `versions.tf` | The input/output contract and provider requirement (`hashicorp/null` only — no cloud provider). |
| `simple-metrics.pin` + `tools/fetch_simple_metrics.sh` | Which release of the [simple-metrics](https://github.com/georgenicoll/simple-metrics) collector to deploy, and the checksum of its archive. `wga` runs the fetch script, which downloads that release on your machine, checks it against the pinned checksum, and hands the path to `tofu` - see "Metrics collector". |
| `scripts/setup_metrics.sh` | Installs the collector's binary, creates its user, and writes and starts its systemd unit. Run by `main.tf`'s separate `metrics_deploy` resource. |
| `wga` | Thin wrapper around `tofu`, same idea as wireguard-router's `wgr` - also fetches the WireGuard client config named by `wireguard_client_name` via wireguard-router's own `scripts/wg-peer.sh`. |

Config is kept deliberately minimal: most of the Pi-side network settings
(`ap_ip`, the DHCP range, the dnsmasq netmask, the bridge name) are **derived**
from a single `ap_net` CIDR rather than each being their own variable — see
the comments in `main.tf`.

## Prerequisites

- [OpenTofu](https://opentofu.org/docs/intro/install/) >= 1.6
- A Raspberry Pi running Raspberry Pi OS (Bookworm-style, NetworkManager),
  reachable over SSH on `eth0`, with passwordless sudo and your public key
  already authorized
- An SSH agent holding the matching key, or `ssh_private_key_path` set in
  your config

## Environment variables

| Variable | Purpose |
| --- | --- |
| `WGA_CONFIG` | Path to your private `.tfvars` file. Required for `plan`/`apply`/`destroy`/`refresh`/`import`/`console`. |
| `WGA_STATE_DIR` | Keep state outside the repo. Recommended — state contains the Wi-Fi PSK and the fetched WireGuard client config. |
| `WGR_REPO_DIR` | Path to a wireguard-router checkout, to run its `scripts/wg-peer.sh`. Defaults to `../wireguard-router`. Only needed when `wireguard_client_name` is set. |
| `WGR_CONFIG` / `WGR_KEYS` | wireguard-router's own env vars, needed for the above — see its README. |

## Usage

```bash
./wga init
./wga plan
./wga apply
./wga output
./wga destroy
./wga test        # integration tests - run after every apply (see below)
```

## Outputs

```bash
./wga output
```

| Output | Value |
| --- | --- |
| `ap_ssid` | SSID broadcast by the AP. |
| `ap_ip` | The Pi's address on the AP network (derived from `ap_net`). |
| `ssh_hint` | Ready-to-use `ssh user@ip` for reaching the Pi once joined to the AP. |
| `mode` | The mode currently applied: `"dual"` or `"uplink"`. |
| `uplink_band` | The single AP radio's band in `"uplink"` mode. `null` (and omitted from plan/apply summaries) in `"dual"` mode, where it doesn't apply. |

## Re-applying and destroying

**Re-apply** (`./wga apply` again) is safe and idempotent, but it isn't
unconditional: OpenTofu only re-runs the upload-and-setup steps when
something the deployment depends on has actually changed — your tfvars, any
of the 6 scripts, the fetched WireGuard client config, or `mode`/`uplink_band`
(the client config is fetched fresh on every invocation, so a change on the
wireguard-router side, e.g. a rotated key, is picked up automatically without
you needing to change anything in wireguard-ap's own tfvars). If nothing
changed, `apply` reports no changes and doesn't touch the Pi. If something did
change, it re-uploads all 6 scripts and the env file (overwriting what's
already there) and reruns `setup_host.sh`, `setup_wireguard.sh` (if a client
config was fetched), `setup_forwarding_and_nat.sh` and
`setup_ap.sh <mode> <band>`, in that order. Those scripts are written to be
safely re-run — they delete and recreate their own configs/units and restart
services — so a full re-run is the expected, harmless outcome of a config
change.

**`destroy` does not undo anything on the Pi.** It only removes OpenTofu's
record of having deployed there — there's no destroy-time provisioner, so
`hostapd`, `dnsmasq`, the NAT rule, the bridge and the uploaded scripts are
left exactly as they were. To actually disable the AP, SSH in and stop/disable
the relevant units by hand (`mnh-hostapd@*`, `dnsmasq`, `mnh-ap-nat`,
`mnh-ap-local-routing`, `mnh-ap-wg-watchdog.timer`, `mnh-ap-webapp`), and
remove
`/etc/NetworkManager/dispatcher.d/90-mnh-ap-wg-route-precedence` if you also
want the `wg0` route-precedence fix gone, or ask for a destroy-time
provisioner to be added if you want `destroy` to do that automatically.

## What gets configured on the Pi

- **`dual` mode**: `wlan0` (2.4 GHz) and `wlan1` (5 GHz) both run `hostapd`
  as one bridged AP, `br-ap`. No band steering — each client picks its band.
- **`uplink` mode**: `wlan1` is the only AP (5 GHz by default, or 2.4 with
  `uplink_band`); `wlan0` becomes a Wi-Fi client via NetworkManager. Join a
  network with `uplink_wifi.sh` (manual step, see above).
- **Internet**: `eth0` when connected (priority), falling back to `wlan0` in
  `uplink` mode. NAT/masquerade out through whichever uplink is active.
- **WireGuard**: if `wireguard_client_name` is set, `wg0` connects to
  wireguard-router as that peer. AP clients get routed to wireguard-router's
  tunnel network and every other peer's LAN (whatever that peer's own
  `AllowedIPs` covers - typically `"auto"` on the wireguard-router side, not
  full-tunnel internet, so this doesn't touch the Pi's own default route).
  `wg0` traffic is **not** NAT'd by `setup_forwarding_and_nat.sh` (unlike the
  real internet uplink), so each AP client's real address is visible to the
  rest of the mesh rather than collapsing into the Pi's own tunnel IP - this
  only works end-to-end if this peer was registered on the wireguard-router
  side with `--lan <ap_net>`, so other peers know to route that subnet back
  here. Monitor with `sudo wg show all` on the Pi.

  **Endpoint watchdog**: wireguard-router's own address usually comes from
  dynamic DNS (`dynu_hostname`), which can change - but `wg-quick` only
  resolves `Endpoint` once, at startup, so the tunnel would otherwise keep
  silently talking to a stale address until something re-resolves it.
  `mnh-ap-wg-watchdog.sh`, run every minute by
  `mnh-ap-wg-watchdog.timer`, is a port of OpenWRT's own
  `wireguard_watchdog` (Jason A. Donenfeld / Aleksandr V. Piskunov,
  GPL-2.0 - see `/usr/bin/wireguard_watchdog` on an OpenWRT router): once
  `wg0`'s handshake has been stale for more than 150s, it re-resolves the
  peer's hostname in place via `wg set wg0 peer <key> endpoint host:port` -
  no interface restart, no route flap. (This is separate from
  `mnh-ap-wg-route-precedence.sh`'s NetworkManager dispatcher hook, which
  handles *this Pi's own* address changing, not the peer's.)

  **Known, accepted trade-off**: if a peer's routed LAN (e.g. another site's
  `--lan`) happens to be the same subnet as wherever the Pi is currently
  plugged in/connected to over `eth0`/`wlan0`, that's a genuine address-space
  collision - the same destination address can validly mean two different
  things (a device on that peer's real LAN, or a device on whatever network
  the Pi is currently attached to). This can't be resolved from the
  destination address alone, so a deliberate, fixed choice is made instead of
  leaving it to chance (see git history on this file/`setup_wireguard.sh` for
  the incident this was first found by, and the discussion around it):

  - **Connections into the Pi itself** (SSH, mainly) always keep replying via
    the interface they arrived on, regardless of any colliding route -
    `setup_forwarding_and_nat.sh` sets up policy routing for this (a
    `mnh_ap_route` nftables table plus
    `/usr/local/sbin/mnh-ap-local-routing.sh`), keyed on arrival interface
    rather than on any peer's registered subnet, so it protects management
    access generically against any current or future peer collision without
    needing to know about peer LANs at all.
  - **Everything else** - AP clients' own traffic, and any new connection the
    Pi itself initiates - deliberately prefers `wg0`:
    `mnh-ap-wg-route-precedence.sh` (run after `wg-quick up` and on every
    NetworkManager event, to survive a DHCP renewal resetting things) forces
    any `wg0` route to win over a colliding connected-interface route by
    metric, rather than leaving the kernel to pick unpredictably. This means
    if the Pi is at home and a peer's registered LAN happens to collide with
    the real home subnet, AP clients (and the Pi itself) lose the ability to
    reach that specific local subnet directly for as long as the collision
    holds - reaching the peer is treated as more important than reaching a
    same-numbered local network. If it ever gets in the way, recover via the
    AP's own Wi-Fi (see Notes) - unaffected either way, since `ap_net` is a
    separate, non-colliding subnet, and management access is never at risk
    of this regardless (see above).
- **DHCP/DNS**: `dnsmasq` on `br-ap`, range and netmask derived from `ap_net`;
  clients get the Pi as DNS, which forwards to the Pi's own resolver.
- **Always reachable**: the bridge and hostapd units are hotplug-safe — the
  AP starts as soon as the USB adapter appears, even without eth0 or wlan0.
- Re-running `setup_ap.sh` (directly, or via `./wga apply`) is always safe,
  including switching between modes.
- **Login banner**: `pi_user`'s interactive shells print the available
  scripts and what each does, the SSID/subnet, and the web UI's URL (via
  `~/.bashrc`, not system-wide `/etc/motd`), so you don't need to remember
  or check this README from the Pi itself.

See `/mnt/c/Users/george/Dropbox/Network/wireguard/pi-ap-handoff.md` for the
full hardware/design rationale and troubleshooting reference these scripts
were built from.

## Metrics collector

The Pi keeps a rolling history of its own CPU, load, memory, swap, temperature
and network traffic (`eth0`, both radios, `br-ap` and `wg0`), sampled every 5
seconds for 7 days, in memory only - nothing is written to disk. That is done
by [simple-metrics](https://github.com/georgenicoll/simple-metrics), a small
separate Rust daemon (about 15 MB of RAM when its history is full), which
serves it on a Unix socket for the web app to read.

**How it gets there.** `simple-metrics.pin` names a release and the SHA-256 of
its `aarch64` archive. `./wga` runs `tools/fetch_simple_metrics.sh`, which
downloads that release *on your machine* (so the Pi needs no access to GitHub),
refuses it unless it matches the pinned checksum, and passes the path to `tofu`,
which uploads it. The checksum comes from the pin file only, never from the
download, so a release altered after the fact is caught. Downloads are cached
in `~/.cache/wireguard-ap/simple-metrics`, and re-checked on every run.

**Upgrading.** Put the new version and the checksum of its
`simple-metrics-<version>-aarch64-unknown-linux-musl.tar.gz` (from the
`SHA256SUMS` on the release's GitHub page) into `simple-metrics.pin`, then
`./wga apply`. **Trying a local build instead:** `SIMPLE_METRICS_BIN=/path/to/simple-metrics ./wga apply`
uses that binary without any check (and says so).

**What runs on the Pi.** `setup_metrics.sh` installs the binary to
`/usr/local/bin/simple-metrics` (root-owned, so nothing that runs as a service
user can change it) and starts `mnh-ap-metrics.service` as its own unprivileged
user, `mnh-metrics`. It needs no privileges at all - everything it reads is
world-readable under `/proc` and `/sys` - so the unit drops every capability
and locks the rest of the system down (`systemd-analyze security` rates it
1.4, "OK"). Its socket, `/run/simple-metrics/simple-metrics.sock`, is created
in a directory only `mnh-metrics` and the web app's group (`mnh-web`) can
enter, so no other account on the Pi can connect.

**Separate from the access point's setup.** The collector has its own
`terraform_data` resource, `metrics_deploy`, and `setup_metrics.sh` is
deliberately not in `local.scripts`. So a new release only re-runs
`setup_metrics.sh` - never `setup_ap.sh`, which takes the access point down and
back up. (A test checks this stays true.) It does run after `ap_deploy` on a
first deploy, because it needs the web app's group.

**What happens to the history.** It survives the web app being redeployed or
restarted, which is the point of a separate service. It is lost when the
collector itself restarts (an upgrade, a crash) and on a reboot.

**Not built yet:** the web app doesn't use it. The Metrics page needs the
collector to answer time-range, per-metric and downsampled queries first - see
its `TODO.md`.

## Web UI

A small FastAPI + [htmx](https://htmx.org) app. The main page shows the Pi's
hostname and IP address, fetched via htmx from `/api/hostinfo` rather than
baked into the initial HTML, plus links to two status pages:

- **`/wireguard`** — `wg0`'s peer status (endpoint, handshake, transfer,
  allowed IPs), via `view_wireguard_status.sh` (`wg show wg0` - which hides
  private/preshared keys by default, unlike `wg show wg0 dump`).
- **`/clients`** — currently associated AP Wi-Fi clients and their IPs, via
  the same `view_currently_associated_clients.sh` used from the CLI (see
  "What gets configured on the Pi" below). Always runs it without `--ssh`
  (SSH session details aren't exposed here).
- **`/diagnostics`** — one card per command that `diagnostics.sh --show-commands`
  lists (name, description, one text box per parameter, a Run button); Run
  calls `diagnostics.sh --run-command <name> <args...>` and streams the output
  into a modal the same way `/manage` does. The page knows nothing about
  specific commands: to add one, write a `cmd_<name>` function in
  `scripts/diagnostics.sh` and `register` it. The script runs as the web
  user (no sudo), validates its own arguments, and the app only ever passes
  them to it as separate exec args (never via a shell), for commands the
  script itself advertised.
  Parameters are declared in the `--show-commands` line: `host` (required
  text), `server?` (optional text), `unit=a,b,c` (pick one) or
  `type?=A,MX` (optional pick one) - the page draws text boxes or drop-downs
  accordingly, and both the app and the script check them. Commands: `ping`,
  `traceroute`, `dig`, `ip-addr-list`, `ip-route-list`, `ip-route-get`,
  `ap-clients` (runs the existing self-elevating view script),
  `service-status` and `logs` (fixed list of this project's units), and
  `uname` (`uname -a`), `free` (`free -h`) and `os-release`
  (`cat /etc/os-release`).

  Commands that need root live in `scripts/diagnostics_sudo.sh` (currently
  `wg-status`), which speaks the same `--show-commands`/`--run-command`
  protocol: `diagnostics.sh` appends that script's `--show-commands` output
  to its own and hands any `--run-command` it doesn't recognise over to it,
  so it holds no knowledge of the root-only commands. Only `--run-command`
  elevates (sudo); the web service's own sudoers file (see "Security model"
  below) already allows `diagnostics_sudo.sh --run-command <anything>`.
  `diagnostics_lib.sh` is the registry/argument-checking code both scripts
  source.
- **`/manage`** — runs `setup_ap.sh`/`uplink_wifi.sh` with parameters chosen
  in the browser (mode, band, upstream SSID/password) and streams their
  output live rather than just showing a final result, using htmx's
  official [SSE extension](https://htmx.org/extensions/sse/)
  (`webapp/static/htmx-ext-sse.js`, vendored) rather than hand-written
  `fetch()`/`EventSource` JS: the form does a normal `hx-post`, whose
  response is an HTML fragment (built with `htpy`, escaping each streamed
  line - it's swapped in via `innerHTML`, not treated as plain text like
  before) wired with `hx-ext="sse" sse-connect="/run/stream/<job_id>"` -
  htmx notices the newly-swapped element's attributes and opens the stream
  itself. The Wi-Fi password still never touches a URL or browser history
  (POST body only; the job id in the stream URL carries no secret). Jobs
  are tracked in a plain in-memory dict - deliberately: this is a
  single-user local admin tool, not a job queue that needs to survive a
  restart. Also has a "Danger zone" - a confirm-gated (Pico `<dialog>`, not
  a bare `confirm()`) button that runs `shutdown_pi.sh`
  (`systemctl poweroff`) via `hx-post="/shutdown"`, fired as a background
  task so the HTTP response reaches the browser before the Pi actually goes
  down.

Both status pages just run the corresponding script and show its raw
output in a `<pre>` block - a starting point, not a polished table, per
"we'll iterate from there". HTML fragments (`/api/hostinfo`) are built with
[htpy](https://htpy.dev) rather than f-strings, so values are escaped
automatically; script output elsewhere renders through Jinja2's default
auto-escaping instead, for the same reason.

```
https://<pi_host>/       # or https://<ap_ip>/ once joined to the AP - also linked from the login banner
```

- **`setup_webapp.sh`** installs [uv](https://docs.astral.sh/uv/) system-wide
  (`/usr/local/bin`, so it's on `PATH` for both this script and the service
  below, whichever user runs each), then runs `webapp/app.py` as
  `mnh-ap-webapp.service` via `uv run app.py`. There's no `requirements.txt`
  or venv to manage by hand: `app.py` declares its own dependencies with
  [PEP 723](https://peps.python.org/pep-0723/) inline script metadata (the
  `# /// script ... ///` block at the top), and `uv run` resolves and caches
  an environment for them on the fly - editing that block is the only thing
  needed to add a dependency.
- Runs as its own unprivileged user `mnh-web` (not `pi_user`, not root) on
  `0.0.0.0:443` - reachable from `eth0`, the AP's own Wi-Fi, and `wg0`
  alike. Binding the default HTTPS port without being root comes from
  `CAP_NET_BIND_SERVICE`, granted to just this service via
  `AmbientCapabilities=` in `mnh-ap-webapp.service` - not from running as
  root. See "Security model" below for why it isn't `pi_user`.
- **HTTPS, self-signed**: `setup_webapp.sh` generates
  `/var/lib/mnh-ap/cert.pem` / `key.pem` (owned by `mnh-web`, key `0600`)
  with `openssl` the first time it runs (left alone on
  later runs, so redeploys don't force the browser to re-trust it). There's
  no real hostname to get a CA-signed cert for, so your browser will warn
  once - expected for a device like this, not a bug.
- **Login-gated**: every page except `/login` requires a password, checked
  against the AP's own Wi-Fi password (`PSK`) - one shared secret rather
  than a separate site password to remember. A signed session cookie
  (`itsdangerous`, `secure`-flagged since the site is HTTPS-only) persists
  the login; its signing key (`/var/lib/mnh-ap/.session_secret`) is generated
  once on first run and kept, so a redeploy doesn't log everyone out. The
  cookie holds only `{"authenticated": true, "login_at": <unix time>}`
  (signed, not encrypted, and never the password). Two limits apply, both
  enforced by the server from the signed cookie rather than by the browser
  dropping it: a login ends after **one hour of inactivity** (sliding - every
  authenticated page load issues a fresh cookie; `SESSION_LIFETIME_SECONDS`
  in `app.py`), and after **4 hours from the login itself** however active
  you've been (`SESSION_ABSOLUTE_LIMIT_SECONDS`; `login_at` is never
  rewritten by the sliding refresh). A cookie with no usable `login_at` -
  such as one from before the cap existed - counts as expired. Sessions are
  stateless, so there's nothing to revoke early: logging out clears your
  browser's copy, and the only way to invalidate every existing cookie is to
  delete `/var/lib/mnh-ap/.session_secret` and restart the service.
- **Security model**: the web app is the part of this project most exposed
  to whatever is on the network, so it's kept unable to hurt anything else
  if it's ever compromised:
  - It runs as `mnh-web`, a system account with no shell and no home. The
    scripts, `webapp/` and everything else deployed under `pi_user`'s home
    are owned by `pi_user` - `mnh-web` can run the few it's allowed to but
    can't change them (traverse-only access to the home directory via an
    ACL; `.wg0.conf`, the env file and keys are `0600`). Had it run as
    `pi_user`, code running in the app could have edited a script sudoers
    lets root run, and so become root.
  - `setup_webapp.sh` writes `/etc/sudoers.d/020-mnh-web-wireguard-ap`
    (checked with `visudo -cf` before it's installed) allowing `mnh-web`
    to sudo only `view_wireguard_status.sh`,
    `view_currently_associated_clients.sh`, `shutdown_pi.sh` (with no
    argument or `reboot`), `diagnostics_sudo.sh --run-command ...`,
    `uplink_wifi.sh` (free-form SSID/password, so it validates its own
    input) and `setup_ap.sh` with exactly `dual`, `uplink 5` or
    `uplink 2.4`. No manual sudoers step is needed for it. `pi_user`'s own
    sudoers file (step 2 above) is for you and the deploy, and no longer
    has to list the scripts only the web app runs.
  - Everything it writes (TLS key, session secret, uv's cache) is in
    `/var/lib/mnh-ap`, owned by `mnh-web` and `0700`. A session secret or
    key that was in `webapp/` (including one uploaded from a dev checkout)
    is deleted on deploy and never used.
  - `NoNewPrivileges`, `CapabilityBoundingSet` and `ProtectSystem`-style
    sandboxing are deliberately *not* set on the unit: they apply to the
    whole process tree, including `sudo` and the setup scripts it runs,
    and would break them.
- **SSID-branded, not hardcoded**: the page title/heading show the AP's
  actual configured SSID rather than a fixed name. `SSID` and `PSK` reach
  the app via `EnvironmentFile=` in `mnh-ap-webapp.service`, pointing at
  the same `wireguard-ap.env` the shell scripts already source - no
  separate config to maintain.
- **htmx is vendored** (`webapp/static/htmx.min.js`, `htmx-ext-sse.js`), not
  pulled from a CDN, for the same reason as the `ascii` banner submodule:
  this AP may have no internet uplink at all (e.g. `dual` mode, or before
  `uplink_wifi.sh` has been run), and the page should still work. Pinned at
  2.0.10 (npm's actual `latest`, not the `next`-tagged 4.0.0 tried
  initially) specifically because the SSE extension needs htmx's classic
  `defineExtension` API, which v4 replaced with `registerExtension` -
  confirmed by grepping the vendored file rather than assuming.
- `uv`'s own package cache means only the *first* run on a given Pi needs
  internet access to resolve `app.py`'s dependencies - later re-runs (e.g.
  after a reboot, or a redeploy that doesn't touch the dependency block)
  don't.

### Local development

`./dev_webapp.sh` runs the web UI locally with hot reload, so UI changes
(templates, `static/`, `app.py`) can be iterated on without deploying to
the Pi each time. It generates a self-signed cert for `localhost` in `.dev/`
(`dev-cert.pem`/`dev-key.pem`, git-ignored, and outside `webapp/` so it's
never uploaded to the Pi; separate from the Pi's own certificate), keeps
its session secret there too, sets a dummy `SSID`/`PSK` (override by exporting
`PSK` beforehand), and serves on `https://127.0.0.1:8443/` via
`uv run app.py` with `uvicorn --reload` enabled. The login password is
printed to the console on startup.

The "Run setup" buttons and the status/shutdown pages won't work locally -
they shell out to `scripts/*.sh`, which self-elevate via `sudo` and touch
real network interfaces (hostapd, nftables, etc.), and aren't even at the
path `app.py` looks for them locally (see `SCRIPTS_DIR`'s comment). This
is for iterating on layout/styling/flow, not exercising the Pi-side
scripts.

## Integration tests

`./wga apply` prints a reminder to run these when it finishes. They're not run
automatically: they take about a minute, log in to the Pi's web UI and use a
real browser, and you may not want that on every apply.

```bash
./wga test                # local + smoke + ui - the normal post-apply check
./wga test smoke          # just the Pi's state and the web app over HTTPS (~30s)
./wga test local          # no Pi needed - safe to run any time, e.g. before an apply
./wga test reboot         # opt-in: restarts the Pi and checks it comes back healthy
./wga test all            # everything, including the reboot
./wga test smoke -k logs  # anything else is passed to pytest
```

`tests/integration.py` is the [uv](https://docs.astral.sh/uv/) script that
runs them (its `pytest`, `httpx` and `playwright` dependencies are declared
inline, like `app.py`'s), so there's nothing to install. The tests themselves
are split by area, one file each:

| File | Area |
|---|---|
| `test_diagnostics_scripts.py` | the diagnostics scripts' argument handling and the generated sudoers file (`local`) |
| `test_login.py` | the login form's responses: blank, wrong and right PassKey (`local`) |
| `test_session_lifetime.py` | session cookie lifetime and expiry (`local`) |
| `test_diagnostics_ui.py` | the Diagnostics page in a browser (`local` and `ui`) |
| `test_metrics_deploy.py` | how the collector gets deployed: the fetch script (download, verify, cache), the pin, the systemd unit, and that a collector upgrade can't re-run the access point's setup (`local`) |
| `test_pi_state.py` | the Pi's services, routing, permissions and leftovers (`smoke`) |
| `test_pi_metrics.py` | the deployed collector: runs unprivileged, its socket is closed to other accounts, it is the pinned release (`smoke`) |
| `test_pi_web.py` | login/session security and the read-only pages (`smoke`) |
| `test_pi_diagnostics.py` | every diagnostic through the deployed web app (`smoke`) |
| `test_reboot.py` | the Restart button, end to end (`reboot`) |

`conftest.py` holds the fixtures (`pi`, `pi_web`, `local_server`, the browser
ones) and `support.py` the shared code (the Pi config, the web client, the
health checks). The Pi tiers read `pi_host`,
`pi_user`, `ssh_private_key_path`, `mode` and `psk` from the tfvars file
`WGA_CONFIG` points at - the same one `./wga apply` uses. The password is held
in memory only and never printed or written anywhere.

| Tier | Needs the Pi | What it checks |
|---|---|---|
| `local` | no | The diagnostics scripts' argument handling, including injection attempts (`;`, `$(...)`, backticks, newlines, option-like values, choice values with commas, unknown or crafted command names) - and that they run nothing. The generated sudoers file for the web user is valid and hasn't been widened. The UI, in a browser, against a throwaway copy of the web app on a spare port (it never touches any other server). |
| `smoke` | yes (SSH + HTTPS) | Every service enabled and active, no failed units, the web app running as `mnh-web` on 443, the routing-protection rules in place, permissions (private state directory, traverse-only ACL on the home directory, scripts `755` and owned by the deploy user, nothing writable by the web user), no leftover secrets, dev files or old `mnet` names on the Pi. Pages need a login and the session cookie is `Secure`/`HttpOnly`/`SameSite`. Every read-only page and diagnostic returns real data (including the ones that go through `sudo`), bad or malicious requests are refused and run nothing, and a `ping` stops when its stream is dropped. |
| `ui` | yes | The same browser checks as `local`, against the real Pi: the picker, the cards, Run button placement, optional and choice fields, `ping` running until the dialog is closed (button or Esc) and then stopping, output that follows at the bottom. Needs Chromium: if it's missing the test says to run `uv run --with playwright playwright install chromium`. |
| `reboot` | yes | Opt-in. Clicks **Restart** in the UI, waits for the "restarting" page to return to the login, confirms the Pi has a new boot id, then waits for every service, the routing protection and the web app to be healthy again. Takes a few minutes and briefly takes the AP down - don't run it while relying on the Pi. |

The Pi tiers are read-only apart from starting and stopping a `ping`; only
`reboot` changes anything. A failing run doesn't undo the deploy - the
output names what's wrong.

To add a check, add a test to the file for its area (or a new `test_*.py`
if it's a new one) and mark it with its tier (`@pytest.mark.smoke` and so
on). A new diagnostic command needs no
test changes for the page itself, but should get a case in the `smoke`
diagnostics tests (and, if it takes free text, in the injection tests).

## Notes

- `psk` is marked `sensitive`, but it still ends up in OpenTofu state because
  it has to be delivered to the Pi. Keep state private (see `WGA_STATE_DIR`).
- `.gitignore` refuses to track `*.tfvars` (except `*.example.tfvars`) as a
  second line of defence against committing secrets.
- The env file uploaded to the Pi (`wireguard-ap.env`) is single-quoted
  (`KEY='value'`) so `$` and backticks in a secret aren't expanded. A secret
  containing a literal single quote would break this — none of the current
  defaults do.
- `ap_net` must be a `/29` or larger, so there's room for the Pi's own
  address, a DHCP range, and a distinct broadcast address; smaller values are
  rejected before `apply` touches the Pi.
- The SSH connection has a 20s timeout (`main.tf`, default is 5m). A wrong
  key, wrong host, or unreachable Pi then fails fast with a clear error
  instead of `apply` sitting on `Provisioning with 'file'...` for minutes
  while it silently retries.
- The `ascii` submodule is vendored (not fetched from a URL at apply time)
  specifically so `apply` works without internet access, once it's been
  fetched at least once - `wga` fetches it automatically if missing, so
  this only matters if you're invoking `tofu` directly instead.
- The fetched WireGuard client config's `DNS =` line is stripped before it
  reaches the Pi (`main.tf`'s `local.wireguard_conf`): `wg-quick` would
  otherwise try to manage the Pi's own system resolver via
  `resolvconf`/`systemd-resolved` when bringing `wg0` up, which isn't needed
  here and would fail hard if neither is installed. Routing is unaffected -
  only interface-level DNS management is removed.
- `wireguard_client_config` is fetched fresh on every `wga` invocation and
  never written to disk by this project (it does end up in OpenTofu state,
  same as `psk`) - editing wireguard-ap's own tfvars can't change it; that
  has to happen on the wireguard-router side (`scripts/wg-peer.sh update`).
- **If `wg0` ever breaks `eth0` access** (a peer's routed LAN colliding with
  wherever the Pi is currently connected - see the WireGuard section above),
  the AP's own Wi-Fi (`ap_net`, default `10.1.1.0/24`) is on a separate
  subnet and is unaffected: connect to the SSID and
  `ssh <pi_user>@<ap_ip>` (the `ssh_hint` output) instead of `pi_host`, then
  `sudo systemctl disable --now wg-quick@wg0` to remove the tunnel's routes
  immediately.

## Layout

```
wga                             driver script: ./wga <tofu command> | ./wga test
tests/integration.py            runs the integration tests (uv script); tests/test_*.py, conftest.py, support.py - see "Integration tests"
wireguard-ap.example.tfvars     template for your private config file
main.tf / variables.tf / outputs.tf / versions.tf
templates/wireguard-ap.env.tftpl
templates/motd.tftpl
templates/AP.txt
ascii/                          git submodule: github.com/georgenicoll/ascii (auto-fetched by wga)
scripts/setup_host.sh
scripts/setup_wireguard.sh
scripts/setup_forwarding_and_nat.sh
scripts/setup_ap.sh
scripts/uplink_wifi.sh
scripts/view_currently_associated_clients.sh
scripts/view_wireguard_status.sh
scripts/diagnostics.sh          Diagnostics page commands (--show-commands / --run-command)
scripts/diagnostics_sudo.sh     the root-only ones, same protocol; diagnostics.sh delegates to it
scripts/diagnostics_lib.sh      registry/argument checking both source
scripts/setup_webapp.sh
scripts/setup_metrics.sh        installs and runs the simple-metrics collector (see "Metrics collector")
scripts/shutdown_pi.sh
simple-metrics.pin              the collector release to deploy + its checksum
tools/fetch_simple_metrics.sh   downloads and verifies that release (run by wga)
webapp/app.py
webapp/templates/_header.html   shared by every page
webapp/templates/index.html
webapp/templates/login.html
webapp/templates/output.html    shared by /wireguard and /clients
webapp/templates/manage.html
webapp/static/site.css
webapp/static/pico.min.css      vendored, not CDN-loaded - see "Web UI"
webapp/static/htmx.min.js       vendored, not CDN-loaded - see "Web UI"
webapp/static/htmx-ext-sse.js   vendored, not CDN-loaded - see "Web UI"
webapp/static/favicon.svg
```
