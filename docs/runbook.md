# Runbook: NetBox as the inventory source for a Metal3 NodeProvider

**Requires:** vCluster Platform **4.13.0-alpha.12** or newer, with the Metal3
feature on the licence, and a Metal3 NodeProvider that already provisions
machines.
**NetBox:** community chart `netbox` 8.3.77 -> NetBox **v4.7.0** or newer.
**Related:** [netbox-data-model.md](netbox-data-model.md),
[troubleshooting.md](troubleshooting.md),
[reference-lab.md](reference-lab.md).

Source `env.sh` before you start (`cp env.example env.sh`, edit, `source
env.sh`). Every command below uses those variables verbatim.

---

## 0. What changes, and what does not

Before this runbook, something else is your inventory: a spreadsheet, a
generator script, a folder of hand-written BareMetalHost YAML. Whatever it is,
adding a machine means writing Kubernetes objects.

After this runbook, **NetBox is the inventory**. You tag a device and the
platform creates a Machine, and then, if the record carries everything a
provisioner needs, the BMC credentials Secret and the BareMetalHost. You untag
it and the platform takes them away again, unless something is using the
machine, in which case it keeps it and says so.

The Machine appearing is not the same thing as the host appearing. A device
NetBox cannot fully describe still gets a Machine, sitting `Pending` with the
missing fields named; it gets no BareMetalHost and no Secret until you fix the
record. That is the useful failure mode, and step 5 is about staying out of it.

Nothing else moves. Your Metal3/Ironic deployment, your provisioning network,
your OS images and your templates are all untouched, and whatever you use today
stays in place as a fallback lane.

One correctness improvement comes free: the import creates hosts with
`spec.online: false`, which is the state vMetal's claim path actually wants. A
generator that sets `online: true` gives you hosts that reach `available` and
then never bind, which is a genuinely annoying thing to debug.

The platform only ever **reads** NetBox. There is no write path: no `POST`, no
`PATCH`, no token provisioning. NetBox stays the operator's record.

---

## 1. Get NetBox running somewhere the Platform can reach

**If you already have NetBox, skip to the reachability check at the end of this
step.** It is the part that catches people, and it catches them regardless of
who installed NetBox.

NetBox does not have to live on any particular cluster. It does have to be
reachable **from the vCluster Platform pods**, because that is where the sync
runs. A NetBox you can curl from your laptop is not evidence.

### Installing it from here

```bash
kubectl --context "$METAL3_CONTEXT" get storageclass
# manifests/netbox/values.yaml assumes $STORAGE_CLASS; change persistence.storageClass
# and postgresql.primary.persistence.storageClass if yours is named differently
```

`manifests/netbox/values.yaml` pins every workload to one node with
`$BMH_HOST_LABEL`. That is a choice, not a requirement: NetBox has no host-local
dependency the way Ironic and the DHCP proxy do. Drop the `nodeSelector` blocks
if you do not want it, but keep the workload and its PVs pinned together, or
pinned to nothing at all. A node-affine `local-path` volume plus a nodeSelector
that disagrees with it is a `Pending` pod with an unhelpful message.

The chart's HTTPRoute wants an existing Gateway:

```bash
kubectl --context "$METAL3_CONTEXT" -n "$GATEWAY_NAMESPACE" get gateway "$GATEWAY_NAME" \
  -o jsonpath='{range .spec.listeners[*]}{.name}{"\t"}{.hostname}{"\t"}{.allowedRoutes.namespaces.from}{"\n"}{end}'
```

Check three things in that output: a listener named `$GATEWAY_SECTION` exists,
its hostname covers `$NETBOX_HOSTNAME`, and it takes routes from the `netbox`
namespace. `from: All` needs nothing further; `from: Same` or a selector needs a
ReferenceGrant or a different namespace. If you use classic Ingress instead, set
`ingress.enabled` and drop the `httpRoute` block.

