# --- SSH connection to the Pi -------------------------------------------

variable "pi_host" {
  description = "Hostname or IP address of the Pi, reachable over SSH (use the eth0 address/hostname, not the AP)."
  type        = string
}

variable "pi_user" {
  description = "SSH/login user on the Pi. Needs passwordless sudo."
  type        = string
}

variable "ssh_private_key_path" {
  description = "Path to the SSH private key for pi_user. Leave empty to use the local SSH agent instead."
  type        = string
  default     = ""
}

# --- AP mode --------------------------------------------------------------

variable "mode" {
  description = "AP mode: 'dual' (both radios as AP) or 'uplink' (wlan1 as AP, wlan0 as Wi-Fi client)."
  type        = string
  default     = "dual"

  validation {
    condition     = contains(["dual", "uplink"], var.mode)
    error_message = "mode must be \"dual\" or \"uplink\"."
  }
}

variable "uplink_band" {
  description = "Band for the single AP radio in 'uplink' mode. Ignored in 'dual' mode (always 5 GHz on wlan1, 2.4 GHz on wlan0)."
  type        = string
  default     = "5"

  validation {
    condition     = contains(["5", "2.4"], var.uplink_band)
    error_message = "uplink_band must be \"5\" or \"2.4\"."
  }
}

# --- Wi-Fi AP settings ------------------------------------------------------

variable "ssid" {
  description = "SSID broadcast by the AP."
  type        = string
}

variable "psk" {
  description = "WPA2-PSK passphrase for the AP."
  type        = string
  sensitive   = true
}

variable "reg_domain" {
  description = "Wi-Fi regulatory domain (2-letter country code), required for 5 GHz."
  type        = string
  default     = "GB"
}

# --- Radios and channels ----------------------------------------------------

variable "if_24" {
  description = "Interface name for the 2.4 GHz radio (onboard, brcmfmac)."
  type        = string
  default     = "wlan0"
}

variable "if_5" {
  description = "Interface name for the 5 GHz-capable radio (USB adapter, mt7921u)."
  type        = string
  default     = "wlan1"
}

variable "ch_24" {
  description = "2.4 GHz channel (1, 6 or 11)."
  type        = number
  default     = 6
}

variable "ch_5" {
  description = "5 GHz channel (36, 40, 44 or 48 - non-DFS only)."
  type        = number
  default     = 36
}

# --- AP network --------------------------------------------------------------

variable "ap_net" {
  description = "AP network in CIDR form (e.g. \"10.1.1.0/24\"). Everything else is derived from it: the Pi is host .1, DHCP hands out host .2 up to the second-to-last address, and the dnsmasq netmask comes from the prefix."
  type        = string
  default     = "10.1.1.0/24"
}

variable "lease" {
  description = "DHCP lease time (dnsmasq format, e.g. \"15m\")."
  type        = string
  default     = "15m"
}

# --- WireGuard uplink to wireguard-router -----------------------------------

variable "wireguard_client_name" {
  description = "This Pi's peer name in wireguard-router's key store (e.g. \"wireguard-ap-peer\"), used by wga to fetch its client config via wireguard-router's scripts/wg-peer.sh. See README for how wga locates that script."
  type        = string
}

variable "wireguard_client_config" {
  description = "Full WireGuard client config (wg-quick .conf format) for wireguard_client_name, as printed by wireguard-router's scripts/wg-peer.sh client <name>. Populated automatically by wga - not meant to be set by hand in tfvars, since it's fetched fresh each run rather than stored. Empty skips WireGuard setup entirely (e.g. when using tofu directly without wga)."
  type        = string
  sensitive   = true
  default     = ""
}

# --- Metrics collector -------------------------------------------------------

variable "simple_metrics_binary" {
  description = "Path to the simple-metrics binary (aarch64, static) to install on the Pi as the metrics collector. Populated automatically by wga, which downloads the release pinned in simple-metrics.pin and verifies its checksum - not meant to be set by hand in tfvars. Empty skips the collector entirely (e.g. when using tofu directly without wga)."
  type        = string
  default     = ""
}
