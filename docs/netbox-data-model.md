# What the platform reads out of NetBox

**Source:** `loft-enterprise/pkg/netbox` and `pkg/controllers/netbox`, as of
vCluster Platform 4.13.0-alpha.12. Read this when a device "looks fine in
NetBox" but the platform disagrees.

---

## 1. The shape of the sync

A `Syncer` inside the Platform ticks every **60 seconds**. For every
NodeProvider with `spec.metal3.netBox.secretRef` set, it:

1. Builds a client from the connector Secret (`url`, `apiToken`, `insecure`).
2. Resolves the tag slug. A tag that does not exist is its own error
   (`NetBoxTagMissing`), because "nobody created the tag" and "no device is
   tagged" need different fixes.
3. Lists every device carrying that tag, and **only** the tag. It is deliberately
   not narrowed by role: the tag is an operator saying *this one*, which
   outranks the platform guessing which roles hold servers.
4. Fetches the sub-collections it needs and joins them by id.
5. Projects each device into a Machine, then converges the Machine and its
   BareMetalHost.
6. Releases machines whose device is no longer selected, unless they are in
   use.

It never writes to NetBox.

### Endpoints it reads

| Endpoint | Scope | Why |
| --- | --- | --- |
| `/api/extras/tags/` | `?q=<slug>` | resolve the sync tag |
| `/api/dcim/devices/` | `?tag=<slug>` | the fleet |
| `/api/dcim/interfaces/` | `?mgmt_only=true&brief=1` | find the BMC port |
| `/api/ipam/ip-addresses/` | per device | the BMC address |
| `/api/dcim/mac-addresses/` | per device | the boot MAC |
| `/api/dcim/modules/` | per device | installed hardware |
| `/api/dcim/module-types/` | all | part numbers and profile attributes |

Below 50 devices it pushes `device_id=` filters down; above that it reads the
collection whole and filters locally, because a URL with hundreds of ids costs
more than the extra rows.

It deliberately does **not** read every interface of every device in the list
path. A site's interface table is the one collection NetBox cannot serve in
bulk, and asking for it concurrently is what makes NetBox answer 503. That is
why a list only knows the management interfaces and the MAC objects.

Pages are 250 rows, at most 3 requests in flight across all collections, with
retries on 429/502/503/504 honouring `Retry-After`.

---

## 2. Device → Machine, field by field

| NetBox | Platform | Notes |
| --- | --- | --- |
| `name` | Machine + BareMetalHost name | must be a valid DNS-1123 subdomain, else falls back to `device-<id>` |
| `id` | `machines.vcluster.com/netbox-device` annotation, as `<connector>.<id>` | the real identity: NetBox renames devices in place, so the name is not something an import can follow |
| `serial` | `status.hardware.serial`, and `{{ .Serial }}` in the address template | **required**; a blank serial holds the host back |
| `status` | `netbox.vcluster.com/status` | only `active` reads as available |
| `tenant` | availability `Assigned` | leave empty for a free machine |
| `site.slug` | `netbox.vcluster.com/site` | |
| `location.slug` | `netbox.vcluster.com/location` | |
| `rack.name` | `netbox.vcluster.com/rack` | the **name**, not the slug, so a rack NetBox calls `Rack A` yields no label at all |
| `role.slug` | `netbox.vcluster.com/role` | |
| `device_type.slug` | `netbox.vcluster.com/device-type` | |
| `device_type.manufacturer.slug` | `netbox.vcluster.com/manufacturer` | |
| `oob_ip` **or** an IP on a `mgmt_only` interface | `{{ .Address }}` | `oob_ip` wins |
| `display_url` | `netbox.vcluster.com/url` annotation | links the Machine back to the record |
| custom fields | passed through as text, except the BMC password | the platform assigns no meaning to them |

Every label is rewritten on every pass. A value NetBox allows but Kubernetes
does not is dropped rather than mangled, so a selector never matches something
it should not.

