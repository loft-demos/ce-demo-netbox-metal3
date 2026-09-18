#!/usr/bin/env python3
"""Seed NetBox with an emulated bare-metal fleet.

EMULATED FLEETS ONLY. If your devices are real and already in NetBox, you do
not need this: add the sync tag, confirm the five requirements in
docs/netbox-data-model.md, and you are done. This script exists so people
without a rack can still run the demo.

Reads a libvirt inventory (the file a sushy-tools demo's create-vms.sh writes)
and builds the NetBox record vCluster Platform's Metal3 NetBox import needs: a
site, a location, racks, a manufacturer, device types, device roles, and one
device per libvirt domain carrying the sync tag, its serial, its BMC address
and its boot MAC.

Inventory format, whitespace-separated, `#` comments ignored:

    NAME UUID MAC PROFILE FIRMWARE RACK [LAN_MAC] [CUSTOMER] [BMC]

Only the first six are required. A name shaped `<rack>-u<NN>-<profile>` also
states its rack elevation, which the seed uses.

Idempotent: every object is looked up by its natural key and patched in place,
so re-running after a fleet rebuild converges rather than duplicating.

Stdlib only -- it runs on the hypervisor host with no pip install.

  source env.sh          # or set NETBOX_URL / NETBOX_TOKEN by hand
  python3 hack/seed-netbox.py --inventory "$VM_INVENTORY" --dry-run
  python3 hack/seed-netbox.py --inventory "$VM_INVENTORY" --with-modules

NETBOX_TOKEN must be the WRITE-enabled token. The platform's own token should
be read-only; this script is the only thing here that writes to NetBox.

Add --with-modules to also record CPU/GPU modules, which is what fills in
Machine.status.hardware on the platform side.

NO DEVICE GETS A GPU unless you name it with --gpu-device. That is deliberate:
an emulated fleet has as many GPUs as the host physically owns, usually one or
none, and seeding a whole size class as gpu-compute puts machines in a pool
that cannot run the workload.
"""

import argparse
import json
import re
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# NetBox v2 tokens (4.5+) are presented as `nbt_<key>.<secret>` and NetBox's own
# UI prints them behind `Bearer`; v1 tokens are a bare 40-char string behind
# `Token`. NetBox infers the version from the value rather than the keyword, so
# either scheme carries either token; this script sends the conventional one for
# whichever it is handed. Pass the value only, never the scheme word.
V2_TOKEN_PREFIX = "nbt_"

# A GPU is a fact about a specific machine, not about a size class: whichever
# domain the card is passed through to gets it, and every other machine is
# CPU-only. Default is none at all; name one with --gpu-device.
#
# NOTE: the inventory NAME column is not necessarily the libvirt domain name.
# The UUID column is the join between the two, and it is what sushy-tools
# serves as the Redfish system id.
GPU_DEVICE = ""
GPU_MANUFACTURER = "NVIDIA"
GPU_MODEL = "RTX 2000 Ada"

CPU_ROLE = "cpu-compute"
GPU_ROLE = "gpu-compute"

# NetBox device roles. The role slug becomes the platform label
# netbox.vcluster.com/role, which is what NodeTypes select on.
ROLES = {
    CPU_ROLE: ("CPU Compute", "2f6ec9"),
    GPU_ROLE: ("GPU Compute", "9c27b0"),
}

# Rack units and vCPU counts for the profile names a typical emulated fleet
# uses. An unknown profile is NOT an error: it gets a 1U device type named
# after itself and a CPU module with no stated core count, which is better than
# refusing to seed an inventory whose size classes happen to be spelled
# differently.
PROFILES = {
    "small": {"u_height": 1, "cores": 2},
    "medium": {"u_height": 1, "cores": 3},
    "large": {"u_height": 1, "cores": 4},
    "xlarge": {"u_height": 2, "cores": 8},
    "xlarge-gpu": {"u_height": 2, "cores": 8},
}
UNKNOWN_PROFILE = {"u_height": 1, "cores": None}


