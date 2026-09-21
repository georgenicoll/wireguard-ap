# Copy this file out of the git repo (e.g. to the Dropbox wireguard folder
# alongside wireguard-router's own tfvars) and fill in real values there.
# Never commit the copy - it contains the Wi-Fi PSK.

# --- SSH connection to the Pi -------------------------------------------

pi_host = "changeme" # hostname or IP address, reachable over eth0
pi_user = "changeme" # login user on the Pi, needs passwordless sudo
# ssh_private_key_path = "/path/to/id_ed25519"   # leave unset to use the SSH agent

# --- AP mode ---------------------------------------------------------------

mode        = "dual" # "dual" or "uplink"
uplink_band = "5"    # "5" or "2.4" (uplink mode only)

# --- Wi-Fi AP settings -------------------------------------------------------

ssid = "changeme" # SSID broadcast by the AP
psk  = "changeme" # WPA2-PSK passphrase

reg_domain = "GB"

# --- Radios and channels ----------------------------------------------------

if_24 = "wlan0" # onboard radio (brcmfmac)
if_5  = "wlan1" # USB adapter (mt7921u)
ch_24 = 6       # 1, 6 or 11
ch_5  = 36      # 36, 40, 44 or 48 (non-DFS)

# --- AP network --------------------------------------------------------------

ap_net = "10.1.1.0/24" # the Pi is .1, DHCP hands out .2 up to the second-to-last address, netmask is derived
lease  = "15m"

# Joining an upstream Wi-Fi network in "uplink" mode isn't set here - SSH in
# and run ~/uplink_wifi.sh "SSID" "password" once you know the network.

# --- WireGuard uplink to wireguard-router -----------------------------------

# This Pi's peer name in wireguard-router's key store. wga fetches its
# client config automatically via wireguard-router's scripts/wg-peer.sh
# (see WGR_REPO_DIR/WGR_CONFIG/WGR_KEYS in wga's own comments) - nothing
# else to set here, and nothing is stored in this file.
wireguard_client_name = "changeme"
