from pathlib import Path

import pytest

from netmedic.errors import UnknownDeviceError
from netmedic.inventory import Device, Inventory

PROJECT_INVENTORY = Path(__file__).resolve().parents[1] / "inventory.yaml"


def test_load_and_get(tmp_path):
    path = tmp_path / "inv.yaml"
    path.write_text(
        "devices:\n"
        "  r1: {host: 10.0.0.1, platform: frr, role: router}\n"
        "  r3: {host: 10.0.0.3, platform: arista_eos, role: router, port: 2222}\n"
    )
    inv = Inventory.load(path)
    assert inv.get("r1") == Device("r1", "10.0.0.1", "frr", "router", 22)
    assert inv.get("r3").port == 2222


def test_unknown_device_raises(tmp_path):
    path = tmp_path / "inv.yaml"
    path.write_text("devices:\n  r1: {host: h, platform: frr, role: router}\n")
    with pytest.raises(UnknownDeviceError, match="r9"):
        Inventory.load(path).get("r9")


def test_project_inventory_matches_spec():
    inv = Inventory.load(PROJECT_INVENTORY)
    platforms = {name: d.platform for name, d in inv.devices.items()}
    assert platforms == {
        "r1": "frr", "r2": "frr", "r3": "arista_eos", "host1": "linux", "host2": "linux",
    }
    assert inv.get("r1").host == "clab-netmedic-r1"
    assert inv.get("host1").role == "host"