```bash
helm upgrade --install netbox \
  oci://ghcr.io/netbox-community/netbox-chart/netbox \
  --version 8.3.77 \
  --namespace "$NETBOX_NAMESPACE" --create-namespace \
  -f manifests/netbox/values.yaml \
  --set superuser.password="$NETBOX_ADMIN_PASSWORD"

kubectl --context "$METAL3_CONTEXT" -n "$NETBOX_NAMESPACE" get pods -o wide -w
```

Expect five workloads: `netbox`, `netbox-worker`, `netbox-postgresql-0`,
`netbox-valkey-primary-0`, and the `netbox-housekeeping` CronJob.

> **Bitnami subcharts pull `:latest`.** Since Docker Hub's August 2025 change
> the `postgresql` and `valkey` subcharts reference `bitnami/postgresql:latest`
> and `bitnami/valkey:latest`. That works and is multi-arch, but it is not
> pinned. If reproducibility matters, mirror those images and set
> `postgresql.image.*` / `valkey.image.*`.

First login is `admin` with `$NETBOX_ADMIN_PASSWORD`.

### The reachability check, which is not optional

```bash
kubectl --context "$PLATFORM_CONTEXT" -n "$PLATFORM_NAMESPACE" run netbox-reach \
  --rm -i --restart=Never --image=curlimages/curl -- \
  curl -sS -o /dev/null -w 'status %{http_code}\n' "$NETBOX_URL/api/"
```

Any HTTP status proves routing and TLS trust. NetBox answers `403` to an
unauthenticated API call, which is a pass. A DNS failure, a connection timeout
or a certificate error is not; fix it before going further, because it is the
one thing the sync cannot report usefully. It just retries, quietly, forever.

If NetBox and the Platform are on different networks, this is where you find
out. In the reference lab they are, which is why this check exists.

---

## 2. Create the API token

Do this before anything else touches NetBox. `hack/seed-netbox.py` and the
NodeProvider both authenticate with a token, and it is the one piece of setup no
script in this repo can create for you.

Either token version works. NetBox reads the version off the **value**, not off
the scheme keyword: a value starting `nbt_` is a v2 token, anything else is v1,
and both `Token` and `Bearer` are accepted as the keyword. The platform sends
`Authorization: Token <apiToken>`, so a v2 token authenticates fine provided the
whole `nbt_<key>.<secret>` string is what lands in `apiToken`.

The thing that actually catches people is the copy-paste. NetBox shows a new
token exactly once, as the **complete header value**:

```text
v2:  Bearer nbt_aB3dE5gH7jK9.LmN0pQr...
v1:  Token  aB3dE5gH7jK9LmN0pQrS...
```

`apiToken` takes only what follows the space. Paste the banner verbatim and the
platform sends `Authorization: Token Bearer nbt_...`, which NetBox rejects with
`Invalid authorization header: Must be in the form "Bearer <key>.<token>" or
"Token <token>"`.

In NetBox: your user menu -> **API Tokens** -> **Add a token**:

- Version: either. **v1 is the better choice for this connector**, for the
  reason below.
- Write enabled: **off** for the platform's token (the platform only reads)
- Expires: blank
- Description: something that names the consumer, e.g. `vCluster Platform`

Create a **second** token, **write enabled**, for the seeding script. One token
for both works fine in a lab; two makes the read-only claim about the platform
verifiable, and it is the sort of thing a customer will ask about.

> **Why v1 is the better default here.** A v2 token is stored as an HMAC digest
> keyed by `API_TOKEN_PEPPERS`, which lives in NetBox's configuration rather
> than its database. Rebuild the release without carrying the peppers over, or
> move the database under a different NetBox deployment, and every v2 token
> stops validating. A v1 token stores its plaintext in the database and
> survives both. For a connector nobody wants to think about again, that
> matters more than the stronger at-rest story.

> The chart's `superuser.apiToken` value cannot do this for you. On NetBox 4.7
> the container only creates a token when both `superuser_api_token` and
> `superuser_api_key` are mounted, and the chart mounts only the first, so the
> value is accepted and quietly ignored.

