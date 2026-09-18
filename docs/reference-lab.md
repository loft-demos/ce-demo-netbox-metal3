# The reference lab

Everything in this repo was built and exercised against one environment. The
rest of the docs use placeholders; this page is what those placeholders were
filled in with, so you have a worked example and so nothing gets lost in the
generic pass.

You do not need to reproduce this. You do need the equivalent of each row.

---

## Filled-in `env.sh`

```bash
export PLATFORM_CONTEXT="loft-cluster"      # the KPI cluster, runs vCluster Platform
export METAL3_CONTEXT="spark"               # the Spark cluster, runs Metal3/Ironic
export METAL3_CLUSTER="spark-cluster"       # what the Platform calls it
export NODE_PROVIDER="metal3-us-va-blacksburg-dc1"
export NETBOX_URL="https://netbox.spark.lab.kurtmadel.com"

export PLATFORM_NAMESPACE="vcluster-platform"
export METAL3_NS="metal3-system"
export NETBOX_NAMESPACE="netbox"

export NETBOX_SYNC_TAG="vcluster-sync"
export NETBOX_SITE="us-va-blacksburg-dc1"
export NETBOX_LOCATION="row-1"

export NETBOX_HOSTNAME="netbox.spark.lab.kurtmadel.com"
export STORAGE_CLASS="local-path"
export GATEWAY_NAME="spark"
export GATEWAY_NAMESPACE="envoy-gateway-system"
export GATEWAY_SECTION="https"

export BMH_HOST_LABEL="demo.vcluster.com/bmh-host"
export PROVISION_BRIDGE="br-provision"
export PROVISION_CIDR="172.22.0.0/24"
export BMC_IP_START="172.22.0.3"
export BMC_IP_COUNT="12"
export VM_INVENTORY="$HOME/loft-demos/vmetal-sushy-demo/configs/vm-inventory.txt"
```

---

## The shape of it

**Two clusters.** vCluster Platform runs on the KPI `loft-cluster`. Metal3,
Ironic and every BareMetalHost live on a separate cluster, `spark`, registered
with the Platform as `spark-cluster`. The NetBox sync runs **inside the
Platform**, so the KPI cluster is the one that has to reach NetBox; the Spark
cluster never talks to it.

That split is the interesting part of the reference lab, because it is the
configuration most likely to surprise you: a NetBox that is reachable from where
you are running `kubectl` is not the same thing as a NetBox that is reachable
from the Platform's pods. See runbook step 1.

**One host does the emulation.** The `vmetal` node (an X1 Pro, amd64) runs
libvirt, sushy-tools, and the `br-provision` bridge. Ironic and the DHCP proxy
are pinned to it because they reach sushy at `172.22.0.1:8000` and attach the
host-local `br-provision` network attachment definition. NetBox is pinned to the
same node, but only because it is the amd64 node with NVMe and the DGX node
`spark-w01` has to stay free. NetBox has no host-local dependency of its own.

**The fleet is emulated.** Ten-ish libvirt domains fronted by sushy-tools, from
the `vmetal-sushy-demo` repo. Its
`configs/vm-inventory.txt` is what `hack/seed-netbox.py` reads, and its
`hack/generate-bmh.sh` is the shell-script lane the NetBox import replaces.

**The NodeProvider is GitOps.** `infrastructure/metal3-node-provider.yaml` in a
`platform-config` repo on a self-hosted Forgejo, reconciled by Argo CD with
`selfHeal: true`. That is why the runbook says to merge a fragment and push
rather than `kubectl apply` or `kubectl patch`: a patch gets reverted, usually a
minute after you convince yourself it worked.

---

## The edge

Confirmed 2026-09-16 and unchanged since:

```text
Gateway        spark, namespace envoy-gateway-system
GatewayClass   eg
EnvoyProxy     spark-edge
address        192.168.49.100
listeners      http  *.spark.lab.kurtmadel.com
               https *.spark.lab.kurtmadel.com  (TLS terminate, secret spark-lab-kurtmadel-com-tls)
allowedRoutes  namespaces.from: All
```

Routes attach from the `netbox` namespace with no ReferenceGrant, and
`netbox.spark.lab.kurtmadel.com` falls under the wildcard, so
`manifests/netbox/values.yaml` needed no edit for the edge.

The Spark edge sits on `192.168.49.0/24` while the vMetal host's LAN is
`192.168.50.0/24`, which is why runbook step 1 makes you prove the Platform
cluster can actually reach the NetBox URL before going further.

---

## The GPU, and why the seed script is careful about it

The `vmetal` host owns exactly **one** physical accelerator, an RTX 2000 Ada,
passed through to a single libvirt domain. There are no H100s anywhere in this
lab.

That is why `hack/seed-netbox.py` treats a GPU as a fact about one named device
rather than about a size class: seeding a whole `xlarge` class as `gpu-compute`
would put machines in a GPU pool that cannot run a GPU workload, and the failure
would show up as a mysteriously unschedulable tenant node rather than as
anything NetBox or the platform complains about.

The two naming schemes in that lab do not match, which is its own trap: the
inventory names the machine `rack-c-u12-xlarge-gpu` while virsh calls the domain
`vmetal-xlarge-gpu-1`. The UUID column joins them, and the UUID is what gets
written as the NetBox device serial and served by sushy-tools as the Redfish
system id.

```bash
# on the host: which domain actually holds the card?
lspci -nn | grep -i nvidia
for d in $(virsh list --all --name); do
  virsh dumpxml "$d" | grep -q "<hostdev" && echo "$d has a PCI hostdev"
done
```

---

## Addressing

```text
172.22.0.0/24     br-provision on the vmetal host
172.22.0.1        the bridge itself; sushy-tools listens on 0.0.0.0:8000
172.22.0.2        DHCP proxy VIP
172.22.0.3-.14    emulated BMC aliases (hack/bmc-ip-aliases.sh, 12 of them)
172.22.0.11-.20   static IPs the LEGACY generate-bmh.sh hosts carry
172.22.0.23-.250  the NodeProvider's own provisioning IPAM range
```

Note the overlap: while both lanes coexist, the BMC aliases at `.11` through
`.14` collide with four hand-rolled hosts. Runbook step 3 covers what that does
and how to avoid it.

---

## Node labels, which read backwards

Two labels in this lab are named in a way that will mislead you:

- `demo.vcluster.com/bmh-host=true` is on `vmetal`, and `vmetal` **is** the
  Metal3 host.
- `demo.vcluster.com/vmetal-host=true` is on `vmetal-w02`, and `vmetal-w02` is
  **not** the Metal3 host. It is the NiCo host. See
  [nico-and-other-providers.md](nico-and-other-providers.md).

`env.example` calls the first one `BMH_HOST_LABEL` for that reason: what matters
is which node owns the provisioning bridge, not what the label happens to be
called.

---

## Related repos

| Repo | What it is |
| --- | --- |
| `vmetal-sushy-demo` | the emulated fleet: libvirt, sushy-tools, `br-provision`, the image server, the Metal3/Ironic deployment, the OS images and the templates |
| `platform-config` | the GitOps source for the live NodeProvider, Argo CD `selfHeal: true` |

Nothing in `vmetal-sushy-demo` moves when you adopt the NetBox import.
`generate-bmh.sh` stays in place as the fallback lane, and every other piece of
that demo is untouched.
