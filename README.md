# ce-demo-netbox-metal3

NetBox as the inventory source for a Metal3 NodeProvider.

**Feature:** NetBox import on the Metal3 NodeProvider, new in vCluster Platform
**4.13.0-alpha.12**. Requires the Metal3 feature on the licence.

---

## What it does

Tag a device in NetBox. On the next sync (the NetBox syncer runs every minute) the platform creates a `Machine`. That
happens first and it happens regardless: the Machine is the import made
visible, and it exists whether or not NetBox knows enough to boot the thing.

What follows depends on the record. If it is complete, the platform writes the
BMC credentials Secret and then registers a `BareMetalHost` in the Metal3
namespace on the provider's cluster, in that order, and the Machine goes
`Registered=True`. If it is not complete, the Machine sits `Pending` with the
missing fields named in its message and **no host and no Secret are created at
all**: the platform will not hand Ironic a host it knows will fail to register.
Fix NetBox and the host appears on the next sync.

No per-host Secret is written at all if your provider's
`bareMetalHostTemplate` names an existing one with `bmc.credentialsName`. That
is the supported way to keep BMC passwords out of NetBox, and it also drops the
BMC login out of the completeness check above.

Rack, role, device type and site from the DCIM record are stamped on as labels
the NodeType pools select on, for every value that is a valid Kubernetes label
value. Anything else is dropped rather than mangled, so a selector never
matches something it should not. Rack is the one to watch: it takes the NetBox
rack **name**, not its slug, and a name with a space in it is not a valid label
value, so the label is simply absent and a pool selecting on it matches
nothing.

Untag the device and a free machine goes away again, host and Secret included.
If something is using it, the platform keeps it and marks it `Orphaned` rather
than deprovisioning hardware because somebody edited a records database.

The platform only ever **reads** NetBox. There is no write path: no `POST`, no
`PATCH`, no token provisioning. NetBox stays the operator's record.

The demo beat worth showing is not the install. It is the loop: untag a device,
watch the host disappear; retag it, watch it come back; then do it to a machine
that is running a tenant node and watch the platform refuse.

---

## Who this is for

Anyone standing up the NetBox import: on real hardware with real BMCs, or on an
emulated fleet if you do not have a rack handy.

Two lanes run through the docs, marked where they differ:

| | Real BMCs | Emulated fleet (sushy-tools + libvirt) |
| --- | --- | --- |
| Seeding NetBox | however you already do it, or adapt `hack/seed-netbox.py` | `hack/seed-netbox.py` reads the libvirt inventory |
| BMC addressing | already unique per machine | `hack/bmc-ip-aliases.sh` gives each one its own IP |
| `addressTemplate` | vendor-specific, often the platform default works | must render the Redfish system id (see below) |
| Everything else | identical | identical |

Emulating with **kubevirtbmc** instead of sushy-tools works too, and drops two
of the three traps below. It has one of its own, because a virtbmc Service is
addressed by DNS name and the import requires an IP. See
[docs/kubevirtbmc.md](docs/kubevirtbmc.md) before you seed anything.

The platform side is the same either way, and so is everything in
[docs/netbox-data-model.md](docs/netbox-data-model.md) and
[docs/troubleshooting.md](docs/troubleshooting.md).

---

## Make it yours

Every environment-specific value lives in one file:

```bash
cp env.example env.sh
$EDITOR env.sh          # clusters, NetBox URL, NodeProvider, site, tokens
source env.sh
```

`hack/verify-netbox-import.sh` and `hack/seed-netbox.py` read those variables,
the docs use the same names, and the manifests carry the same placeholders. If
a command in the runbook does not work after sourcing `env.sh`, that is a bug in
this repo rather than something for you to hand-edit.

The concrete lab all of this was built and verified against, if you want a
worked example of what those values look like filled in, is in
[docs/reference-lab.md](docs/reference-lab.md).

---

## The three things that will bite you

1. **Paste the API token without its scheme word.** NetBox shows a new token
   once, as the complete header value: `Bearer nbt_<key>.<secret>` for a v2
   token or `Token <40 chars>` for a v1 one. `apiToken` takes only the part
   after the space. Either version works, because NetBox reads the version off
   the value rather than the keyword, but pasting the whole banner sends
   `Authorization: Token Bearer nbt_...` and gets a 403. The chart's
   `superuser.apiToken` value cannot create the token for you either: on NetBox
   4.7 it is accepted and silently ignored.

2. **Check whether the BMC address template has a usable default.** The
   platform default is `redfish://{{ .Address }}/redfish/v1/Systems/1`, which is
   right for a lot of real BMCs and wrong for anything that keys systems by an
   id. sushy-tools keys them by libvirt domain UUID, so that lane needs the UUID
   stored as the NetBox device **serial** and rendered:
   `redfish+http://{{ .Address }}:8000/redfish/v1/Systems/{{ .Serial }}`.
   kubevirtbmc does not have this problem and has a different one
   ([docs/kubevirtbmc.md](docs/kubevirtbmc.md)).