Put both in `env.sh` (`NETBOX_PLATFORM_TOKEN` read-only,
`NETBOX_TOKEN` write-enabled) and re-source it.

Sanity-check the token the way the platform will:

```bash
curl -fsS -H "Authorization: Token $NETBOX_PLATFORM_TOKEN" \
  "$NETBOX_URL/api/dcim/devices/?limit=1" | head -c 200
```

A 403 here is almost always the paste: `Invalid authorization header` means the
scheme word came along with the value, and `Invalid v1 token` means a v2 value
lost its `nbt_<key>.` prefix. Nothing downstream will work until this succeeds.

### Register the connector on the Platform cluster

```bash
kubectl --context "$PLATFORM_CONTEXT" -n "$PLATFORM_NAMESPACE" \
  create secret generic netbox \
  --from-literal=url="$NETBOX_URL" \
  --from-literal=apiToken="$NETBOX_PLATFORM_TOKEN" \
  --from-literal=insecure=false

kubectl --context "$PLATFORM_CONTEXT" -n "$PLATFORM_NAMESPACE" \
  label secret netbox loft.sh/connector-type=netbox

kubectl --context "$PLATFORM_CONTEXT" -n "$PLATFORM_NAMESPACE" \
  annotate secret netbox loft.sh/display-name="NetBox"
```

The label is what makes it a connector; without it the platform refuses the
Secret by name rather than by content, and the UI will not list it either. See
`manifests/platform/netbox-connector-secret.yaml` for the shape, or create it in
the UI under **Connectors -> NetBox -> Connect NetBox**.

Set `insecure=true` if NetBox serves a self-signed certificate.

---

## 3. Emulated fleet only: give every BMC its own address

**Skip this step entirely if your BMCs are real.** Real BMCs already have their
own addresses, which is the whole point of them.

**Skip it for kubevirtbmc too.** It gives every machine its own BMC Service, so
there is nothing to alias. See [kubevirtbmc.md](kubevirtbmc.md), which also
covers the problem you get instead.

NetBox's `ENFORCE_GLOBAL_UNIQUE` is on by default, so ten devices cannot all
record the same out-of-band IP. sushy-tools listens on `0.0.0.0:8000`, so the
fix is to put several addresses on the provisioning bridge. Each one answers
Redfish for the whole fleet, and each NetBox device points at its own.

Run on the host that owns the bridge:

```bash
sudo -E bash hack/bmc-ip-aliases.sh apply
sudo -E bash hack/bmc-ip-aliases.sh status
```

That adds `$BMC_IP_COUNT` addresses starting at `$BMC_IP_START` to
`$PROVISION_BRIDGE` and installs a `bmc-ip-aliases.service` unit so they come
back after a reboot, ordered before `sushy-tools.service`. (`sudo -E` so the
variables survive; the script also has defaults if you would rather pass none.)

The range has to clear three things: the bridge address itself, any DHCP proxy
VIP, and the NodeProvider's own provisioning IPAM range. Check the last one:

```bash
kubectl --context "$PLATFORM_CONTEXT" get nodeprovider "$NODE_PROVIDER" \
  -o jsonpath='{.spec.properties.metal3\.vcluster\.com/network-ip-range}{"\n"}'
```

> **It may not clear your existing lane.** If you already have BareMetalHosts
> carrying static provisioning addresses, the aliases can land on top of them.
> The host then answers ARP for addresses those machines want, and an IPA
> ramdisk handed a colliding address cannot call its result back to Ironic. The
> failure is silent: the BareMetalHost sits in `deprovisioning` with
> `errorCount: 0` and an empty `errorMessage` while Ironic waits in clean wait
> for a callback that never arrives.
>
> Drop the colliding ones for as long as the old hosts exist:
>
> ```bash
> sudo ip addr del 172.22.0.11/24 dev "$PROVISION_BRIDGE"    # etc
> ```
>
> Step 6's clean cut removes the static range along with the hand-rolled hosts,
> after which `sudo -E bash hack/bmc-ip-aliases.sh apply` restores the full set
> safely.

