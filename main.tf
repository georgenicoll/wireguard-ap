locals {
  remote_dir = "/home/${var.pi_user}"
  scripts = [
    "setup_forwarding_and_nat.sh",
    "setup_ap.sh",
    "uplink_wifi.sh",
    "view_currently_associated_clients.sh",
  ]

  # Derived from ap_net alone: the Pi is host .1, DHCP covers host .2 up to
  # the second-to-last address (last usable before the broadcast address).
  ap_netmask = cidrnetmask(var.ap_net)
  ap_ip      = cidrhost(var.ap_net, 1)
  dhcp_start = cidrhost(var.ap_net, 2)
  dhcp_end   = cidrhost(var.ap_net, -2)

  # Banner art comes from the ascii submodule (github.com/georgenicoll/ascii)
  # rather than a URL fetch, so it's available with no network access at
  # plan/apply time - as long as `git submodule update --init` has been run
  # once, which the README asks for.
  motd = "${file("${path.module}/ascii/monkeynuthead.txt")}\n\n${file("${path.module}/templates/motd.txt")}"

  env_file = templatefile("${path.module}/templates/wireguard-ap.env.tftpl", {
    pi_user    = var.pi_user
    ssid       = var.ssid
    psk        = var.psk
    reg_domain = var.reg_domain
    if_24      = var.if_24
    if_5       = var.if_5
    ch_24      = var.ch_24
    ch_5       = var.ch_5
    ap_ip      = local.ap_ip
    ap_net     = var.ap_net
    ap_netmask = local.ap_netmask
    dhcp_start = local.dhcp_start
    dhcp_end   = local.dhcp_end
    lease      = var.lease
  })
}

# Re-runs the deploy whenever the scripts, the rendered env file, or the mode
# change. Every step below is idempotent (safe to re-run), matching how the
# scripts themselves are designed to be re-run on the Pi.
resource "terraform_data" "ap_deploy" {
  lifecycle {
    precondition {
      # cidrhost(net, -2) needs at least host .1, .2 and a distinct .-2, i.e.
      # a /29 or larger (>= 8 addresses).
      condition     = tonumber(split("/", var.ap_net)[1]) <= 29
      error_message = "ap_net (${var.ap_net}) is too small - it must be a /29 or larger to fit the Pi's address and a DHCP range."
    }
  }

  triggers_replace = {
    env_file    = sha256(local.env_file)
    scripts     = sha256(join("", [for f in local.scripts : filesha256("${path.module}/scripts/${f}")]))
    motd        = sha256(local.motd)
    mode        = var.mode
    uplink_band = var.uplink_band
  }

  connection {
    type        = "ssh"
    host        = var.pi_host
    user        = var.pi_user
    private_key = var.ssh_private_key_path != "" ? file(var.ssh_private_key_path) : null
    agent       = var.ssh_private_key_path == ""
    # Default is 5m, which turns a wrong key/host/port into a long silent
    # hang (retried connection attempts) instead of a quick, clear error.
    timeout = "20s"
  }

  provisioner "file" {
    content     = local.env_file
    destination = "${local.remote_dir}/wireguard-ap.env"
  }

  provisioner "file" {
    source      = "${path.module}/scripts/setup_forwarding_and_nat.sh"
    destination = "${local.remote_dir}/setup_forwarding_and_nat.sh"
  }

  provisioner "file" {
    source      = "${path.module}/scripts/setup_ap.sh"
    destination = "${local.remote_dir}/setup_ap.sh"
  }

  provisioner "file" {
    source      = "${path.module}/scripts/uplink_wifi.sh"
    destination = "${local.remote_dir}/uplink_wifi.sh"
  }

  provisioner "file" {
    source      = "${path.module}/scripts/view_currently_associated_clients.sh"
    destination = "${local.remote_dir}/view_currently_associated_clients.sh"
  }

  # Staged here and installed to /etc/motd below - the upload itself can't
  # write there directly, since it runs as pi_user, not root.
  provisioner "file" {
    content     = local.motd
    destination = "${local.remote_dir}/.wireguard-ap-motd"
  }

  provisioner "remote-exec" {
    inline = [
      "chmod 600 ${local.remote_dir}/wireguard-ap.env",
      "chmod +x ${join(" ", [for f in local.scripts : "${local.remote_dir}/${f}"])}",
      "sudo install -o root -g root -m 0644 ${local.remote_dir}/.wireguard-ap-motd /etc/motd",
      "rm -f ${local.remote_dir}/.wireguard-ap-motd",
    ]
  }

  # One-time host setup (section 3 of the handoff doc). Idempotent: safe on
  # every apply, including the first one.
  provisioner "remote-exec" {
    inline = [
      "sudo apt-get update -y",
      "sudo apt-get install -y hostapd dnsmasq nftables iw",
      "sudo systemctl disable --now hostapd 2>/dev/null || true",
      "sudo systemctl enable dnsmasq",
      "sudo systemctl enable --now ssh",
      "sudo raspi-config nonint do_wifi_country ${var.reg_domain}",
      "sudo rfkill unblock wlan",
    ]
  }

  provisioner "remote-exec" {
    inline = [
      "${local.remote_dir}/setup_forwarding_and_nat.sh",
      "${local.remote_dir}/setup_ap.sh ${var.mode} ${var.uplink_band}",
    ]
  }

  # uplink_wifi.sh is uploaded but not run here: which network to join isn't
  # known until you're actually on the road. SSH in and run it manually, e.g.
  # ssh <pi_user>@<pi_host> '~/uplink_wifi.sh "some-hotel-wifi" "password"'
}