The dropping is silent, which makes it worth a sanity check the first time you
seed: a NodeType pool selecting on a label that was never written matches
nothing, and looks exactly like a pool whose machines are all claimed. `rack`
is the usual culprit, because it carries the rack's display name rather than
its slug and NetBox rack names commonly contain spaces. Compare what you
expect against what actually landed:

```bash
kubectl get machines.management.loft.sh -l machines.vcluster.com/source=netbox \
  -L netbox.vcluster.com/site,netbox.vcluster.com/location,netbox.vcluster.com/rack,netbox.vcluster.com/role,netbox.vcluster.com/device-type
```

An empty column is a dropped label, not a missing NetBox field.

---

## 3. The five things a device must have

`netbox.MachineStatus.Missing` is the gate between "NetBox knows about this
machine" and "the platform could actually boot it". A device missing any of
these gets **no BareMetalHost at all**, not a broken one, and the Machine
sits `Pending` / `MissingRequirements` with the list in its message. The host
appears on the first sync after NetBox is fixed.

| Requirement | Satisfied by |
| --- | --- |
| `BMCInterface` | `oob_ip` set on the device, **or** any interface with `mgmt_only: true` |
| `BMCAddress` | an actual IP: `oob_ip`, or an IP assigned to a `mgmt_only` interface |
| `BootMACAddress` | a MAC on a non-management, non-fabric interface, **or** a `bootMACAddress` pinned in the provider's host template |
| `Serial` | a non-empty device serial |
| `BMCCredentials` | both `bmc_username` and `bmc_password` custom fields, **or** a `bmc.credentialsName` pinned in the provider's host template |

### How the boot MAC is chosen

NetBox does not record which interface boots, so this is a preference, not a
fact:

1. never a `mgmt_only` interface;
2. an Ethernet port ahead of a fabric port, where anything typed `infiniband*` or
   named `ib*` (but not `ibm*`) is treated as fabric, because a GPU node's
   InfiniBand links are not a provisioning path;
3. otherwise the first one in **name order**.

So `bmc0` / `eth0` / `eth1` gives you `eth0`, which is what the seeding script
relies on.

MACs live in their own objects since NetBox 4.2, and the fields on the
interface are commonly null even when the interface has one. The MAC objects
are the source; `primary_mac_address` and the legacy inline `mac_address` are
only fallbacks for older instances.

---

## 4. Hardware, and what NetBox usually does not know

Modules are how a NetBox deployment records what is physically inside a
chassis. The projection groups them into `Machine.status.hardware`:

- The module's **bay name** decides the kind when no module-type profile is
  set: a bay starting `cpu` is a CPU, `gpu` is a GPU. Model-name matching
  catches NVMe/SSD/BOSS as storage and NIC/ConnectX/BlueField as network.
- A module type **profile** overrides that guess: profiles named `cpu`, `gpu`,
  `memory`, `hard disk` map straight through.
- A model containing `baseboard` counts as nothing: a GPU baseboard sits in a
  bay named like the GPUs it carries, and counting it would report nine GPUs on
  an eight-GPU node.

Counts and models come out reliably. **Quantities do not.** Core counts and
capacities live in module-type profile attributes (`cores`, `architecture`,
`memory`/`size` in GB), which a deployment has to fill in, and memory is
commonly not modelled as modules at all. Anything NetBox does not state is left
unset rather than inferred from a model name, so a caller can tell "no memory
recorded" from "no memory".

`hack/seed-netbox.py --with-modules` creates bays and module types but no
profiles, so you get `capabilities` with counts and models and no core counts.
Adding profiles in NetBox later fills the rest in with no platform change.

---

## 5. BareMetalHost, and what the template controls

The generated spec is deliberately minimal:

```yaml
spec:
  online: false
  bmc:
    address: <rendered from addressTemplate>
    credentialsName: <host>-bmc
  bootMACAddress: <from NetBox>
  description: "Imported from NetBox: <device URL>"
```

`bareMetalHostTemplate.spec` is merged over it field by field, nested objects
merged, everything else replaced, so the template wins wherever it overlaps.
Its labels and annotations are kept current on existing hosts; its **spec is
applied at create time only**.

