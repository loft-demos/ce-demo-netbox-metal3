#!/usr/bin/env bash
# verify-netbox-import.sh -- check what the NetBox import actually produced.
#
# Two contexts, two halves:
#   PLATFORM_CONTEXT  where vCluster Platform and its Machines live
#   METAL3_CONTEXT    where metal3-system and the BareMetalHosts live
#
# They are often the same cluster; set both anyway, so the output says which
# half of the story each block is telling. Leave either empty to use the current
# kube context.
#
#   source env.sh && bash hack/verify-netbox-import.sh
#
# or ad hoc:
#
#   PLATFORM_CONTEXT=platform METAL3_CONTEXT=metal3 NODE_PROVIDER=metal3-dc1 \
#     bash hack/verify-netbox-import.sh
set -uo pipefail

PLATFORM_CONTEXT="${PLATFORM_CONTEXT:-}"
METAL3_CONTEXT="${METAL3_CONTEXT:-}"
NODE_PROVIDER="${NODE_PROVIDER:-}"
METAL3_NS="${METAL3_NS:-metal3-system}"

kp() { kubectl ${PLATFORM_CONTEXT:+--context "${PLATFORM_CONTEXT}"} "$@"; }
km() { kubectl ${METAL3_CONTEXT:+--context "${METAL3_CONTEXT}"} "$@"; }

hr() { printf '\n== %s\n' "$*"; }

# There is no sensible default for a NodeProvider name, so ask rather than
# guess: a typo'd provider name produces empty output that reads exactly like
# "the import is not working".
if [[ -z "${NODE_PROVIDER}" ]]; then
  echo "NODE_PROVIDER is not set. Source env.sh, or pick one:" >&2
  kp get nodeprovider -o custom-columns='NAME:.metadata.name,TYPE:.spec.type,PHASE:.status.phase' 2>&1 | sed 's/^/  /' >&2
  exit 1
fi

hr "NodeProvider ${NODE_PROVIDER}"
kp get nodeprovider "${NODE_PROVIDER}" \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason,MESSAGE:.status.message'
kp get nodeprovider "${NODE_PROVIDER}" -o jsonpath='{.spec.metal3.netBox}' 2>/dev/null \
  | python3 -m json.tool 2>/dev/null \
  || echo "  (no spec.metal3.netBox -- the import is not configured on this provider)"

hr "Recent NetBox sync events on the provider"
# The syncer reports every setup problem here: NetBoxTagMissing,
# NetBoxConnectorInvalid, NetBoxHostTemplateInvalid, NetBoxHostConflict.
kp get events --field-selector "involvedObject.name=${NODE_PROVIDER}" \
  --sort-by=.lastTimestamp 2>/dev/null | tail -15

hr "Imported Machines"
kp get machines.management.loft.sh -l machines.vcluster.com/source=netbox \
  -L netbox.vcluster.com/rack,netbox.vcluster.com/device-type,netbox.vcluster.com/role,netbox.vcluster.com/provisionable

hr "Machines NetBox cannot yet describe well enough to boot"
# provisionable=false means the platform found a missing requirement: no BMC
# address, no boot MAC, no serial, or no BMC login. The Registered condition
# says which. This is the normal, useful failure mode.
notready="$(kp get machines.management.loft.sh \
  -l machines.vcluster.com/source=netbox,netbox.vcluster.com/provisionable=false \
  -o name 2>/dev/null)"
if [[ -z "${notready}" ]]; then
  echo "  none -- every imported device carries what a provisioner needs"
else
  for machine in ${notready}; do
    echo "--- ${machine}"
    kp get "${machine}" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}'
  done
fi

hr "BareMetalHosts created by the import (${METAL3_NS})"
km -n "${METAL3_NS}" get baremetalhost -l machines.vcluster.com/source=netbox \
  -L netbox.vcluster.com/rack,netbox.vcluster.com/device-type

hr "BMC addresses and credentials Secrets"
# ONLINE should be false. The import creates hosts powered off, which is what
# the claim path needs; `true` here means something pinned online: true in the
# bareMetalHostTemplate.
km -n "${METAL3_NS}" get baremetalhost -l machines.vcluster.com/source=netbox \
  -o custom-columns='NAME:.metadata.name,ONLINE:.spec.online,BMC:.spec.bmc.address,CREDS:.spec.bmc.credentialsName,BOOTMAC:.spec.bootMACAddress'

hr "BareMetalHosts the import did NOT create"
# A host here under a name a tagged NetBox device also wants is a HostConflict
# waiting to happen: the import will not adopt it and will not overwrite it.
km -n "${METAL3_NS}" get baremetalhost \
  -o jsonpath='{range .items[?(@.metadata.labels.machines\.vcluster\.com/source!="netbox")]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
  | grep -v '^$' || echo "  none -- every host in the namespace came from the import"

hr "Machines the import kept but NetBox no longer selects"
# SourceAttached=False/Orphaned: the tag was removed while the machine was in
# use, so the sync refused to deprovision it. This is the demo beat, not a bug.
kp get machines.management.loft.sh -l machines.vcluster.com/source=netbox \
  -o jsonpath='{range .items[?(@.status.conditions[?(@.type=="SourceAttached")].status=="False")]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
  | grep -v '^$' || echo "  none"
