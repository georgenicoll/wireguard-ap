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
  `uplink_wifi.sh`), not root access in general:
  ```bash
  PI_USER=changeme   # <-- OVERWRITE with your real pi_user before running this

  cat <<EOF | sudo tee "/etc/sudoers.d/010-${PI_USER}-wireguard-ap"
  ${PI_USER} ALL=(root) NOPASSWD: /home/${PI_USER}/setup_host.sh, /home/${PI_USER}/setup_wireguard.sh, /home/${PI_USER}/setup_forwarding_and_nat.sh, NOPASSWD:SETENV: /home/${PI_USER}/setup_ap.sh, NOPASSWD: /home/${PI_USER}/uplink_wifi.sh
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
| `main.tf` | One `terraform_data` resource: opens an SSH connection, uploads the 6 scripts and a rendered env file, then runs the host, WireGuard, forwarding and AP setup scripts. |
| `templates/wireguard-ap.env.tftpl` | Renders your config into a `KEY='value'` file the scripts `source` on the Pi, instead of having their settings hardcoded. |
| `templates/motd.tftpl` + `ascii/monkeynuthead.txt` + `templates/AP.txt` | Combined into `~/.wireguard-ap-motd` and printed by a snippet appended to `pi_user`'s own `~/.bashrc` on every interactive login: the shared "monkey / nut / head" banner (from the [ascii](https://github.com/georgenicoll/ascii) submodule), "AP" underneath it in the same style but kept local to this repo since it's project-specific, then a summary of the available scripts plus the current SSID and subnet. Per-user rather than system-wide (`/etc/motd`), and needs no root at all. |
| `scripts/*.sh` | The Pi-side scripts, uploaded byte-for-byte (not passed through `templatefile()`, since they use bash `${VAR}` inside heredocs that would collide with OpenTofu's own templating). `setup_host.sh` (packages, stock services, Wi-Fi country, radio unblock - the one-time-ish setup that used to be inline `sudo` commands in `main.tf`), `setup_wireguard.sh`, `setup_ap.sh`, `setup_forwarding_and_nat.sh` and `uplink_wifi.sh` all self-elevate with `sudo` internally (`[[ $EUID -eq 0 ]] \|\| exec sudo "$SCRIPT" "$@"`) rather than being invoked with `sudo` at the call site, so sudoers can be scoped to exactly these script paths - see step 2 above. |
| `variables.tf` / `outputs.tf` / `versions.tf` | The input/output contract and provider requirement (`hashicorp/null` only — no cloud provider). |
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
the relevant units by hand (`mnet-hostapd@*`, `dnsmasq`, `mnet-ap-nat`,
`mnet-ap-local-routing`, `mnet-ap-wg-watchdog.timer`), and remove
`/etc/NetworkManager/dispatcher.d/90-mnet-ap-wg-route-precedence` if you also
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
  `mnet-ap-wg-watchdog.sh`, run every minute by
  `mnet-ap-wg-watchdog.timer`, is a port of OpenWRT's own
  `wireguard_watchdog` (Jason A. Donenfeld / Aleksandr V. Piskunov,
  GPL-2.0 - see `/usr/bin/wireguard_watchdog` on an OpenWRT router): once
  `wg0`'s handshake has been stale for more than 150s, it re-resolves the
  peer's hostname in place via `wg set wg0 peer <key> endpoint host:port` -
  no interface restart, no route flap. (This is separate from
  `mnet-ap-wg-route-precedence.sh`'s NetworkManager dispatcher hook, which
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
    `mnet_ap_route` nftables table plus
    `/usr/local/sbin/mnet-ap-local-routing.sh`), keyed on arrival interface
    rather than on any peer's registered subnet, so it protects management
    access generically against any current or future peer collision without
    needing to know about peer LANs at all.
  - **Everything else** - AP clients' own traffic, and any new connection the
    Pi itself initiates - deliberately prefers `wg0`:
    `mnet-ap-wg-route-precedence.sh` (run after `wg-quick up` and on every
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
  scripts and what each does (via `~/.bashrc`, not system-wide `/etc/motd`),
  so you don't need to remember or check this README from the Pi itself.

See `/mnt/c/Users/george/Dropbox/Network/wireguard/pi-ap-handoff.md` for the
full hardware/design rationale and troubleshooting reference these scripts
were built from.

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
wga                             driver script: ./wga <tofu command>
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
```
