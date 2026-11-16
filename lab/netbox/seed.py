"""Seed a local NetBox with the NetMedic lab devices. UNVERIFIED against a real NetBox.

Reads the devices from inventory.yaml (and their management IPs from
lab/topology.clab.yml), so it stays in sync with the lab. Safe to run repeatedly:
every object is looked up first and only created when missing.

Usage, from the project root:
    NETBOX_URL=http://localhost:8000 NETBOX_TOKEN=... python lab/netbox/seed.py
Requires pynetbox:  pip install -e ".[netbox]"
Written for NetBox 4.x (devices use the `role` field; NetBox 3.x called it `device_role`).
"""

from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path

import pynetbox
import yaml

ROOT = Path(__file__).resolve().parents[2]
SITE = "netmedic-lab"
TAG = os.environ.get("NETBOX_TAG") or "netmedic"

# NetMedic platform -> (NetBox platform slug, platform name, manufacturer, device type)
PLATFORMS = {
    "frr": ("frr", "FRRouting", "FRRouting", "FRR container"),
    "arista_eos": ("arista-eos", "Arista EOS", "Arista", "cEOS-lab"),
    "nokia_srlinux": ("nokia-srlinux", "Nokia SR Linux", "Nokia", "SR Linux container"),
    "linux": ("linux", "Linux", "Linux", "Linux container"),
}
ROLE_COLORS = {"router": "2196f3", "host": "9e9e9e"}


def slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def get_or_create(endpoint, lookup: dict, **fields):
    """Find by `lookup`; create with `fields` (which default to the lookup) if missing."""
    obj = endpoint.get(**lookup)
    if obj is None:
        obj = endpoint.create(**{**lookup, **fields})
        print(f"created {endpoint.name} {lookup}")
    return obj


def mgmt_ips() -> dict[str, str]:
    topo = ROOT / "lab" / "topology.clab.yml"
    if not topo.is_file():
        return {}
    nodes = (yaml.safe_load(topo.read_text(encoding="utf-8")) or {}).get("topology", {})
    return {name: node.get("mgmt-ipv4") for name, node in (nodes.get("nodes") or {}).items()
            if isinstance(node, dict) and node.get("mgmt-ipv4")}


def ipv4_for(name: str, host: str, ips: dict[str, str]) -> str | None:
    if name in ips:
        return ips[name]
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return None


def main() -> int:
    url, token = os.environ.get("NETBOX_URL"), os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        print("set NETBOX_URL and NETBOX_TOKEN", file=sys.stderr)
        return 2
    nb = pynetbox.api(url, token=token)
    inventory = yaml.safe_load((ROOT / "inventory.yaml").read_text(encoding="utf-8"))
    ips = mgmt_ips()

    tag = get_or_create(nb.extras.tags, {"slug": TAG}, name=TAG)
    site = get_or_create(nb.dcim.sites, {"slug": SITE}, name=SITE, status="active")

    for name, fields in (inventory.get("devices") or {}).items():
        platform = fields["platform"]
        if platform not in PLATFORMS:
            print(f"skipping {name}: no NetBox mapping for platform {platform}", file=sys.stderr)
            continue
        p_slug, p_name, maker, model = PLATFORMS[platform]
        manufacturer = get_or_create(nb.dcim.manufacturers, {"slug": slugify(maker)}, name=maker)
        device_type = get_or_create(nb.dcim.device_types, {"slug": slugify(model)},
                                    model=model, manufacturer=manufacturer.id)
        nb_platform = get_or_create(nb.dcim.platforms, {"slug": p_slug}, name=p_name,
                                    manufacturer=manufacturer.id)
        role_name = fields["role"]
        role = get_or_create(nb.dcim.device_roles, {"slug": slugify(role_name)},
                             name=role_name, color=ROLE_COLORS.get(role_name, "607d8b"))
        device = get_or_create(
            nb.dcim.devices, {"name": name},
            device_type=device_type.id, role=role.id, site=site.id, platform=nb_platform.id,
            status="active", tags=[tag.id],
        )
        address = ipv4_for(name, str(fields.get("host", "")), ips)
        if not address:
            print(f"{name}: no IPv4 found; NetMedic will use the device name as host")
            continue
        iface = nb.dcim.interfaces.get(device_id=device.id, name="mgmt0")
        if iface is None:
            iface = nb.dcim.interfaces.create(device=device.id, name="mgmt0", type="virtual")
        ip = get_or_create(nb.ipam.ip_addresses, {"address": f"{address}/32"},
                           assigned_object_type="dcim.interface", assigned_object_id=iface.id)
        if not device.primary_ip4 or device.primary_ip4.id != ip.id:
            device.primary_ip4 = ip.id
            device.save()
            print(f"{name}: primary IPv4 {address}")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