def device_type_for(profile, prefix):
    """(model, slug, u_height) for a size class.

    The slug becomes netbox.vcluster.com/device-type, which NodeType pools
    select on, so changing --device-type-prefix means rewriting those
    selectors.
    """
    u_height = PROFILES.get(profile, UNKNOWN_PROFILE)["u_height"]
    slug = f"{prefix}-{profile}" if prefix else profile
    model = " ".join(word.capitalize() for word in slug.split("-"))
    return model, slug, u_height


def cpu_module_for(profile, manufacturer):
    """(bay name, manufacturer, model) for the CPU module of a size class.

    Module bays and types only, no module-type profiles, so the platform gets
    counts and models rather than core counts. See docs/netbox-data-model.md.
    """
    cores = PROFILES.get(profile, UNKNOWN_PROFILE)["cores"]
    model = f"vCPU Package ({cores} core)" if cores else "vCPU Package"
    return ("CPU1", manufacturer, model)


def _ref_id(value):
    """NetBox returns related objects nested; a payload sends their id."""
    if isinstance(value, dict):
        return value.get("id")
    return value


def _same(current, wanted):
    """Compare what NetBox returned against what we want to set.

    NetBox echoes related objects as nested dicts, choice fields as
    {"value": ..., "label": ...}, and decimals as floats -- none of which
    compare equal to the scalar a payload carries.
    """
    if isinstance(current, dict):
        if "id" in current:
            current = current["id"]
        elif "value" in current:
            current = current["value"]
    if isinstance(current, (int, float)) and isinstance(wanted, (int, float)):
        return float(current) == float(wanted)
    try:
        if current is not None and wanted is not None:
            return float(current) == float(wanted)
    except (TypeError, ValueError):
        pass
    return current == wanted


class NetBox:
    def __init__(self, url, token, dry_run=False, insecure=False):
        self.base = url.rstrip("/")
        self.dry_run = dry_run
        if token.startswith(V2_TOKEN_PREFIX):
            self.auth = "Bearer " + token
        else:
            self.auth = "Token " + token
        self.opener = urllib.request.build_opener()
        if insecure:
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx)
            )

    def request(self, method, path, params=None, payload=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=60) as response:
                text = response.read().decode()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as err:
            detail = err.read().decode()[:2000]
            raise SystemExit(
                f"\nNetBox {method} {path} failed with {err.code}:\n{detail}\n"
            )
        except urllib.error.URLError as err:
            raise SystemExit(f"\nCannot reach NetBox at {self.base}: {err}\n")

    def find(self, path, params):
        page = self.request("GET", path, params=params)
        results = page.get("results", []) if page else []
        return results[0] if results else None

    def ensure(self, path, lookup, payload, label):
        """Create the object or patch the fields that drifted."""
        existing = self.find(path, lookup)
        if existing is None:
            if self.dry_run:
                print(f"  + would create {label}")
                return {"id": f"<new {label}>", **payload}
            created = self.request("POST", path, payload=payload)
            print(f"  + created {label} (id {created['id']})")
            return created

        drift = {}
        for key, value in payload.items():
            if key == "tags":
                current = sorted(_ref_id(t) for t in existing.get("tags", []))
                if current != sorted(value):
                    drift[key] = value
                continue
            if key == "custom_fields":
                have = existing.get("custom_fields") or {}
                if any(have.get(k) != v for k, v in value.items()):
                    drift[key] = {**have, **value}
                continue
            if not _same(existing.get(key), value):
                drift[key] = value
        if not drift:
            print(f"  = {label} up to date (id {existing['id']})")
            return existing
        if self.dry_run:
            print(f"  ~ would patch {label}: {sorted(drift)}")
            return existing
        updated = self.request(
            "PATCH", f"{path}{existing['id']}/", payload=drift
        )
        print(f"  ~ patched {label}: {sorted(drift)}")
        return updated


