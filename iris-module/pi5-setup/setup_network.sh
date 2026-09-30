#!/usr/bin/env bash
# Pi 5: private Wi-Fi access point for the Pi Zero camera (and the ESP32),
# with DHCP and mDNS, while Ethernet keeps the internet connection.
#   sudo ./setup_network.sh <ssid> <password>
# Result: Wi-Fi <ssid> on wlan0, Pi 5 = 192.168.50.1, clients get
# 192.168.50.x by DHCP, Pi 5 reachable as main-pi.local.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SSID=${1:?usage: setup_network.sh <ssid> <password>}
PASS=${2:?usage: setup_network.sh <ssid> <password>}
[ ${#PASS} -ge 8 ] || { echo "password must be at least 8 characters"; exit 1; }

# "shared" makes NetworkManager run DHCP (dnsmasq) on this interface.
nmcli connection delete ProjectNet >/dev/null 2>&1 || true
nmcli connection add type wifi ifname wlan0 con-name ProjectNet autoconnect yes ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg \
  ipv4.method shared ipv4.addresses 192.168.50.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS"
nmcli connection up ProjectNet

hostnamectl set-hostname main-pi
# Keep 127.0.1.1 in step with the new hostname (sudo warns otherwise).
sed -i '/^127\.0\.1\.1/d' /etc/hosts && echo "127.0.1.1 main-pi" >> /etc/hosts

# Firewall: the LifeLink backend (TCP 3000) and device discovery (UDP 37020)
# only answer this Pi itself and the private Wi-Fi, never the Ethernet
# (hospital) network. The API also checks per-device keys; this is the
# second layer.
apt-get install -y nftables
cat > /etc/nftables.d-lifelink.conf <<'NFT'
table inet lifelink {
  chain input {
    type filter hook input priority 0; policy accept;
    iifname { "lo", "wlan0" } accept
    tcp dport 3000 drop
    udp dport 37020 drop
    tcp dport 8010 drop
  }
}
NFT
grep -q nftables.d-lifelink.conf /etc/nftables.conf || echo 'include "/etc/nftables.d-lifelink.conf"' >> /etc/nftables.conf
systemctl enable nftables
nft delete table inet lifelink 2>/dev/null || true
nft -f /etc/nftables.d-lifelink.conf
apt-get install -y avahi-daemon
systemctl enable --now avahi-daemon
echo "Done: Wi-Fi '$SSID' on 192.168.50.1, hostname main-pi.local"
