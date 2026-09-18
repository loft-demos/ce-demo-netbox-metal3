# Troubleshooting the NetBox import

Every symptom below is something the platform states explicitly, as a condition
on the Machine, an event on the NodeProvider, or both. Start there rather than
in the logs.

Commands assume a sourced `env.sh` (see [../env.example](../env.example)).

```bash
# the events are where every setup problem surfaces
kubectl --context "$PLATFORM_CONTEXT" get events \
  --field-selector involvedObject.name="$NODE_PROVIDER" \
  --sort-by=.lastTimestamp | tail -20

# the conditions are where every per-device problem surfaces
kubectl --context "$PLATFORM_CONTEXT" get machines.management.loft.sh \
  -l machines.vcluster.com/source=netbox -o yaml | grep -A6 conditions:
```

---

## Nothing at all happens

**No Machines, no events.** Work down the list:

1. **Is the provider configured?**
   ```bash
   kubectl --context "$PLATFORM_CONTEXT" get nodeprovider "$NODE_PROVIDER" \
     -o jsonpath='{.spec.metal3.netBox}'
   ```
   Empty means your change never landed, or you edited the wrong object. The
   sync skips any provider without `spec.metal3.netBox.secretRef` entirely and
   silently, because that is the normal case for every other provider.

   Under GitOps, empty usually means the reconciler has not synced yet, or
   reverted you. A `kubectl patch` against a provider with `selfHeal: true` will
   look like it worked for about a minute.

2. **Wait a minute.** The resync interval is 60 seconds and there is no trigger.
   NetBox is a records database, not a live inventory.

3. **Is the Metal3 feature on?** The NetBox UI and the whole import are gated
   behind the Metal3 feature flag. If **Connectors -> NetBox** is missing from
   the sidebar, the licence does not have it.

4. **Is the connector Secret labelled?**
   ```bash
   kubectl --context "$PLATFORM_CONTEXT" -n "$PLATFORM_NAMESPACE" \
     get secret netbox --show-labels
   ```
   `loft.sh/connector-type=netbox` is mandatory. Without it the platform refuses
   the Secret by name, not by content, and the UI will not list it either, which
   is the fastest way to notice.

5. **Is the Secret where the provider says it is?** `secretRef` carries both a
   namespace and a name, and it is resolved against the **Platform** cluster, not
   the Metal3 one.

---

## `NetBoxConnectorInvalid`

The Secret is missing `url` or `apiToken`, or the URL is not `http`/`https`.
`url` must be the base URL with no trailing `/api`.

---

## 403 on every request

Two distinct messages, two different mistakes. Neither is about the token's
version: NetBox accepts both `Token` and `Bearer` as the scheme and infers the
version from whether the value starts with `nbt_`, so the platform's hardcoded
`Token` scheme works with a v1 and a v2 token alike.

**`Invalid authorization header: Must be in the form ...`** means the scheme word
is inside `apiToken`. NetBox displays a new token as the complete header value,
`Bearer nbt_<key>.<secret>` or `Token <40 chars>`, and only the part after the
space belongs in the Secret.

**`Invalid v1 token`** means a v2 value arrived without its `nbt_<key>.` prefix,
so NetBox read it as a v1 token and found no match. That is what you get from
`/api/users/tokens/provision/`, which returns the bare plaintext and the key as
separate fields; it is why the platform never provisions a token itself.
Recreate the token in the UI and copy the whole displayed value minus the scheme
word.

**`Invalid v2 token`** on a token that used to work means `API_TOKEN_PEPPERS`
changed. v2 tokens are HMAC digests keyed by a pepper held in NetBox's
configuration, not in its database, so a release rebuilt without the old
peppers, or a database moved under a different NetBox deployment, invalidates
every v2 token. Reissue it, or use a v1 token, whose plaintext lives in the
database and survives both.

Verify before you touch anything else:

```bash
curl -fsS -H "Authorization: Token $NETBOX_PLATFORM_TOKEN" \
  "$NETBOX_URL/api/dcim/devices/?limit=1"
```

The chart's `superuser.apiToken` value does not help: on NetBox 4.7 the
container needs both `superuser_api_token` and `superuser_api_key` mounted and
the chart mounts only the first, so the value is accepted and ignored.

---

## `NetBoxTagMissing`

> NetBox has no tag with slug "vcluster-sync"; create it in NetBox
> (Customization > Tags) and apply it to the devices to import

Exactly what it says. This is its own error because "no device is tagged" and
"nobody created the tag yet" need different fixes, and only the second is a
one-time setup step. The slug is matched case-insensitively against both slug
and name, so the tag typed as a name works too.

---

## Machine is `Pending` with reason `MissingRequirements`