def parse_inventory(path):
    """Parse the libvirt inventory file.

    Columns: NAME UUID MAC PROFILE FIRMWARE RACK [LAN_MAC] [CUSTOMER] [BMC]
    Field 7 is a MAC when it looks like one, a customer name otherwise.

    A name shaped <rack>-u<NN>-<profile> states its own rack unit, so use it
    rather than numbering devices in file order: a NetBox elevation that
    disagrees with the machine's own name is worse than no elevation at all.
    Names without a -uNN- fall back to the sequential --rack-u-start counter.
    """
    machines = []
    with open(path) as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 6:
                raise SystemExit(f"inventory line needs >=6 columns: {line}")
            entry = {
                "name": fields[0],
                "uuid": fields[1],
                "mac": fields[2],
                "profile": fields[3],
                "firmware": fields[4],
                "rack": fields[5],
                "lan_mac": "",
                "position": None,
            }
            unit = re.search(r"(?:^|-)u(\d+)(?:-|$)", entry["name"])
            if unit:
                entry["position"] = int(unit.group(1))
            if len(fields) >= 7 and is_mac(fields[6]):
                entry["lan_mac"] = fields[6]
            machines.append(entry)
    if not machines:
        raise SystemExit(f"no machines found in {path}")
    return machines


def is_mac(value):
    parts = value.split(":")
    return len(parts) == 6 and all(
        len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts
    )