Verify each address really answers:

```bash
bash hack/bmc-ip-aliases.sh status
```

<details>
<summary>Shortcut if you would rather not touch the host</summary>

Set `enforceGlobalUnique: false` in the NetBox values, re-run `helm upgrade`,
and point every device at the bridge address. You still need one `IPAddress`
object per device (`Device.oob_ip` is one-to-one), they just no longer have to
be unique. Pass `--bmc-ip-start` at the bridge address and edit the script's
`ip_add` to stop incrementing. It works; it also makes the NetBox record a lie,
which is an odd thing to demo about a source of truth.
</details>

---

## 4. Create the sync tag and the BMC custom fields

**If you are running `hack/seed-netbox.py` in step 5, there is nothing to do
here.** It creates the tag and both custom fields for you, idempotently, in its
`[1/6]` block. This section exists so you can do it by hand once, which is also
what you want if your NetBox is already populated with real devices.

**Customization -> Tags -> Add:** name and slug `$NETBOX_SYNC_TAG`.

The platform only ever reads NetBox, so it will not create the tag itself. If it
is missing at import time you get a `NetBoxTagMissing` warning event on the
NodeProvider rather than a generic connector failure.

**Customization -> Custom Fields -> Add**, twice, both on object type
`DCIM > device`, type `Text`:

| Name | Label |
| --- | --- |
| `bmc_username` | BMC username |
| `bmc_password` | BMC password |

The names are configurable on the provider
(`spec.metal3.netBox.customFields`); these are the defaults.

> These are text fields. NetBox stores and displays the password in the clear to
> anyone who can view the device. The platform never serves it back (it is
> stripped from the machine projection), but NetBox will. If that is not
> acceptable, use the shared-Secret variant in
> `manifests/metal3/shared-bmc-creds-secret.yaml` and skip both fields, or point
> `customFields` at fields your NetBox already protects.

---

## 5. Get the fleet into NetBox