Two template values change the rules rather than just the output:

- `bmc.credentialsName` makes the BMC login custom fields optional and stops
  the import writing per-host Secrets.
- `bootMACAddress` makes the NetBox boot MAC optional.

The template may not set the keys the import owns
(`machines.vcluster.com/source`, `machines.vcluster.com/netbox-device`, or
anything under `netbox.vcluster.com/`); the provider is rejected at write time
if it tries.

### What can and cannot be corrected later

Metal3's webhook refuses a change to `spec.bmc.address` once it is set, unless
the host is still registering or has been detached, and refuses **any** change
to `bootMACAddress` once set. So:

- A corrected management IP in NetBox reaches Ironic only in that window.
- A corrected boot MAC never does. Fix it by deleting the host (untag, let the
  release run, retag) rather than by editing NetBox and waiting.

Labels and annotations always follow NetBox, which is why moving a device
between racks moves the host between NodeType pools.

---

## 6. Ownership, and why untagging is safe

Everything the import creates carries
`machines.vcluster.com/netbox-device: <connector>.<device id>`.

- A BareMetalHost **without** that annotation is somebody else's, even if it
  carries the exact name the device would be given. The sync reports
  `HostConflict` and touches nothing.
- When a device stops being selected, the sync deletes the Machine, the host,
  and the BMC Secret, but **only** if the machine is free: no `NodeClaim`
  annotation, and a phase of `Pending` or `Available`. `Unknown` is not free; a
  provider that cannot be reached may well be running a workload.
- A host with an image or a `consumerRef` is never deleted.
- Otherwise the Machine is kept and `SourceAttached` goes `False` with reason
  `Orphaned`. Re-tagging the device re-attaches it; a device that was untagged
  and tagged again is imported again, not left Orphaned forever.
- The Secret is deleted only after the host is actually gone, because Metal3
  holds a finalizer on it while the host still references it.
- **The Secret deletion is the one step with no ownership check.** The host has
  one: a BareMetalHost without the correlation annotation is left alone
  whatever its name, and the release stops there without touching a Secret. But
  once the host is gone, or was never created, the release deletes the computed
  name `<host>-bmc` unconditionally. So a Secret you created yourself that
  happens to be called `<device-name>-bmc` is deleted when that device is
  untagged, including when the device was never provisionable and so never had
  a host at all. It is a narrow case, and it only bites where a name collides,
  but it is worth knowing if you are running two lanes side by side and your
  other lane also suffixes `-bmc`.

---

## 7. NetBox version notes

Field names follow NetBox 4.6+; unknown fields are ignored on decode, so a
newer NetBox is fine.

**API tokens.** NetBox 4.5 introduced v2 tokens and has defaulted new ones to
v2 since. Both versions work with this integration: NetBox accepts `Token` and
`Bearer` interchangeably as the scheme keyword and infers the version from the
value, treating anything starting `nbt_` as v2. So the platform's hardcoded
`Authorization: Token` carries either, as long as `apiToken` holds the value
alone and not the scheme word NetBox prints in front of it.

`/api/users/tokens/provision/` is the one path that does not work unaided: it
returns the plaintext and the key as separate fields, so the value sent back
lacks the `nbt_<key>.` prefix and NetBox reads it as a v1 token that does not
exist. That is why the platform never provisions one itself.

Prefer a **v1** token for a long-lived connector. A v2 token is an HMAC digest
keyed by `API_TOKEN_PEPPERS`, which is configuration rather than database
state, so it stops validating if the peppers are regenerated or the database is
moved under a different NetBox deployment. A v1 token keeps its plaintext in
the database and survives both.

**Nested objects are not the full serializer.** An interface nested inside an
IP address carries the interface id, name and device but not `mgmt_only`; a
module type nested inside a module carries the model but not `part_number` or
`attributes`. That is why each collection is fetched separately and joined by
id rather than walked through nested objects, and why a fact you can see in
the NetBox UI may still be invisible to a bulk list.
