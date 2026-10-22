# Running the import against kubevirtbmc instead of sushy-tools

Short answer: **yes, and two of the awkward parts go away.** One new problem
takes their place, and it needs a decision before you seed anything. Two
smaller ones come with the usual way kubevirtbmc is deployed: a TLS front end
that only answers by name, and a credentials Secret named the way the import
names its own. All three were hit, and fixed, on a real kubevirtbmc run
([what was verified](#what-is-verified-here-and-what-is-not)).

This page is the delta. Everything in [runbook.md](runbook.md),
[netbox-data-model.md](netbox-data-model.md) and
[troubleshooting.md](troubleshooting.md) still applies: the platform side does
not know or care what is answering Redfish.

---

## What changes

| | sushy-tools | kubevirtbmc |
| --- | --- | --- |
| BMCs per endpoint | one listener for the whole fleet | one per machine, its own Service |
| `hack/bmc-ip-aliases.sh` | required | **not needed at all** |
| Redfish system id | libvirt domain UUID | always `1` |
| `{{ .Serial }}` in the address template | required | **not needed** |
| Device serial in NetBox | must be the domain UUID | a real serial, whatever you set |
| Addressed by | IP | **Kubernetes Service DNS name** |
| Transport | `redfish+http://` (plaintext) | `redfish://` (TLS, self-signed) |
| BMC credentials | one shared login | per machine Secret, commonly named `<vm>-bmc`: **the name the import deletes** ([see below](#the-credentials-secret-name-collides)) |

Two of the three things the README warns about stop applying. The token paste
still bites; the rest of this page is the one that replaces them.

### `hack/bmc-ip-aliases.sh` is not needed

That script exists only because sushy-tools serves an entire fleet from one
listener, which collides with NetBox's `ENFORCE_GLOBAL_UNIQUE`: ten devices,
one address. kubevirtbmc gives every machine its own BMC, so the problem it
solves does not exist. **Skip runbook step 3 entirely.**

### The serial stops being a smuggling channel

sushy-tools keys systems by libvirt domain UUID, which is why the seed writes
the UUID into the NetBox device serial and the address template renders
`{{ .Serial }}`. kubevirtbmc serves `/redfish/v1/Systems/1` and
`/redfish/v1/Chassis/1` regardless, and takes its advertised serial from a
`BMC_SERIAL_NUMBER` environment variable.

So the serial goes back to being a serial. It still has to be non-empty (it is
one of the five requirements), but it can be whatever your VMs actually claim,
which is a better demo anyway: the DCIM serial and the machine's serial agree
because they are the same fact, not because a script copied a UUID.

**Not every virtbmc build reads `BMC_SERIAL_NUMBER`.**
`ghcr.io/loft-sh/kubevirtbmc/virtbmc:v0.7.0-vcluster` ignores it and reports
`SerialNumber: "000000000000"` (and an all-zero UUID) for every machine, with
the variable set. The import does not care: it only requires a non-empty NetBox
serial and never compares it with Redfish. But the "same fact" story only holds
on a build that honours the variable, so check one before you tell it:

```bash
curl -sk -u '<user>:<password>' https://<pinned-ip>:443/redfish/v1/Systems/1 | grep -o '"SerialNumber":"[^"]*"'
```

---

## The one new problem: DNS names, not IPs

kubevirtbmc's BMC is a Service. It is addressed like this:

```text
redfish://<vm-name>-virtbmc.<namespace>.svc.cluster.local:443
```

**The import requires an IP.** There is no way around this with a template
alone:

- `missingRequirements` flags `BMCAddress` unless the device has an `oob_ip` or
  an IP on a `mgmt_only` interface. Unlike `BootMACAddress` and
  `BMCCredentials`, which the host template can satisfy with
  `bootMACAddress` and `bmc.credentialsName`, there is **no host-template
  escape hatch for the address**.
- `RenderBMCAddress` returns an empty string outright when the management IP is
  empty, so even a template that never mentions `{{ .Address }}` renders
  nothing without one.

An IP has to be in NetBox. The question is whether it is a real one.

### Option A: render the DNS name, park a dummy IP

`{{ .Device }}` is the NetBox device name, so the address can be built from it:

```yaml
addressTemplate: "redfish://{{ .Device }}-virtbmc.<namespace>.svc.cluster.local:443"
```

Name the NetBox device after the VM and that renders exactly what a
kubevirtbmc chart hardcodes into its BareMetalHost.

You still have to put something in `oob_ip` to clear the gate, and nothing ever
dials it. That is a fiction in the IPAM, and it is the same objection the
runbook raises against the shared-BMC-IP shortcut: it works, and it also makes
the NetBox record a lie, which is an odd thing to demo about a source of truth.

Take this option when you do not control the Service definitions.

### Option B: pin the ClusterIP and record that (recommended)

Give each virtbmc Service a fixed ClusterIP, allocate those from the service
CIDR, and record them in NetBox as the device's out-of-band IP:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: <vm-name>-virtbmc
spec:
  clusterIP: 10.96.12.31        # pinned, allocated by you, recorded in NetBox
  ports:
    - name: https
      port: 443
      targetPort: https
```

Then the address template is nearly the platform default, plus the port:

```yaml
addressTemplate: "redfish://{{ .Address }}:443"
```

NetBox records something real and dialable, the IPAM story stays true, and the
service CIDR becomes a NetBox prefix, which is a legitimate thing for a DCIM to
own. It also means a NetBox device with no `oob_ip` is a genuine "nobody has
allocated this machine a BMC address yet" rather than a formality.

**Pin the ClusterIP, do not let Kubernetes assign it.** `spec.bmc.address` is
immutable once the host has registered (see
[netbox-data-model.md](netbox-data-model.md#what-can-and-cannot-be-corrected-later)),
so a Service recreated with a different address strands the BareMetalHost and
forces a delete-and-reimport. A pinned value survives recreation.

### Option B also needs the TLS terminator to answer on the IP

This is the step that fails first, and it fails before NetBox is involved at
all. The usual kubevirtbmc front end is a Caddy sidecar with `tls internal`
whose only site address is the Service DNS name:

```text
<vm-name>-virtbmc.<namespace>.svc.cluster.local:443 {
    tls internal
    reverse_proxy localhost:10080
}
```

Caddy picks the certificate by SNI. A client that dials an IP sends no SNI, so
Caddy has no certificate to offer and aborts the handshake:

```text
curl -sk https://10.128.0.201:443/redfish/v1/Systems/1
OpenSSL/3.3.2: error:0A000438:SSL routines::tlsv1 alert internal error
```

The same request by DNS name returns the system document. That is the whole
symptom: Redfish works by name and fails by IP. Ironic, given
`redfish://<pinned-ip>:443`, fails the same way.

Serve the pinned IP as a second site address and make it the default for
SNI-less clients:

```text
{
    default_sni <pinned-ip>
}
<vm-name>-virtbmc.<namespace>.svc.cluster.local:443, <pinned-ip>:443 {
    tls internal
    reverse_proxy localhost:10080
}
```

Caddy's internal CA issues IP SANs, and `disableCertificateVerification: true`
in the host template already covers the self-signed chain. Keeping the DNS
site means hosts addressed by name keep working through the change.
[manifests/kubevirtbmc/virtbmc-machine.yaml](../manifests/kubevirtbmc/virtbmc-machine.yaml)
has the full sidecar.

Check it the way Ironic will, by IP, from inside the cluster:

```bash
kubectl --context "$METAL3_CONTEXT" -n "$METAL3_NS" run redfish-check --rm -i --restart=Never \
  --image=curlimages/curl --command -- \
  curl -sk -u '<user>:<password>' -o /dev/null -w '%{http_code}\n' \
  https://<pinned-ip>:443/redfish/v1/Systems/1
```

`200` is a pass, and a wrong password should give `401`. Run it from the
Ironic pod too (`kubectl exec ironic-0 -c ironic -- curl ...`) if Ironic runs
anywhere unusual.

### Picking and pinning the addresses

Take them from the low end of the Service CIDR. Kubernetes reserves the bottom
of the range for static assignment and allocates dynamically from above it
first, so a pinned address there does not race the allocator. For a `/16` that
band is the first 256 addresses: `10.128.0.0`-`10.128.0.255` for
`10.128.0.0/16`. `.1` is always the `kubernetes` Service, and kubeadm-built
clusters also pin kube-dns at `.10`.
Check before you pick:

```bash
kubectl get svc -A -o jsonpath='{range .items[*]}{.spec.clusterIP}{"\n"}{end}' | sort -V
```

`clusterIP` is immutable on a live Service. Moving an existing virtbmc Service
onto its pinned address means deleting it and letting your chart or reconciler
recreate it; an in-place apply is refused with
`spec.clusterIPs[0]: Invalid value: ["<ip>"]: may not change once set`.
Hosts already registered by DNS name are not affected by the move, so do this
before the import creates any host, not after.

Record the Service CIDR as a NetBox prefix while you are there. It is what
those `oob_ip` addresses actually belong to.

---

## The credentials Secret name collides

kubevirtbmc deployments commonly render one login Secret per machine, named
`<vm-name>-bmc`, and point the virtbmc container's `BMC_USERNAME` /
`BMC_PASSWORD` at it. That is exactly the name the import computes for the
Secret it writes, `<host>-bmc`, and the device name is the VM name. Two things
follow, both read from the platform source at v4.13.0-alpha.22
(`pkg/controllers/netbox/host.go`) rather than observed:

- **With per-device custom fields**, the import finds the existing Secret and
  overwrites its data with the NetBox login, without an ownership check. Same
  values, no harm; different values, and virtbmc and Ironic now disagree.
- **On untag, the release deletes `<host>-bmc` unconditionally**, once the host
  is gone. That holds **even when the host template pins
  `bmc.credentialsName`**: the delete does not consult the template. virtbmc
  loses the Secret its env references, and the next restart of that pod fails
  `CreateContainerConfigError` until something recreates it.

[troubleshooting.md](troubleshooting.md#a-secret-named-host-bmc-disappeared)
describes the general rule; on this lane it is the default naming rather than
an unlucky collision. Before wiring the provider:

1. Give every virtbmc one shared Secret under a name no device can produce,
   e.g. `virtbmc-shared-creds`
   ([manifests/metal3/shared-bmc-creds-secret.yaml](../manifests/metal3/shared-bmc-creds-secret.yaml)
   is the shape; any name that does not end in `-bmc` for a device name works).
2. Point the virtbmc env at it, and pin the same name with
   `bareMetalHostTemplate.spec.bmc.credentialsName`. The import then writes no
   per-host Secret and the login custom fields become optional.
3. Remove the per-host `<vm-name>-bmc` Secrets, or keep them only for hosts the
   import does not own.

That is the setup the second reference lab ran (below): no `<host>-bmc` Secret
was created for any imported host, and NetBox held no credentials.

---

## What the NetBox record looks like

Per device, for a kubevirtbmc fleet:

| NetBox | Value | Note |
| --- | --- | --- |
| Name | the KubeVirt VM name | option A renders the Service name from it |
| Serial | the VM's serial | whatever `BMC_SERIAL_NUMBER` advertises, if your virtbmc build reads it (below) |
| `oob_ip` | the pinned virtbmc ClusterIP | option B; a placeholder under option A. On a `mgmt_only` interface `bmc0` |
| Interface `eth0` + MAC | the VM's pinned NIC MAC | pin MACs in the VM spec, or they change on recreate |
| `bmc_username` / `bmc_password` | what virtbmc checks against | better: pin a shared `bmc.credentialsName` and skip both ([why](#the-credentials-secret-name-collides)) |
| Tag | your sync tag | as always |

`hack/seed-netbox.py` reads a libvirt inventory and will not help you here. For
a KubeVirt fleet the machines are already declared in a chart or a set of CRs,
so generate the NetBox record from the same values file that generates the VMs,
or enter a handful by hand. The seeding script is a convenience for the
sushy-tools lane, not a dependency of the import.

Pin the MACs. A KubeVirt VM that gets a fresh MAC on recreate invalidates the
NetBox boot MAC, and `bootMACAddress` is the one field Metal3 will **never**
accept a change to once set. That is a delete-and-reimport, not an edit.

---

## Provider fragment deltas

Against
[metal3-node-provider-netbox.fragment.yaml](../manifests/platform/metal3-node-provider-netbox.fragment.yaml):

```yaml
netBox:
  # option B; see above for option A
  addressTemplate: "redfish://{{ .Address }}:443"

  bareMetalHostTemplate:
    spec:
      bmc:
        # The one login every virtbmc checks; keeps <host>-bmc out of the picture.
        credentialsName: virtbmc-shared-creds
        # REQUIRED, not merely convenient: virtbmc is commonly fronted by a TLS
        # terminator using an internally-issued certificate.
        disableCertificateVerification: true
      # KubeVirt disk bus decides this: virtio -> /dev/vda, scsi/sata/usb -> /dev/sda
      rootDeviceHints:
        deviceName: /dev/vda
      automatedCleaningMode: disabled
```

The whole block, ready to merge, is
[manifests/kubevirtbmc/metal3-node-provider-netbox.kubevirtbmc.fragment.yaml](../manifests/kubevirtbmc/metal3-node-provider-netbox.kubevirtbmc.fragment.yaml).
With `credentialsName` pinned, drop `customFields` from the provider and the
`bmc_username` / `bmc_password` fields from NetBox.

Four things to notice:

**`redfish://`, not `redfish+http://`.** The sushy lane uses plaintext because
sushy-tools serves plaintext. A kubevirtbmc deployment usually has TLS in front
of virtbmc's plain Redfish port, so the scheme goes back to the default and
certificate verification has to be off.

**`credentialsName`, not custom fields.** The sushy lane writes a per-host
`<host>-bmc` Secret from NetBox, which is the feature worth showing there. On
this lane that name is usually already taken by virtbmc's own Secret; see
[the collision](#the-credentials-secret-name-collides).

**`automatedCleaningMode: disabled`.** The sushy fragment uses `metadata` to
exercise the full clean lifecycle. Existing kubevirtbmc demos disable it.
Match whichever your VM definitions expect rather than copying either blindly.

**`rootDeviceHints` is one value for every host.** This is the real limitation.
KubeVirt's guest device name follows the disk bus, so a fleet mixing virtio and
scsi/sata/usb disks needs two different hints, and `bareMetalHostTemplate.spec`
applies a single value to all of them. Same shape as any per-host value a
generator used to stamp: the template is not per-device.

Your options are a uniform disk bus across the tagged fleet, or one
NodeProvider per bus with a different tag for each. The first is easier and is
what a demo should do.

---

## Placement

Ironic has to be able to reach the BMC, which for both options means Ironic and
the VMs share a cluster: a ClusterIP is not routable from outside it, and
`*.svc.cluster.local` does not resolve from outside it either.

That is worth stating because the reference lab already splits the Platform
cluster from the Metal3 cluster, and it is easy to read that split as "things
can live anywhere". The NetBox **sync** runs in the Platform and needs to reach
NetBox. **Ironic** needs to reach the BMC. With kubevirtbmc those are two
different networks and only the second one changed.

If the VMs genuinely must live in a different cluster from Ironic, neither
option above works and you need the BMCs exposed as real routable endpoints,
at which point you are back to recording ordinary IPs and none of this page
applies.

---

## What is verified here, and what is not

### Run end to end on a kubevirtbmc fleet (October 2026)

The import has now been run against a kubevirtbmc fleet, on a second reference
lab. That lab:
- is one vCluster Standalone cluster that runs vCluster Platform
  4.13.0-alpha.22, Metal3/Ironic and the KubeVirt VMs together;
- has four machines, each a VM plus a virtbmc StatefulSet
  (`ghcr.io/loft-sh/kubevirtbmc/virtbmc:v0.7.0-vcluster`, Caddy `tls internal`
  in front) and its Service;
- runs NetBox 4.7.0 from chart 8.3.77;
- uses option B, a shared `credentialsName`, `/dev/vda` and cleaning
  `disabled`.

Observed, in order:

1. **The whole chain works.** For each tagged device:
   - a Machine, then a BareMetalHost with `bmc.address:
     redfish://<pinned-ip>:443` and the pinned boot MAC;
   - `Registered=True`, then `inspecting`, then `available`, powered off,
     about four minutes after the tag;
   - then a tenant cluster claimed one of them through a pool selecting on
     `netbox.vcluster.com/rack`, provisioned it, and the node joined.

   No `HostConflict`, no `MissingRequirements`, no change to the platform side.
   `redfish://<ip>:443` with no system path was accepted by BMO and Ironic, as
   the DNS form already was.
2. **The TLS terminator has to answer on the IP.** With the stock DNS-only
   Caddy site, Redfish by ClusterIP failed the handshake (`tlsv1 alert
   internal error`) while the DNS name answered. Adding the IP site and
   `default_sni` fixed it, checked from a pod and from inside the Ironic pod.
   See [Option B](#option-b-also-needs-the-tls-terminator-to-answer-on-the-ip).
3. **The credentials Secret name collides.** The fleet's chart named its
   per-machine Secrets `<vm>-bmc`. Moving virtbmc and the host template onto
   one shared Secret kept the import from writing any `<host>-bmc`. The
   overwrite and delete behaviour that makes this necessary comes from the
   source, not from a run. See
   [the collision](#the-credentials-secret-name-collides).
4. **Pinning ClusterIPs works as described, with one practical step.** The
   addresses came from the Service CIDR's static band. Moving the existing
   Services needed a delete and recreate (`may not change once set`). Hosts
   registered by DNS name were unaffected by the move. See
   [Picking and pinning the addresses](#picking-and-pinning-the-addresses).
5. **The serial only has to be non-empty.** `v0.7.0-vcluster` ignored
   `BMC_SERIAL_NUMBER` and reported `000000000000`. NetBox held `VMETAL-000N`,
   and the import was satisfied either way.
6. **The provisioning IP annotator is needed on this lane too.**
   [manifests/metal3/provisioning-ip-annotator-cronjob.yaml](../manifests/metal3/provisioning-ip-annotator-cronjob.yaml),
   with its pool moved to a free range on that lab's provisioning network,
   stamped each new host within one schedule tick. Each host then inspected on
   that address. At claim, the platform rewrote it to the tenant's range and
   kept the annotator's value as `metal3.vcluster.com/original-ip-address`,
   for release.

### Still not verified on this lane

- Option A (DNS name rendered from `{{ .Device }}` with a parked `oob_ip`).
- Per-device credentials from NetBox custom fields, and BMC password rotation.
  The shared Secret was used throughout.
- Ironic and the VMs in different clusters. Placement is unchanged from
  [above](#placement): neither option works across a cluster boundary.
- A mixed disk bus. Every VM was virtio.

### Earlier verification, still current

Verified by reading the platform source: the `BMCAddress` requirement has no
host-template escape hatch, `RenderBMCAddress` short-circuits on an empty
management IP, and `{{ .Device }}` carries the NetBox device name.

Verified by reading a working kubevirtbmc deployment: one StatefulSet, Service
and credentials Secret per machine; `/redfish/v1/Systems/1` and
`/redfish/v1/Chassis/1`; TLS terminated in front of virtbmc's plaintext port;
`redfish://<name>-virtbmc.<ns>.svc.cluster.local:443` as a BareMetalHost
`bmc.address` that Metal3 accepts, with no system path on the end.
