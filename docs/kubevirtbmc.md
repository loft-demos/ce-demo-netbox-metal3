# Running the import against kubevirtbmc instead of sushy-tools

Short answer: **yes, and two of the awkward parts go away.** One new problem
takes their place, and it needs a decision before you seed anything.

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
| BMC credentials | one shared login | per machine, already a Secret |

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

---

## What the NetBox record looks like

Per device, for a kubevirtbmc fleet:

| NetBox | Value | Note |
| --- | --- | --- |
| Name | the KubeVirt VM name | option A renders the Service name from it |
| Serial | the VM's serial | whatever `BMC_SERIAL_NUMBER` advertises |
| `oob_ip` | the pinned virtbmc ClusterIP | option B; a placeholder under option A |
| Interface `eth0` + MAC | the VM's pinned NIC MAC | pin MACs in the VM spec, or they change on recreate |
| `bmc_username` / `bmc_password` | what virtbmc checks against | or pin `bmc.credentialsName` and skip both |
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
        # REQUIRED, not merely convenient: virtbmc is commonly fronted by a TLS
        # terminator using an internally-issued certificate.
        disableCertificateVerification: true
      # KubeVirt disk bus decides this: virtio -> /dev/vda, scsi/sata/usb -> /dev/sda
      rootDeviceHints:
        deviceName: /dev/vda
      automatedCleaningMode: disabled
```

Three things to notice:

**`redfish://`, not `redfish+http://`.** The sushy lane uses plaintext because
sushy-tools serves plaintext. A kubevirtbmc deployment usually has TLS in front
of virtbmc's plain Redfish port, so the scheme goes back to the default and
certificate verification has to be off.

**`automatedCleaningMode: disabled`.** The sushy fragment uses `metadata` to
exercise the full clean lifecycle. Existing kubevirtbmc demos disable it.
Match whichever your VM definitions expect rather than copying either blindly.

**`rootDeviceHints` is one value for every host.** This is the real limitation.
KubeVirt's guest device name follows the disk bus, so a fleet mixing virtio and
scsi/sata/usb disks needs two different hints, and `bareMetalHostTemplate.spec`
applies a single value to all of them. Same shape as the
`lan.vcluster.com/mac` gap: the template is not per-device.

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

Verified by reading the platform source: the `BMCAddress` requirement has no
host-template escape hatch, `RenderBMCAddress` short-circuits on an empty
management IP, and `{{ .Device }}` carries the NetBox device name.

Verified by reading a working kubevirtbmc deployment: one StatefulSet, Service
and credentials Secret per machine; `/redfish/v1/Systems/1` and
`/redfish/v1/Chassis/1`; TLS terminated in front of virtbmc's plaintext port;
`redfish://<name>-virtbmc.<ns>.svc.cluster.local:443` as a BareMetalHost
`bmc.address` that Metal3 accepts, with no system path on the end.

**Not verified: the two halves together.** Nobody has yet run the NetBox import
against a kubevirtbmc fleet. The reasoning above is sound and the failure modes
are the ordinary ones this repo already documents, but treat the first run as a
first run, and start with a single tagged device.