The platform needs five things per device. They are listed in
[netbox-data-model.md](netbox-data-model.md#3-the-five-things-a-device-must-have)
and it is worth reading that section before seeding anything, because a device
missing any of them gets **no BareMetalHost at all** and sits `Pending` with the
list in its message.

| Requirement | Satisfied by |
| --- | --- |
| BMC interface | `oob_ip` on the device, **or** any interface with `mgmt_only: true` |
| BMC address | an actual IP on one of those |
| Boot MAC | a MAC on a non-management, non-fabric interface, **or** `bootMACAddress` pinned in the provider's host template |
| Serial | a non-empty device serial |
| BMC credentials | both custom fields, **or** `bmc.credentialsName` pinned in the provider's host template |

### If your devices are already in NetBox

Add the tag, confirm the five requirements, and go to step 6. That is the entire
adoption path: the import reads what a DCIM already holds.

### If you are seeding an emulated fleet

`hack/seed-netbox.py` builds the whole record from a **libvirt** inventory. It
is stdlib-only, so it runs on the hypervisor host with no `pip install`.

It is specific to that lane. A KubeVirt fleet is already declared in a chart or
a set of CRs, so generate the NetBox record from the same values that generate
the VMs rather than reaching for this script; the import does not depend on it
either way.

```bash
python3 hack/seed-netbox.py --inventory "$VM_INVENTORY" --dry-run
python3 hack/seed-netbox.py --inventory "$VM_INVENTORY" --with-modules
```

It is idempotent: it looks every object up by its natural key and patches only
what drifted, so re-run it after any fleet rebuild.

What it builds, per device:

| NetBox | Value | Why the platform cares |
| --- | --- | --- |
| Name | the inventory name | becomes the Machine and BareMetalHost name |
| Serial | libvirt domain UUID | rendered into `bmc.address` by `addressTemplate` |
| Status | `active` | anything else reads as Unavailable |
| Site / Location / Rack | `$NETBOX_SITE` / `$NETBOX_LOCATION` / from the inventory | NodeType pool selectors |
| Position | read from a `-uNN-` in the name | rack elevation; nothing selects on it |
| Device type | `<prefix>-<profile>` | the size dimension |
| Role | `cpu-compute` / `gpu-compute` | the accelerator dimension |
| Tag | `$NETBOX_SYNC_TAG` | what marks it for import |
| `bmc_username` / `bmc_password` | `$BMC_USERNAME` / `$BMC_PASSWORD` | written into the host's BMC Secret |
| Interface `bmc0` | `mgmt_only`, holds the BMC IP | where the BMC is |
| Interface `eth0` | provisioning MAC | the boot MAC |
| Interface `eth1` | LAN MAC | recorded, not used for PXE |
| `oob_ip` | `$BMC_IP_START` upward | `{{ .Address }}` in the BMC address template |

`--with-modules` also records a CPU module per device, which is what fills in
`Machine.status.hardware`. It uses only module bays and module types, no
module-type profiles, so you get counts and models, not core counts. See
[netbox-data-model.md](netbox-data-model.md#4-hardware-and-what-netbox-usually-does-not-know).

**Record GPUs honestly.** No device gets a GPU unless you name it:

```bash
python3 hack/seed-netbox.py --inventory "$VM_INVENTORY" --with-modules \
  --gpu-device rack-c-u12-xlarge-gpu \
  --gpu-manufacturer NVIDIA --gpu-model 'RTX 2000 Ada'
```

Only the named device gets the GPU module and the `gpu-compute` role; everything
else is `cpu-compute`. That is deliberate. An emulated fleet has as many GPUs as
the host physically owns, usually one, and seeding a whole size class as
`gpu-compute` puts machines in a pool that cannot run the workload. The script
refuses a `--gpu-device` that is not in the inventory rather than silently
seeding a CPU-only fleet.

Two details worth knowing before you re-run it:

**Rack positions come from the machine name.** A name like `<rack>-u<NN>-<profile>`
states its own rack unit and the seed uses it; `--rack-u-start` is only the
fallback for names with no `-uNN-`.

**Interface naming is load-bearing.** The import picks the boot MAC as the first
non-`mgmt_only` interface **in name order**, skipping anything that looks like a
fabric port. `bmc0` is management, `eth0` sorts before `eth1`, so the
provisioning NIC wins. Rename them at your peril.

Spot-check one device in the UI. It should show a rack elevation position, an
out-of-band IP, a serial that matches the real machine, and the tag.

---

## 6. Decide what happens to existing BareMetalHosts

**Read this before touching the NodeProvider.**

The import refuses to write to a BareMetalHost it did not create. If a host
already exists under the name a NetBox device wants, the sync marks the new
Machine `Registered=False` with reason `HostConflict`, raises a
`NetBoxHostConflict` warning event, and leaves both objects alone. It does not
adopt and it does not overwrite, which is the behaviour you want, but it does
mean two lanes cannot both own the same host name.

Check what you have:

```bash
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" get bmh \
  -L machines.vcluster.com/source \
  -o custom-columns='NAME:.metadata.name,STATE:.status.provisioning.state,CONSUMER:.spec.consumerRef.name'
```

Pick one:

**A. Clean cut (recommended).** Delete the hand-rolled hosts while Ironic is
still running (their finalizers hang forever otherwise), then let the import
recreate them under the same names. Nothing may be provisioned or claimed;
check `CONSUMER` above first.

```bash
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" \
  delete bmh -l <your-existing-label> --wait=true --timeout=10m
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" get secrets -o name \
  | awk '/-bmc-creds$/' | xargs -r -n1 kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" delete
```

Substitute whatever label or name prefix your existing hosts carry. In the
reference lab it is `demo=vmetal` and the Secret suffix is `-bmc-creds`.

**B. Side-by-side.** Leave the existing hosts alone and give the NetBox devices
different names (`nb-<name>`) so both lanes coexist. The NodeType selectors in
the fragment already carry `machines.vcluster.com/source: netbox`, so the pools
only draw from imported hosts. Costs you double the machines, so it is only
worth it if you need the old lane live during a demo.

---

## 7. Wire the NodeProvider

`manifests/platform/metal3-node-provider-netbox.fragment.yaml` holds the two
blocks to merge into your existing provider.
`manifests/platform/metal3-node-provider-netbox.example.yaml` is a complete
worked provider if you would rather diff against a whole object.

Create the networkData template the worked provider points at if you are using
the reference lab's two-NIC shape:

```bash
kubectl --context "$PLATFORM_CONTEXT" \
  apply -f manifests/platform/vmetal-lan-only-postboot-machineconfigtemplate.yaml
```

That `MachineConfigTemplate` is intentionally **not** a NetBox import object.
It lives on the Platform cluster and is selected by the provider property
`vcluster.com/network-data-template-config: vmetal-lan-only-postboot`. It
renders networkData at provision time from the inspected BareMetalHost NIC list,
choosing the first NIC whose `pxe` field is not true for the tenant/LAN link.

Skip this template if your existing networkData already targets the NIC the
installed node should use. A single-NIC lab often does not need it at all: the
default bare-metal networkData uses `spec.bootMACAddress`, which is fine when
the same NIC and network carry PXE/inspection, the installed node address, and
the tenant cluster join path. In that case, do not copy
`vcluster.com/network-data-template-config: vmetal-lan-only-postboot` from the
worked example.

1. **`spec.metal3.netBox`**: the connector reference, the tag, the address
   template, the custom field names, and the BareMetalHost template.
2. **`spec.metal3.nodeTypes[*].bareMetalHosts.selector`**: rewritten to select
   on `netbox.vcluster.com/*` labels instead of whatever labels your current
   lane writes.

**Leave `spec.metal3.deploy` exactly as it is.** The metal3 chart runs Helm
*with* schema validation, so a bad value there fails closed and takes every
other change in the same apply down with it.

**Apply it the way you normally apply it.** If the provider is under GitOps,
edit it in the repo and push. In the reference lab Argo CD runs `selfHeal: true`,
which reverts a `kubectl patch` about a minute after you convince yourself it
worked.

### The address template

```yaml
addressTemplate: "redfish+http://{{ .Address }}:8000/redfish/v1/Systems/{{ .Serial }}"
```

That is the **sushy-tools** form. sushy keys systems by libvirt domain UUID,
and the UUID is in the device serial, so the default
(`redfish://{{ .Address }}/redfish/v1/Systems/1`) would dial the same
non-existent system for every host.

It is not the form for every emulator. kubevirtbmc always serves
`/redfish/v1/Systems/1`, terminates TLS, and is reached by Service name rather
than by IP, so its template is a different shape again:
[kubevirtbmc.md](kubevirtbmc.md).

For real BMCs, the default is often right, because many vendors do serve
`/redfish/v1/Systems/1`. Check one by hand before you assume:

```bash
curl -sk -u "$BMC_USERNAME:$BMC_PASSWORD" https://<a-real-bmc>/redfish/v1/Systems | head -c 400
```

The fields available in the template are `.Address`, `.IP`, `.Device`,
`.Manufacturer`, `.DeviceType`, `.Serial`. A template referencing anything else
fails at render with `missingkey=error`, and templates are parsed when the
provider is written, so a syntax error is rejected where you typed it.

After it lands:

```bash
kubectl --context "$PLATFORM_CONTEXT" get nodeprovider "$NODE_PROVIDER" \
  -o jsonpath='{.spec.metal3.netBox}' | python3 -m json.tool
```

### The provisioning IP annotator

Apply this to the Metal3 cluster while using the NetBox import:

```bash
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" \
  apply -f manifests/metal3/provisioning-ip-annotator-cronjob.yaml
```

The vMetal DHCP proxy needs `metal3.vcluster.com/ip-address` before the first
inspection boot. The platform already writes that annotation, but only when a
Machine claims the host, which is too late for newly imported BareMetalHosts.
The cronjob gives unclaimed hosts a short-lived address from the reference lab's
reserved pool; when a host is claimed, the platform overwrites it with the normal
claim-time IPAM address.

---

## 8. Watch the import land

The sync runs on a **one-minute** ticker inside the Platform, so nothing is
instant and nothing needs poking.

```bash
bash hack/verify-netbox-import.sh
```

Or by hand, on the Platform cluster:

```bash
kubectl --context "$PLATFORM_CONTEXT" get machines.management.loft.sh \
  -l machines.vcluster.com/source=netbox \
  -L netbox.vcluster.com/rack,netbox.vcluster.com/device-type,netbox.vcluster.com/provisionable
```

and on the Metal3 cluster:

```bash
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" get bmh \
  -l machines.vcluster.com/source=netbox \
  -o custom-columns='NAME:.metadata.name,STATE:.status.provisioning.state,BMC:.spec.bmc.address,MAC:.spec.bootMACAddress'
```

Expected order of events:

The MachineConfigTemplate is a prerequisite for claim-time networkData, not an
import event. The NetBox sync does not create or update it.

1. A `Machine` appears per tagged device, `Pending` / `NotRegistered`, with the
   hardware NetBox knows about already filled in. This is always first, and it
   happens whether or not the record is complete.
2. If the device is complete, a `<name>-bmc` Secret and then a `BareMetalHost`
   appear on the Metal3 cluster, in that order, and the Machine goes
   `Registered=True`. No Secret is written if the provider's
   `bareMetalHostTemplate` pins `bmc.credentialsName`.
3. The provisioning IP annotator patches imported BareMetalHosts that do not yet
   carry `metal3.vcluster.com/ip-address`.
4. Metal3 registers and inspects the host: `registering -> inspecting ->
   available`, powered off.
5. A tenant cluster using one of the pools claims a host as before.

If a Machine stays `Pending` with reason `MissingRequirements`, NetBox is missing
something the provisioner needs; the condition message names it. That is the
normal, useful case. See [troubleshooting.md](troubleshooting.md).

---

## 9. The demo beat

The thing worth showing is not the install. It is the loop:

1. In NetBox, open a device, **remove the sync tag**, save.
2. Within a minute, the Machine and its BareMetalHost are gone from the
   platform, provided nothing was using them.
3. Add the tag back. They come back.
4. Now do it to a machine that is running a tenant node. The Machine stays, and
   `SourceAttached` flips to `False` with reason `Orphaned`: *"NetBox no longer
   tags this device with '<tag>'; the machine is kept because it is in use."* An
   edit in a records database does not get to deprovision hardware.

Step 4 is the one that lands with an infrastructure audience, so do not rush
past it to get to the happy path.

Second beat, for the operations story: change a BMC password in NetBox and watch
the `<host>-bmc` Secret follow it on the next sync.

Third beat: move a device to a different rack in NetBox. The
`netbox.vcluster.com/rack` label on both the Machine and the BareMetalHost
follows, so the host moves between NodeType pools without anyone editing a
selector.

---

## 10. Rolling back

The import is additive and reversible.

```bash
# 1. remove spec.metal3.netBox from the NodeProvider and restore your previous
#    selectors, however you apply it

# 2. the sync no longer owns anything, but what it created stays. Remove it:
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" \
  delete bmh -l machines.vcluster.com/source=netbox
kubectl --context "$PLATFORM_CONTEXT" delete machines.management.loft.sh \
  -l machines.vcluster.com/source=netbox

# 3. back to whatever produced your BareMetalHosts before
```

NetBox itself can stay installed; with no provider referencing the connector,
nothing reads it.
