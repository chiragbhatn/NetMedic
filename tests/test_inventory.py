from pathlib import Path

import pytest

from netmedic.errors import NetMedicError, UnknownDeviceError
from netmedic.inventory import Device, Inventory, load_inventory

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
    # the original five; more devices (r4, host3) are being added to the lab
    assert platforms.items() >= {
        "r1": "frr", "r2": "frr", "r3": "arista_eos", "host1": "linux", "host2": "linux",
    }.items()
    assert inv.get("r1").host == "clab-netmedic-r1"
    assert inv.get("host1").role == "host"


def test_links(tmp_path):
    path = tmp_path / "inv.yaml"
    path.write_text(
        "devices:\n"
        "  r1: {host: h1, platform: frr, role: router}\n"
        "  r2: {host: h2, platform: frr, role: router}\n"
        "  host1: {host: h3, platform: linux, role: host}\n"
        "links:\n"
        "  - [host1, r1]\n"
        "  - [r1, r2]\n"
        "  - [r2, r9]\n"        # unknown device: ignored
        "  - [r1]\n"            # malformed: ignored
        "  - not-a-pair\n"
    )
    inv = Inventory.load(path)
    assert inv.links == [("host1", "r1"), ("r1", "r2")]
    assert len(inv.warnings) == 3 and "r9" in inv.warnings[0]
    assert inv.source == str(path)


def test_links_default_empty(tmp_path):
    path = tmp_path / "inv.yaml"
    path.write_text("devices:\n  r1: {host: h, platform: frr, role: router}\n")
    inv = Inventory.load(path)
    assert inv.links == [] and inv.warnings == []
    assert Inventory({}).links == []


@pytest.fixture
def no_inventory_env(monkeypatch):
    for var in ("NETMEDIC_INVENTORY", "NETBOX_URL", "NETBOX_TOKEN", "NETBOX_TAG"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_load_inventory_yaml_default_and_override(no_inventory_env, tmp_path):
    (tmp_path / "inventory.yaml").write_text(
        "devices:\n  r1: {host: h, platform: frr, role: router}\n")
    (tmp_path / "other.yaml").write_text(
        "devices:\n  r7: {host: h, platform: frr, role: router}\n")
    assert list(load_inventory(tmp_path).devices) == ["r1"]
    no_inventory_env.setenv("NETMEDIC_INVENTORY", "other.yaml")
    assert list(load_inventory(tmp_path).devices) == ["r7"]
    no_inventory_env.setenv("NETMEDIC_INVENTORY", str(tmp_path / "other.yaml"))
    assert list(load_inventory(Path("/elsewhere")).devices) == ["r7"]


def test_load_inventory_missing_file(no_inventory_env, tmp_path):
    with pytest.raises(NetMedicError, match="inventory file not found"):
        load_inventory(tmp_path)
