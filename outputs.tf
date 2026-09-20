output "ap_ssid" {
  description = "SSID broadcast by the AP."
  value       = var.ssid
}

output "ap_ip" {
  description = "IP address of the Pi on the AP network."
  value       = local.ap_ip
}

output "ssh_hint" {
  description = "How to reach the Pi once joined to the AP."
  value       = "ssh ${var.pi_user}@${local.ap_ip}"
}

output "mode" {
  description = "AP mode currently applied: \"dual\" or \"uplink\"."
  value       = var.mode
}

output "uplink_band" {
  description = "Band of the single AP radio in \"uplink\" mode. null in \"dual\" mode, where it doesn't apply."
  value       = var.mode == "uplink" ? var.uplink_band : null
}
