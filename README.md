# wireguard-ap

OpenTofu configuration that turns an already-running Raspberry Pi into a
portable Wi-Fi access point, by uploading a handful of scripts over SSH and
running them.

This is meant to work alongside
[wireguard-router](https://github.com/georgenicoll/wireguard-router), a
separate OpenTofu project that stands up a WireGuard endpoint on a cloud
provider. The intent is for the Pi to also act as a WireGuard peer, so that
devices connected to this travel AP get routed through that WireGuard
endpoint and can reach the other peers on that network — a phone on the AP
should be able to see home network resources the same way a laptop connected
directly by WireGuard would. **The WireGuard side of that isn't wired up
yet** — this project currently only handles the AP itself; the WireGuard
peer configuration is the next piece of work.

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
  `setup_forwarding_and_nat.sh`, `setup_ap.sh`, `uplink_wifi.sh`), not root
  access in general:
  ```bash
  PI_USER=changeme   # <-- OVERWRITE with your real pi_user before running this

  cat <<EOF | sudo tee "/etc/sudoers.d/010-${PI_USER}-wireguard-ap"
  ${PI_USER} ALL=(root) NOPASSWD: /home/${PI_USER}/setup_host.sh, /home/${PI_USER}/setup_forwarding_and_nat.sh, /home/${PI_USER}/setup_ap.sh, /home/${PI_USER}/uplink_wifi.sh
  EOF
  sudo chmod 0440 "/etc/sudoers.d/010-${PI_USER}-wireguard-ap"
  sudo visudo -c   # validates syntax - a bad sudoers file can lock out sudo entirely
  ```
  A bare script path with no arguments listed permits *any* arguments to
  that script, so `dual`/`uplink`, the SSID/password, etc. all still work -
  sudo just won't run anything *else* as root for this account. The login
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

The values you must fill in: `pi_host`, `pi_user`, `ssid`, `psk`. Everything
else has a working default — see [variables.tf](variables.tf) or the comments
in [wireguard-ap.example.tfvars](wireguard-ap.example.tfvars).

Put the two `export` lines in your shell profile or a direnv `.envrc`, or you
will be re-typing them every session.

**4. Deploy.**

```bash
./wga init
./wga apply
```

**5. Verify.**

```bash
ssh <pi_user>@<pi_host> '~/view_currently_associated_clients.sh'
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
| `main.tf` | One `terraform_data` resource: opens an SSH connection, uploads the 5 scripts and a rendered env file, then runs the host setup and AP setup scripts. |
| `templates/wireguard-ap.env.tftpl` | Renders your config into a `KEY='value'` file the scripts `source` on the Pi, instead of having their settings hardcoded. |
| `templates/motd.txt` + `ascii/monkeynuthead.txt` + `templates/AP.txt` | Combined into `~/.wireguard-ap-motd` and printed by a snippet appended to `pi_user`'s own `~/.bashrc` on every interactive login: the shared "monkey / nut / head" banner (from the [ascii](https://github.com/georgenicoll/ascii) submodule), "AP" underneath it in the same style but kept local to this repo since it's project-specific, then a summary of the available scripts. Per-user rather than system-wide (`/etc/motd`), and needs no root at all. |
| `scripts/*.sh` | The Pi-side scripts, uploaded byte-for-byte (not passed through `templatefile()`, since they use bash `${VAR}` inside heredocs that would collide with OpenTofu's own templating). `setup_host.sh` (packages, stock services, Wi-Fi country, radio unblock - the one-time-ish setup that used to be inline `sudo` commands in `main.tf`), `setup_ap.sh`, `setup_forwarding_and_nat.sh` and `uplink_wifi.sh` all self-elevate with `sudo` internally (`[[ $EUID -eq 0 ]] \|\| exec sudo "$SCRIPT" "$@"`) rather than being invoked with `sudo` at the call site, so sudoers can be scoped to exactly these script paths - see step 2 above. |
| `variables.tf` / `outputs.tf` / `versions.tf` | The input/output contract and provider requirement (`hashicorp/null` only — no cloud provider). |
| `wga` | Thin wrapper around `tofu`, same idea as wireguard-router's `wgr`. |

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
| `WGA_STATE_DIR` | Keep state outside the repo. Recommended — state contains the Wi-Fi PSK. |

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
of the 4 scripts, or `mode`/`uplink_band`. If nothing changed, `apply` reports
no changes and doesn't touch the Pi. If something did change, it re-uploads
all 4 scripts and the env file (overwriting what's already there) and reruns
the one-time host setup, `setup_forwarding_and_nat.sh` and
`setup_ap.sh <mode> <band>`. Those scripts are written to be safely re-run —
they delete and recreate their own configs/units and restart services — so a
full re-run is the expected, harmless outcome of a config change.

**`destroy` does not undo anything on the Pi.** It only removes OpenTofu's
record of having deployed there — there's no destroy-time provisioner, so
`hostapd`, `dnsmasq`, the NAT rule, the bridge and the uploaded scripts are
left exactly as they were. To actually disable the AP, SSH in and stop/disable
the relevant units by hand (`mnet-hostapd@*`, `dnsmasq`, `mnet-ap-nat`), or
ask for a destroy-time provisioner to be added if you want `destroy` to do
that automatically.

## What gets configured on the Pi

- **`dual` mode**: `wlan0` (2.4 GHz) and `wlan1` (5 GHz) both run `hostapd`
  as one bridged AP, `br-ap`. No band steering — each client picks its band.
- **`uplink` mode**: `wlan1` is the only AP (5 GHz by default, or 2.4 with
  `uplink_band`); `wlan0` becomes a Wi-Fi client via NetworkManager. Join a
  network with `uplink_wifi.sh` (manual step, see above).
- **Internet**: `eth0` when connected (priority), falling back to `wlan0` in
  `uplink` mode. NAT/masquerade out through whichever uplink is active.
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

## Layout

```
wga                             driver script: ./wga <tofu command>
wireguard-ap.example.tfvars     template for your private config file
main.tf / variables.tf / outputs.tf / versions.tf
templates/wireguard-ap.env.tftpl
templates/motd.txt
templates/AP.txt
ascii/                          git submodule: github.com/georgenicoll/ascii (auto-fetched by wga)
scripts/setup_host.sh
scripts/setup_forwarding_and_nat.sh
scripts/setup_ap.sh
scripts/uplink_wifi.sh
scripts/view_currently_associated_clients.sh
```
