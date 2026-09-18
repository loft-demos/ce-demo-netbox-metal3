#!/usr/bin/env bash
# bmc-ip-aliases.sh -- give every EMULATED BMC its own address on the
# provisioning bridge.
#
# EMULATED FLEETS ONLY. Real BMCs already have their own addresses.
#
# sushy-tools listens on 0.0.0.0:8000, so every address on the provisioning
# bridge answers Redfish for every libvirt domain. Handing each NetBox device
# its own BMC IP is what lets NetBox's IPAM stay honest (ENFORCE_GLOBAL_UNIQUE
# is on by default) and makes the demo read like real hardware.
#
# The range has to clear three things, and the script does not check any of
# them for you:
#   1. the bridge's own address
#   2. any DHCP proxy VIP on that bridge
#   3. the NodeProvider's provisioning IPAM range
#      (spec.properties."metal3.vcluster.com/network-ip-range")
# It also will not notice if it lands on addresses your existing hand-rolled
# BareMetalHosts already use statically. That collision fails silently and
# expensively -- see docs/runbook.md step 3.
#
# Run on the host that owns the bridge. Configured by env.sh, or by the
# defaults below:
#   PROVISION_BRIDGE  (default br-provision)
#   BMC_IP_START      (default 172.22.0.3)
#   BMC_IP_COUNT      (default 12)
#   BMC_IP_PREFIX_LEN (default 24)
#
#   sudo -E bash hack/bmc-ip-aliases.sh apply     # add now + install systemd unit
#   sudo -E bash hack/bmc-ip-aliases.sh status
#   sudo -E bash hack/bmc-ip-aliases.sh remove
#
# `sudo -E` so a sourced env.sh survives into the script.
set -euo pipefail

BRIDGE="${PROVISION_BRIDGE:-br-provision}"
START="${BMC_IP_START:-172.22.0.3}"
COUNT="${BMC_IP_COUNT:-12}"
PREFIX_LEN="${BMC_IP_PREFIX_LEN:-24}"
UNIT=/etc/systemd/system/bmc-ip-aliases.service

addresses() {
  local base="${START%.*}" last="${START##*.}"
  for ((i = 0; i < COUNT; i++)); do
    echo "${base}.$((last + i))/${PREFIX_LEN}"
  done
}

require_bridge() {
  ip link show "${BRIDGE}" >/dev/null 2>&1 ||
    { echo "ERROR: bridge ${BRIDGE} does not exist. Create the provisioning bridge first, or set PROVISION_BRIDGE." >&2; exit 1; }
}

case "${1:-status}" in
  apply)
    require_bridge
    while read -r addr; do
      if ip addr show dev "${BRIDGE}" | grep -q " ${addr%/*}/"; then
        echo "= ${addr} already on ${BRIDGE}"
      else
        ip addr add "${addr}" dev "${BRIDGE}"
        echo "+ ${addr} added to ${BRIDGE}"
      fi
    done < <(addresses)

    # Survive a reboot. Whatever creates the bridge still has to create it;
    # this unit only re-adds the aliases on top of it, before sushy starts.
    {
      echo "[Unit]"
      echo "Description=Emulated BMC address aliases on ${BRIDGE}"
      echo "After=network-online.target"
      echo "Wants=network-online.target"
      echo "Before=sushy-tools.service"
      echo
      echo "[Service]"
      echo "Type=oneshot"
      echo "RemainAfterExit=yes"
      while read -r addr; do
        echo "ExecStart=/sbin/ip addr replace ${addr} dev ${BRIDGE}"
      done < <(addresses)
      echo
      echo "[Install]"
      echo "WantedBy=multi-user.target"
    } > "${UNIT}"
    systemctl daemon-reload
    systemctl enable bmc-ip-aliases.service
    echo "installed ${UNIT}"
    ;;

  remove)
    require_bridge
    systemctl disable --now bmc-ip-aliases.service 2>/dev/null || true
    rm -f "${UNIT}"
    systemctl daemon-reload
    while read -r addr; do
      ip addr del "${addr}" dev "${BRIDGE}" 2>/dev/null && echo "- ${addr} removed" || true
    done < <(addresses)
    ;;

  status)
    ip -4 addr show dev "${BRIDGE}" | awk '/inet /{print "  " $2}'
    echo "--- Redfish answers on each? ---"
    while read -r addr; do
      ip="${addr%/*}"
      if curl -fsS -m 3 "http://${ip}:8000/redfish/v1/Systems" >/dev/null 2>&1; then
        echo "  ok   ${ip}:8000"
      else
        echo "  FAIL ${ip}:8000"
      fi
    done < <(addresses)
    ;;

  *)
    echo "usage: $0 {apply|remove|status}" >&2
    exit 1
    ;;
esac
