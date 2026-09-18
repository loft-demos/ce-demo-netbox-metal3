# NetBox and the other NodeProvider types

Short answer, as of vCluster Platform **4.13.0-alpha.12**: **the NetBox import
is Metal3-only.** It is a field on the Metal3 provider (`spec.metal3.netBox`),
not a platform-wide inventory source, and `NodeProviderNICo` has no equivalent.

Worth knowing before you promise a customer "NetBox as the inventory", because
it changes what NetBox is *for* on anything that is not Metal3.

---

## Why it is Metal3-only

The import's whole output is a **BareMetalHost** plus its BMC credentials
Secret, in the provider's `clusterRef` cluster. Everything it reads from NetBox
exists to fill that object in: the out-of-band IP and the BMC login become
`spec.bmc`, the boot MAC becomes `spec.bootMACAddress`, the serial becomes the
Redfish system id through the address template. There is no BareMetalHost in the
NICo model to write.

NICo is its own source of truth. A `NodeProviderNICo` points at the NVIDIA Infra
Controller REST API with an endpoint, an org, a site UUID and an optional
`instanceTypeIds` allow-list, and the platform surfaces NICo InstanceTypes as
NodeTypes. The inventory question NetBox answers for Metal3, *which physical
boxes exist and how do I reach their BMCs*, NICo already answers for itself.

The platform models this generically enough that a second source could be added
later: `machines.vcluster.com/source` exists precisely to mark a machine as
"recorded from an inventory before the provider knew about it", and `netbox` is
currently its only value. Nothing in the Machine or NodeClaim machinery is
Metal3-specific. But the wiring is not there today, and the `netBox` block is
silently ignored if you put it on a NICo provider: the sync only looks at
`spec.metal3.netBox`.

---

## What NetBox can still usefully do alongside a NICo provider

Three things, none of which need platform support:

1. **Be the physical record.** Rack, position, serials, NICs, BMC addresses,
   cabling. That is what a DCIM is for, and it costs nothing to keep NICo-managed
   machines in the same NetBox as a Metal3 fleet: one site, one location,
   different racks or roles.

2. **Drive the NICo site model.** NICo wants a site UUID and either an existing
   site-level IPBlock or a CIDR to create one (`siteIPBlockID` /
   `siteIPBlockCIDR`). NetBox IPAM is a reasonable place to own those prefixes
   and hand the authoritative CIDR to the provider manifest, the same way a
   Metal3 provider's `metal3.vcluster.com/network-cidr` gets written down once
   in GitOps.

3. **Be the reconciliation target.** Once NICo reports machines, compare its view
   against NetBox's. A box NICo knows about that NetBox does not is either
   undocumented hardware or a stale NICo record. That is exactly the drift a DCIM
   is supposed to surface.

---

## Mixing provider types on one cluster

If a Metal3 provider and a NICo provider share a cluster, keep one provider type
per node and no overlap. Devices belonging to the non-Metal3 hosts go into
NetBox **untagged**: they sit in the same site and location as the Metal3 fleet,
a different rack or device role tells them apart, and the platform simply never
selects them, because the import only ever lists devices carrying the sync tag.

Two practical notes from doing this in the reference lab:

- **Check what your node labels actually mean.** A label naming a node after a
  product is not evidence of which provider owns it. In the reference lab,
  `demo.vcluster.com/bmh-host=true` marks the Metal3 host and
  `demo.vcluster.com/vmetal-host=true` marks the node that is *not* the Metal3
  host. See [reference-lab.md](reference-lab.md).
- **Ironic's placement is not negotiable.** Ironic and the DHCP proxy have to run
  where they can reach the BMC network and attach the host-local provisioning
  network attachment. Nothing about a NICo provider wants a share of that, which
  is the main reason the split stays clean.

---

## Worth raising with engineering

If a mixed lab is going to be a customer-facing story, "NetBox as the common
inventory across Metal3 and NICo providers" is a reasonable ask, and the platform
is already shaped for it: `MachineSourceLabel`, the `SourceAttached` /
`Registered` conditions, and the orphan-protection rules are all provider
agnostic. What is missing is a projection from a NetBox device to whatever a NICo
provider would need to register a machine, which only makes sense if NICo has a
registration API at all.