The normal, useful case. The condition message names what NetBox does not have:

```bash
kubectl --context "$PLATFORM_CONTEXT" get machines.management.loft.sh <name> \
  -o jsonpath='{range .status.conditions[*]}{.type}: {.reason} {.message}{"\n"}{end}'
```

No BareMetalHost is created at all until the list is empty: the platform will
not hand Ironic a host it knows will fail to register. Fix NetBox and the host
appears on the next sync; nothing needs restarting.

| In the message | Fix in NetBox |
| --- | --- |
| `BMCAddress` | set the device's **out-of-band IP**, or assign an IP to an interface with `mgmt_only` |
| `BMCInterface` | same: either `oob_ip` or a `mgmt_only` interface satisfies it |
| `BootMACAddress` | add a MAC object on a non-management interface, not just the interface |
| `Serial` | the device serial is blank; on an emulated fleet it must be the libvirt domain UUID |
| `BMCCredentials` | one or both of the BMC login custom fields is empty |

`BootMACAddress` with MACs clearly present usually means every non-management
interface looks like fabric: anything typed `infiniband*` or named `ib*`. Or the
MAC is set on the legacy inline interface field rather than as a MAC object;
NetBox 4.2+ keeps MACs in their own objects and the inline field is commonly
null.

Two of these five can be made to go away instead of satisfied, by pinning
`bmc.credentialsName` or `bootMACAddress` in the provider's
`bareMetalHostTemplate`. See
[netbox-data-model.md](netbox-data-model.md#5-baremetalhost-and-what-the-template-controls).

---

## Machine is `Registered=False` with reason `HostConflict`

> BareMetalHost metal3-system/<name> already exists and was not created by this
> NetBox import; remove it or untag the device

A host of that name exists without the `machines.vcluster.com/netbox-device`
annotation, almost always one that your previous lane created. The import will
not adopt it: stamping NetBox metadata on somebody else's host would also hand a
later untag the right to delete hardware nobody imported.

Either delete the old host (while Ironic is still running, or the finalizers
hang) or rename the NetBox device. See [runbook step 6](runbook.md#6-decide-what-happens-to-existing-baremetalhosts).

---

## Machine is `Registered=False` with reason `RegistrationFailed`

The BareMetalHost create was rejected, or the address template failed to render.
The error is verbatim in the message.

A template that references a field that does not exist fails at render with
`missingkey=error`; the valid fields are `.Address`, `.IP`, `.Device`,
`.Manufacturer`, `.DeviceType`, `.Serial`. Templates are also parsed when the
provider is written, so a syntax error is rejected where you typed it.

---

## `SourceAttached=False` with reason `Orphaned`

> NetBox no longer tags this device with "<tag>"; the machine is kept because it
> is in use

Working as intended, and it is the beat worth demoing. The device lost the tag
while the machine was held by a `NodeClaim` or in a phase other than
`Pending`/`Available`. Untagging a record does not get to deprovision hardware.

Re-tag the device and it re-attaches on the next sync. To actually release it,
free the machine first (delete the tenant node or tenant cluster that claims
it), and the next sync removes it.

---

## A NodeType pool matches nothing, and no condition says why

Nothing is wrong with the import and nothing will report an error. The likely
cause is a selector key that was never written, because NetBox's value for it
is not a valid Kubernetes label value. Those are dropped silently rather than
mangled, so the pool selects on a label the host does not have.

```bash
# what actually landed on the hosts
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" get bmh \
  -l machines.vcluster.com/source=netbox \
  -L netbox.vcluster.com/site,netbox.vcluster.com/location,netbox.vcluster.com/rack,netbox.vcluster.com/role,netbox.vcluster.com/device-type

# what the pool asks for
kubectl --context "$PLATFORM_CONTEXT" get nodeprovider "$NODE_PROVIDER" \
  -o jsonpath='{range .spec.metal3.nodeTypes[*]}{.name}{"\t"}{.bareMetalHosts.selector.matchLabels}{"\n"}{end}'
```

An empty column is a dropped label. `rack` is the usual one: it carries the
NetBox rack's **name**, not its slug, and a name like `Rack A` has a space in
it. Site, location, role and device type all come from slugs, which NetBox
already constrains, so they rarely fail this way.

Fix it in NetBox by renaming the rack to something label-safe, or select on a
different dimension. Renaming the rack changes the label on the next sync and
the host moves between pools on its own.

---

## A Secret named `<host>-bmc` disappeared

If it was one the import wrote, this is ordinary cleanup: the device was
untagged and the machine was free, so the host and its Secret went with it.

If it was one **you** wrote, it is the one gap in the import's ownership rules.
The BareMetalHost is protected by a correlation annotation and is never touched
if the import did not create it. The Secret is not: once the host is gone, or
if no host of that name ever existed, the release deletes the computed name
`<host>-bmc` without checking who made it.

That only bites where a name collides, so it is worth a moment before you run
two lanes side by side (runbook step 6, option B):

```bash
# does anything you own collide with a name a tagged device would claim?
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" get secrets -o name | grep -- '-bmc$'
```

Rename either side, or give the NetBox devices a distinct name prefix, which
runbook step 6 suggests anyway.

---

## Host created, but Ironic cannot reach the BMC

Check what the import actually wrote:

```bash
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" get bmh <name> \
  -o jsonpath='{.spec.bmc.address}{"\n"}'
```

- **`/Systems/1` at the end when you did not want it**: the `addressTemplate` is
  not set, so the platform default is in use. Fix the provider.
- **Right shape, no answer**: on an emulated fleet, the alias is missing from
  the provisioning bridge: `bash hack/bmc-ip-aliases.sh status` on that host. On
  real hardware, the BMC is on a network Ironic cannot route to.
- **Right shape, wrong system id**: the device serial does not match the machine
  any more. An emulated fleet regenerates UUIDs on every rebuild; re-run
  `hack/seed-netbox.py` to resync. Note that `bmc.address` is only mutable while
  the host is registering or detached, so a host already registered with a stale
  address has to be deleted and re-imported.
- **Nothing reachable from Ironic at all**: that is a pre-existing placement or
  routing problem, not NetBox. Ironic and the DHCP proxy have to sit where they
  can reach the BMC network; in an emulated lab that means pinned to the host
  running sushy-tools, because from any other node the provisioning subnet
  routes to the LAN and dies on a 60s connect timeout.

---

## Hosts reach `available` but NodeClaims never bind

Not a NetBox problem, and the import makes it less likely rather than more:
vMetal requires a BareMetalHost to be **powered off** before it can be
provisioned, and the import creates hosts with `spec.online: false`. If you
copied `online: true` into the `bareMetalHostTemplate`, take it out.

---

## Machines provision, then sit at `NotJoined` forever

The host installs fine and never appears in the tenant cluster. Check whether
your MachineConfigTemplate renders `networkData` from an annotation the import
does not set:

```bash
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" get bmh <name> \
  -o jsonpath='{.metadata.annotations}{"\n"}' | python3 -m json.tool
```

A template like this renders empty on an imported host:

```yaml
"ethernet_mac_address": "{{ index .Values.BareMetalHost "metadata" "annotations" "lan.vcluster.com/mac" }}"
```

The only place to set annotations on imported hosts is
`spec.metal3.netBox.bareMetalHostTemplate.metadata`, which applies **one** value
to every host, and `addressTemplate` is the only templated field. So a generator
script that wrote a per-host annotation has no direct equivalent here.

Rendered empty, cloud-init cannot match the link to a NIC, the interface never
comes up, and the machine provisions successfully and then has no path to the
tenant cluster. `manifests/metal3/lan-mac-annotator-cronjob.yaml` works around it
by reading each host's own `status.hardware.nics` after inspection and stamping
the non-PXE MAC back on. Observed 2026-09-17.

The annotation is read when `networkData` is **rendered**, at provision time, so
a host that is already provisioned will not pick up a late annotation. Release
its NodeClaim so it re-provisions.

---

## NetBox answers 503, or the sync is slow

The client already paces itself: 250-row pages, at most 3 requests in flight
across all collections, retries on 429/502/503/504 honouring `Retry-After`, and
a 5-minute bound on one provider's fetch. It also refuses to read every
interface of every device, because a site's interface table is the one
collection NetBox cannot serve in bulk.

If it still struggles, the fleet is probably not the problem. Check the Postgres
pod and the node it landed on. If NetBox is pinned with a nodeSelector and its
PVs are node-affine `local-path` volumes, a `Pending` `netbox-postgresql-0`
means the two disagree.

---

## Re-running the seeding script after a fleet rebuild

Rebuilding an emulated fleet generates fresh libvirt UUIDs and MACs. Re-running
`hack/seed-netbox.py` patches serials and MACs in place, but a BareMetalHost that
is already registered will not accept a new `bootMACAddress`, and only accepts a
new `bmc.address` while registering or detached.

So after a fleet rebuild, do it in this order:

```bash
# 1. on the Metal3 cluster, while Ironic is up
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" \
  delete bmh -l machines.vcluster.com/source=netbox --wait=true --timeout=10m
# 2. on the hypervisor host: destroy and recreate the domains, restart sushy-tools
# 3. re-seed
python3 hack/seed-netbox.py --inventory "$VM_INVENTORY" --with-modules
# 4. wait one tick; the import rebuilds Machines and hosts from the new record
```