def ip_add(start, offset):
    octets = [int(o) for o in start.split(".")]
    octets[3] += offset
    if octets[3] > 254:
        raise SystemExit(f"BMC IP range overflowed past {start}+{offset}")
    return ".".join(str(o) for o in octets)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    env = os.environ.get

    # Defaults come from env.sh where there is one, so a sourced environment
    # plus `--inventory` is the whole invocation.
    parser.add_argument("--url", default=env("NETBOX_URL"))
    parser.add_argument(
        "--token",
        default=env("NETBOX_TOKEN"),
        help="a WRITE-enabled NetBox token, value only, no scheme word",
    )
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument(
        "--inventory", default=env("VM_INVENTORY", "configs/vm-inventory.txt")
    )
    parser.add_argument("--site", default=env("NETBOX_SITE", "dc1"))
    parser.add_argument("--location", default=env("NETBOX_LOCATION", "row-1"))
    parser.add_argument(
        "--manufacturer",
        default=env("NETBOX_MANUFACTURER", "QEMU"),
        help="device manufacturer, and the manufacturer of the CPU modules "
        "--with-modules records (default QEMU, which is what these are)",
    )
    parser.add_argument(
        "--device-type-prefix",
        default=env("NETBOX_DEVICE_TYPE_PREFIX", "demo"),
        help="prefixes every device type slug, e.g. small -> demo-small "
        "(default demo). The slug becomes netbox.vcluster.com/device-type, so "
        "changing it means rewriting your NodeType selectors. Pass an empty "
        "string for bare profile names.",
    )
    parser.add_argument("--tag", default=env("NETBOX_SYNC_TAG", "vcluster-sync"))
    parser.add_argument("--bmc-username", default=env("BMC_USERNAME", "admin"))
    parser.add_argument("--bmc-password", default=env("BMC_PASSWORD", "password"))
    parser.add_argument(
        "--bmc-ip-start",
        default=env("BMC_IP_START", "172.22.0.3"),
        help="first emulated BMC address, incremented per device. Must clear "
        "the bridge address, any DHCP proxy VIP, and the NodeProvider's own "
        "provisioning IPAM range",
    )
    parser.add_argument(
        "--provision-cidr", default=env("PROVISION_CIDR", "172.22.0.0/24")
    )
    parser.add_argument(
        "--rack-u-start",
        type=int,
        default=10,
        help="first rack unit for machines whose name does not carry a -uNN-",
    )
    parser.add_argument("--with-modules", action="store_true")
    parser.add_argument(
        "--gpu-device",
        default=env("GPU_DEVICE", GPU_DEVICE),
        help="inventory NAME of the one machine the physical GPU is passed "
        "through to. Default is none: no device gets a GPU module or the "
        "gpu-compute role unless you name it here. Repeat the flag is not "
        "supported; if you genuinely have several, seed them and then set the "
        "role on the rest in the NetBox UI",
    )
    parser.add_argument("--gpu-manufacturer", default=GPU_MANUFACTURER)
    parser.add_argument("--gpu-model", default=GPU_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.url or not args.token:
        raise SystemExit("set --url/--token or NETBOX_URL/NETBOX_TOKEN")

    machines = parse_inventory(args.inventory)
    gpu_device = args.gpu_device.strip()
    names = {m["name"] for m in machines}
    if gpu_device and gpu_device not in names:
        raise SystemExit(
            f"--gpu-device {gpu_device!r} is not in {args.inventory}.\n"
            "Name the machine the card is actually passed through to, or omit "
            "--gpu-device to record no GPU at all. Note this is the inventory "
            "NAME column, which may not be the libvirt domain name.\n"
            f"Inventory has: {', '.join(sorted(names))}"
        )
    nb = NetBox(args.url, args.token, dry_run=args.dry_run, insecure=args.insecure)
    prefix_len = args.provision_cidr.split("/")[1]

    print(f"NetBox: {nb.base}   devices in inventory: {len(machines)}")
    if gpu_device:
        print(f"GPU:    {args.gpu_manufacturer} {args.gpu_model} on {gpu_device} (all others CPU-only)")
    else:
        print("GPU:    none recorded (all devices CPU-only)")

    # ---- tag, custom fields ------------------------------------------------
    print("\n[1/6] tag and custom fields")
    tag = nb.ensure(
        "/api/extras/tags/",
        {"slug": args.tag},
        {
            "name": args.tag,
            "slug": args.tag,
            "color": "ff6600",
            "description": "Imported by vCluster Platform into the Metal3 NodeProvider",
        },
        f"tag {args.tag}",
    )
    for field, label, description in (
        ("bmc_username", "BMC username", "BMC login name read by the vCluster Platform NetBox import"),
        ("bmc_password", "BMC password", "BMC password read by the vCluster Platform NetBox import (never served by the platform API)"),
    ):
        nb.ensure(
            "/api/extras/custom-fields/",
            {"name": field},
            {
                "object_types": ["dcim.device"],
                "type": "text",
                "name": field,
                "label": label,
                "description": description,
            },
            f"custom field {field}",
        )

    # ---- physical hierarchy ------------------------------------------------
    print("\n[2/6] site, location, racks")
    site = nb.ensure(
        "/api/dcim/sites/",
        {"slug": args.site},
        {"name": args.site, "slug": args.site, "status": "active"},
        f"site {args.site}",
    )
    location = nb.ensure(
        "/api/dcim/locations/",
        {"slug": args.location, "site_id": site["id"]},
        {"name": args.location, "slug": args.location, "site": site["id"], "status": "active"},
        f"location {args.location}",
    )
    racks = {}
    for rack_name in sorted({m["rack"] for m in machines}):
        racks[rack_name] = nb.ensure(
            "/api/dcim/racks/",
            {"name": rack_name, "site_id": site["id"]},
            {
                "name": rack_name,
                "site": site["id"],
                "location": location["id"],
                "status": "active",
                "u_height": 42,
            },
            f"rack {rack_name}",
        )

    nb.ensure(
        "/api/ipam/prefixes/",
        {"prefix": args.provision_cidr},
        {
            "prefix": args.provision_cidr,
            "site": site["id"],
            "status": "active",
            "description": f"provisioning network {args.provision_cidr} (PXE + emulated BMCs)",
        },
        f"prefix {args.provision_cidr}",
    )

    # ---- catalog -----------------------------------------------------------
    print("\n[3/6] manufacturer, device types, roles")
    manufacturer_slug = args.manufacturer.lower().replace(" ", "-")
    manufacturer = nb.ensure(
        "/api/dcim/manufacturers/",
        {"slug": manufacturer_slug},
        {"name": args.manufacturer, "slug": manufacturer_slug},
        f"manufacturer {args.manufacturer}",
    )

    device_types = {}
    unknown = sorted({m["profile"] for m in machines} - set(PROFILES))
    if unknown:
        print(
            f"  note: profiles not in the built-in table ({', '.join(unknown)}) "
            "get a 1U device type and a CPU module with no core count"
        )
    for profile in sorted({m["profile"] for m in machines}):
        model, slug, u_height = device_type_for(profile, args.device_type_prefix)
        device_types[profile] = nb.ensure(
            "/api/dcim/device-types/",
            {"slug": slug},
            {
                "manufacturer": manufacturer["id"],
                "model": model,
                "slug": slug,
                "u_height": u_height,
            },
            f"device type {slug}",
        )

    # The role is per machine, not per size class: only the domain holding the
    # one physical card is gpu-compute, even though it shares a size class with
    # CPU-only siblings.
    roles = {}
    for machine in machines:
        slug = GPU_ROLE if machine["name"] == gpu_device else CPU_ROLE
        if slug not in roles:
            name, color = ROLES[slug]
            roles[slug] = nb.ensure(
                "/api/dcim/device-roles/",
                {"slug": slug},
                {"name": name, "slug": slug, "color": color},
                f"device role {slug}",
            )
        machine["role_slug"] = slug

    # ---- devices -----------------------------------------------------------
    print("\n[4/6] devices")
    next_position = {rack: args.rack_u_start for rack in racks}
    claimed = {}
    for entry in machines:
        if entry["position"] is not None:
            claimed.setdefault(entry["rack"], set()).add(entry["position"])
    bmc_ips = []
    for index, machine in enumerate(machines):
        rack = racks[machine["rack"]]
        position = machine["position"]
        if position is None:
            taken = claimed.setdefault(machine["rack"], set())
            position = next_position[machine["rack"]]
            while position in taken:
                position += 2
            next_position[machine["rack"]] = position + 2
            taken.add(position)
        bmc_ip = ip_add(args.bmc_ip_start, index)
        bmc_ips.append((machine["name"], bmc_ip))

        print(f"\n  {machine['name']}  ({machine['profile']}, {machine['rack']} U{position}, bmc {bmc_ip})")
        device = nb.ensure(
            "/api/dcim/devices/",
            {"name": machine["name"]},
            {
                "name": machine["name"],
                "role": roles[machine["role_slug"]]["id"],
                "device_type": device_types[machine["profile"]]["id"],
                "site": site["id"],
                "location": location["id"],
                "rack": rack["id"],
                "position": position,
                "face": "front",
                "status": "active",
                # The serial is the libvirt domain UUID on purpose: it is what
                # sushy-tools uses as the Redfish system id, and the provider's
                # addressTemplate renders it into bmc.address.
                "serial": machine["uuid"],
                "description": f"emulated machine, libvirt domain ({machine['firmware']} boot)",
                "tags": [tag["id"]],
                "custom_fields": {
                    "bmc_username": args.bmc_username,
                    "bmc_password": args.bmc_password,
                },
            },
            f"device {machine['name']}",
        )
        if args.dry_run and not isinstance(device.get("id"), int):
            # Nothing below can be looked up without a real device id, so a
            # dry run over an empty NetBox stops at the device itself.
            print("    (dry run: interfaces, MACs and BMC IP need a real device id)")
            continue

        interfaces = {}
        wanted = [("bmc0", True), ("eth0", False)]
        if machine["lan_mac"]:
            wanted.append(("eth1", False))
        for iface_name, mgmt_only in wanted:
            interfaces[iface_name] = nb.ensure(
                "/api/dcim/interfaces/",
                {"device_id": device["id"], "name": iface_name},
                {
                    "device": device["id"],
                    "name": iface_name,
                    "type": "1000base-t",
                    "enabled": True,
                    "mgmt_only": mgmt_only,
                    "description": {
                        "bmc0": "Emulated Redfish BMC (sushy-tools)",
                        "eth0": "Provisioning / PXE",
                        "eth1": "LAN (node identity after install)",
                    }[iface_name],
                },
                f"interface {iface_name}",
            )

        # eth0 must sort first among the non-management interfaces: the import
        # picks the boot MAC by interface name order, skipping mgmt_only ports.
        for iface_name, mac in (("eth0", machine["mac"]), ("eth1", machine["lan_mac"])):
            if not mac or iface_name not in interfaces:
                continue
            mac = mac.upper()
            mac_object = nb.ensure(
                "/api/dcim/mac-addresses/",
                {"mac_address": mac},
                {
                    "mac_address": mac,
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": interfaces[iface_name]["id"],
                },
                f"mac {mac} on {iface_name}",
            )
            if isinstance(mac_object.get("id"), int):
                nb.ensure(
                    "/api/dcim/interfaces/",
                    {"device_id": device["id"], "name": iface_name},
                    {"primary_mac_address": mac_object["id"]},
                    f"primary mac on {iface_name}",
                )

        address = f"{bmc_ip}/{prefix_len}"
        ip_object = nb.ensure(
            "/api/ipam/ip-addresses/",
            {"address": address},
            {
                "address": address,
                "status": "active",
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": interfaces["bmc0"]["id"],
                "description": f"Emulated BMC for {machine['name']}",
            },
            f"ip {address}",
        )
        if isinstance(ip_object.get("id"), int):
            nb.ensure(
                "/api/dcim/devices/",
                {"name": machine["name"]},
                {"oob_ip": ip_object["id"]},
                f"oob_ip on {machine['name']}",
            )

        if args.with_modules:
            bays = [cpu_module_for(machine["profile"], args.manufacturer)]
            if machine["name"] == gpu_device:
                bays.append(("GPU1", args.gpu_manufacturer, args.gpu_model))
            for bay_name, module_manufacturer, model in bays:
                mm_slug = module_manufacturer.lower().replace(" ", "-")
                mm = nb.ensure(
                    "/api/dcim/manufacturers/",
                    {"slug": mm_slug},
                    {"name": module_manufacturer, "slug": mm_slug},
                    f"manufacturer {module_manufacturer}",
                )
                module_type = nb.ensure(
                    "/api/dcim/module-types/",
                    {"model": model},
                    {"manufacturer": mm["id"], "model": model},
                    f"module type {model}",
                )
                bay = nb.ensure(
                    "/api/dcim/module-bays/",
                    {"device_id": device["id"], "name": bay_name},
                    {"device": device["id"], "name": bay_name},
                    f"module bay {bay_name}",
                )
                if not (isinstance(bay.get("id"), int) and isinstance(module_type.get("id"), int)):
                    continue
                nb.ensure(
                    "/api/dcim/modules/",
                    {"device_id": device["id"], "module_bay_id": bay["id"]},
                    {
                        "device": device["id"],
                        "module_bay": bay["id"],
                        "module_type": module_type["id"],
                        "status": "active",
                    },
                    f"module {model} in {bay_name}",
                )

    # ---- what the host still has to do ------------------------------------
    print("\n[5/6] emulated BMC addresses")
    for name, ip in bmc_ips:
        print(f"  {name:24s} {ip}:8000")
    print(
        "\n[6/6] on the host that owns the provisioning bridge, make those "
        "addresses answer:\n"
        "  sudo -E bash hack/bmc-ip-aliases.sh apply\n"
        "  curl -s http://%s:8000/redfish/v1/Systems | head\n" % bmc_ips[0][1]
    )


if __name__ == "__main__":
    sys.exit(main())