3. **Existing BareMetalHosts block the import.** A host the import did not
   create is not its to write to, so a host you made by hand under the name a
   NetBox device wants produces a `HostConflict` rather than an adoption. Decide
   before you wire the provider. See
   [runbook step 6](docs/runbook.md#6-decide-what-happens-to-existing-baremetalhosts).

---

## What is in here

```text
env.example                 every value that is specific to your environment
docs/
  runbook.md                install -> seed -> wire -> verify -> demo -> roll back
  netbox-data-model.md      what the platform reads, and the five things a device must have
  troubleshooting.md        every condition and event the sync can raise, and the fix
  kubevirtbmc.md            running it against kubevirtbmc instead of sushy-tools
  nico-and-other-providers.md  why the import is Metal3-only today
  reference-lab.md          the concrete environment this was built and verified on
manifests/
  netbox/values.yaml                            Helm values, chart 8.3.77 / NetBox v4.7.0
  platform/netbox-connector-secret.yaml         the connector Secret shape
  platform/vmetal-lan-only-postboot-machineconfigtemplate.yaml
                                                optional: networkData from inspected non-PXE NIC
  platform/metal3-node-provider-netbox.fragment.yaml
                                                the two blocks to merge into your NodeProvider
  platform/metal3-node-provider-netbox.example.yaml
                                                a complete worked NodeProvider, for diffing
  metal3/shared-bmc-creds-secret.yaml           optional: no BMC passwords in NetBox
  metal3/provisioning-ip-annotator-cronjob.yaml optional: patch imported BMHs with an inspection DHCP address
hack/
  seed-netbox.py            build a NetBox record from a libvirt inventory (idempotent)
  bmc-ip-aliases.sh         give each emulated BMC its own address on the provisioning bridge
  verify-netbox-import.sh   what actually landed, both clusters
```

Nothing here installs or configures Metal3, Ironic, or the NodeProvider itself.
It assumes you have a working Metal3 NodeProvider that already provisions
machines, and it replaces where that provider's inventory comes from.

---

## Quick path

With `env.sh` sourced:

```bash
# 1. NetBox, on whichever cluster you want it (not necessarily the Platform's)
helm upgrade --install netbox oci://ghcr.io/netbox-community/netbox-chart/netbox \
  --version 8.3.77 -n "$NETBOX_NAMESPACE" --create-namespace \
  -f manifests/netbox/values.yaml \
  --set superuser.password="$NETBOX_ADMIN_PASSWORD"

# 2. In the NetBox UI, create two API tokens: one read-only for the platform,
#    one write-enabled for seeding. See "three things" above about pasting them.

# 3. Emulated fleet only: give each BMC its own address, then seed the record
sudo -E bash hack/bmc-ip-aliases.sh apply          # on the host running sushy-tools
python3 hack/seed-netbox.py --inventory "$VM_INVENTORY" --with-modules

# 4. Register the connector on the Platform cluster
kubectl --context "$PLATFORM_CONTEXT" -n "$PLATFORM_NAMESPACE" \
  create secret generic netbox \
  --from-literal=url="$NETBOX_URL" \
  --from-literal=apiToken="$NETBOX_PLATFORM_TOKEN" \
  --from-literal=insecure=false
kubectl --context "$PLATFORM_CONTEXT" -n "$PLATFORM_NAMESPACE" \
  label secret netbox loft.sh/connector-type=netbox

# 5. Optional Platform-side template for the reference lab's two-NIC shape.
#    Skip this if your existing networkData already targets the right NIC.
kubectl --context "$PLATFORM_CONTEXT" \
  apply -f manifests/platform/vmetal-lan-only-postboot-machineconfigtemplate.yaml

# 6. Merge manifests/platform/metal3-node-provider-netbox.fragment.yaml into
#    your NodeProvider and apply it the way you normally apply it

# 7. Metal3 cluster: bridge the current import gap until the platform allocates
#    inspection DHCP addresses when it registers BareMetalHosts.
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" \
  apply -f manifests/metal3/provisioning-ip-annotator-cronjob.yaml

# 8. Verify
bash hack/verify-netbox-import.sh
```

Full detail, including what to do about hosts that already exist in the Metal3
namespace, is in [docs/runbook.md](docs/runbook.md).

---

## Do you need `vmetal-lan-only-postboot`?

Only if your claim-time `networkData` needs to target a different NIC than the
PXE/provisioning NIC, and you do not already have a template that does that.
The reference sushy-tools lab has two NICs: `eth0`/PXE on the provisioning
network, and `eth1` on the tenant/LAN network. The platform's default bare-metal
networkData uses `spec.bootMACAddress`, which is the PXE NIC, so this repo
includes `vmetal-lan-only-postboot` to choose the first inspected non-PXE NIC
instead.

You probably do **not** need it if your machines have one NIC and that one
network can carry PXE/inspection, Ironic callbacks, the installed node IP, and
the tenant cluster join path. You also do not need it if your existing
`MachineConfigTemplate` already names the correct interface or MAC source. Keep
that template and do not copy the `vcluster.com/network-data-template-config`
property from the worked example.

This is separate from the provisioning IP annotator. Imported hosts still need
`metal3.vcluster.com/ip-address` before first inspection until the platform
allocates that address at BareMetalHost registration time.

---

## What was verified, and what was not

Read directly out of vCluster Platform source code and the NetBox 4.7 source, so
these are facts about the implementation rather than observations: the
token-scheme behaviour, the boot-MAC selection rule, the five missing-requirement
checks, the host-conflict and orphan rules, the label set, and the
`online: false` default.

Verified by running it: the Helm values render clean against chart 8.3.77; the
seeding script is idempotent; the import creates Machines, BareMetalHosts and
BMC Secrets from tagged devices and releases them on untag; imported hosts need
`metal3.vcluster.com/ip-address` before first inspection, so
[manifests/metal3/provisioning-ip-annotator-cronjob.yaml](manifests/metal3/provisioning-ip-annotator-cronjob.yaml)
patches that current platform timing gap; the lab network-data template now
derives the LAN MAC from the host's inspected non-PXE NIC.

Not verified anywhere here: any real BMC. Everything in this repo was exercised
against sushy-tools. The platform code path is the same, but vendor Redfish
implementations differ in exactly the place `addressTemplate` exists to absorb,
so budget time for that line if you are pointing this at a real rack.
