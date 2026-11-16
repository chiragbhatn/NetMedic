"""NetBox as an inventory source (unverified against a real NetBox; tested with stubs).

Active devices and virtual machines are read through pynetbox and mapped to NetMedic
`Device`s. Cables are not imported, so `links` stays empty.
"""

from __future__ import annotations

from typing import Any

from netmedic.errors import NetMedicError
from netmedic.inventory import Device, Inventory

# NetBox platform slug -> NetMedic platform
PLATFORM_MAP = {
    "frr": "frr",
    "arista-eos": "arista_eos",
    "eos": "arista_eos",
    "nokia-srlinux": "nokia_srlinux",
    "srlinux": "nokia_srlinux",
    "sr-linux": "nokia_srlinux",
    "linux": "linux",
}
PORT_FIELD = "netmedic_port"
INSTALL_HINT = 'pynetbox is not installed; run: pip install -e ".[netbox]"'


def _connect(url: str, token: str) -> Any:
    try:
        import pynetbox
    except ImportError:
        raise NetMedicError(INSTALL_HINT) from None
    return pynetbox.api(url, token=token)


def _slug(value: Any) -> str | None:
    """A nested NetBox object's slug (pynetbox Record or plain dict), or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("slug")
    return getattr(value, "slug", None)


def _host(obj: Any, name: str) -> str:
    """Primary IPv4 without the prefix length; the device name when there is none."""
    ip = getattr(obj, "primary_ip4", None)
    address = ip.get("address") if isinstance(ip, dict) else getattr(ip, "address", None)
    return str(address).split("/")[0] if address else name


def _port(obj: Any, name: str, warnings: list[str]) -> int:
    fields = getattr(obj, "custom_fields", None) or {}
    value = fields.get(PORT_FIELD) if isinstance(fields, dict) else None
    if value in (None, ""):
        return 22
    try:
        return int(value)
    except (TypeError, ValueError):
        warnings.append(f"{name}: custom field {PORT_FIELD}={value!r} is not a number; using 22")
        return 22


def _to_device(obj: Any, warnings: list[str]) -> Device | None:
    name = getattr(obj, "name", None)
    if not name:
        warnings.append("skipped an object with no name")
        return None
    slug = _slug(getattr(obj, "platform", None))
    if not slug:
        warnings.append(f"skipped {name}: no platform set in NetBox")
        return None
    platform = PLATFORM_MAP.get(slug.lower())
    if platform is None:
        warnings.append(f"skipped {name}: platform '{slug}' is not supported "
                        f"(known: {', '.join(sorted(PLATFORM_MAP))})")
        return None
    # NetBox 4 calls the device role `role`; NetBox 3 called it `device_role`.
    role = _slug(getattr(obj, "role", None)) or _slug(getattr(obj, "device_role", None))
    return Device(name=str(name), host=_host(obj, str(name)), platform=platform,
                  role=role or "unknown", port=_port(obj, str(name), warnings))


def _error(exc: Exception, url: str, token: str) -> NetMedicError:
    status = getattr(getattr(exc, "req", None), "status_code", None)
    if status in (401, 403):
        message = f"NetBox at {url} rejected the token (HTTP {status})"
    elif status:
        message = f"NetBox API error at {url} (HTTP {status})"
    else:
        message = f"could not query NetBox at {url}: {type(exc).__name__}"
    if token:
        message = message.replace(token, "***")
    return NetMedicError(message)


def load_from_netbox(
    url: str, token: str, tag: str | None = "netmedic", api: Any = None
) -> Inventory:
    """Read active devices and virtual machines (optionally with `tag`) from NetBox."""
    nb = api if api is not None else _connect(url, token)
    query: dict[str, Any] = {"status": "active"}
    if tag:
        query["tag"] = tag
    warnings: list[str] = []
    devices: dict[str, Device] = {}
    try:
        objects = list(nb.dcim.devices.filter(**query))
        objects += list(nb.virtualization.virtual_machines.filter(**query))
    except NetMedicError:
        raise
    except Exception as exc:  # pynetbox RequestError/ContentError, requests errors, ...
        raise _error(exc, url, token) from None
    for obj in objects:
        device = _to_device(obj, warnings)
        if device is None:
            continue
        if device.name in devices:
            warnings.append(f"skipped duplicate name {device.name}")
            continue
        devices[device.name] = device
    if not devices:
        detail = f" tagged '{tag}'" if tag else ""
        raise NetMedicError(
            f"NetBox at {url} returned no usable active devices{detail}"
            + (f" ({len(warnings)} skipped: {'; '.join(warnings)})" if warnings else "")
        )
    return Inventory(devices, source=f"NetBox {url}", warnings=warnings)
