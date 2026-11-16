"""NetBox inventory source, tested against a stub in place of pynetbox.api(...)."""

import sys
from types import SimpleNamespace

import pytest

from netmedic.errors import NetMedicError
from netmedic.inventory import load_inventory
from netmedic.netbox import PLATFORM_MAP, load_from_netbox

URL = "https://netbox.example.test"
TOKEN = "0123456789abcdef-secret-token"


def nb_device(name, platform="frr", role="router", ip="10.0.0.1/24", port=None,
              role_attr="role"):
    obj = SimpleNamespace(
        name=name,
        platform=SimpleNamespace(slug=platform) if platform else None,
        primary_ip4=SimpleNamespace(address=ip) if ip else None,
        custom_fields={"netmedic_port": port} if port is not None else {},
    )
    setattr(obj, role_attr, SimpleNamespace(slug=role) if role else None)
    return obj


class Endpoint:
    def __init__(self, objects=(), exc=None):
        self.objects, self.exc, self.queries = list(objects), exc, []

    def filter(self, **kwargs):
        self.queries.append(kwargs)
        if self.exc:
            raise self.exc
        return iter(self.objects)


def stub_api(devices=(), vms=(), exc=None) -> SimpleNamespace:
    return SimpleNamespace(dcim=SimpleNamespace(devices=Endpoint(devices, exc)),
                           virtualization=SimpleNamespace(virtual_machines=Endpoint(vms)))


@pytest.mark.parametrize(("slug", "platform"), sorted(PLATFORM_MAP.items()))
def test_every_platform_slug_maps(slug, platform):
    inv = load_from_netbox(URL, TOKEN, api=stub_api([nb_device("r1", platform=slug)]))
    assert inv.get("r1").platform == platform


def test_platform_table_covers_the_brief():
    assert PLATFORM_MAP == {
        "frr": "frr", "arista-eos": "arista_eos", "eos": "arista_eos",
        "nokia-srlinux": "nokia_srlinux", "srlinux": "nokia_srlinux",
        "sr-linux": "nokia_srlinux", "linux": "linux",
    }


def test_mapping_of_host_role_port_and_source():
    api = stub_api(
        [nb_device("r1", ip="10.0.0.1/24", port=2222),
         nb_device("r2", ip=None),
         nb_device("sw9", role="leaf", role_attr="device_role")],  # NetBox 3 field name
        vms=[nb_device("host1", platform="linux", role="host", ip="192.168.1.10/24")],
    )
    inv = load_from_netbox(URL, TOKEN, api=api)
    assert inv.get("r1").host == "10.0.0.1" and inv.get("r1").port == 2222
    assert inv.get("r2").host == "r2" and inv.get("r2").port == 22  # no primary IP
    assert inv.get("sw9").role == "leaf"  # unknown roles pass through
    assert inv.get("host1").role == "host" and inv.get("host1").platform == "linux"
    assert inv.links == [] and inv.warnings == []
    assert inv.source == f"NetBox {URL}" and TOKEN not in inv.source


def test_tag_filter_and_status():
    api = stub_api([nb_device("r1")])
    load_from_netbox(URL, TOKEN, api=api)
    assert api.dcim.devices.queries == [{"status": "active", "tag": "netmedic"}]
    assert api.virtualization.virtual_machines.queries == [{"status": "active", "tag": "netmedic"}]
    for empty in ("", None):
        api = stub_api([nb_device("r1")])
        load_from_netbox(URL, TOKEN, tag=empty, api=api)
        assert api.dcim.devices.queries == [{"status": "active"}]


def test_skipped_devices_become_warnings():
    api = stub_api([nb_device("r1"), nb_device("sw1", platform="cisco-ios"),
                    nb_device("x1", platform=None), nb_device("r1"),
                    nb_device("r5", port="ssh")])
    inv = load_from_netbox(URL, TOKEN, api=api)
    assert sorted(inv.devices) == ["r1", "r5"]
    text = " | ".join(inv.warnings)
    assert "sw1: platform 'cisco-ios' is not supported" in text
    assert "x1: no platform" in text
    assert "duplicate name r1" in text
    assert "r5: custom field netmedic_port='ssh'" in text and inv.get("r5").port == 22


def test_zero_usable_devices_is_an_error():
    with pytest.raises(NetMedicError, match="no usable active devices tagged 'netmedic'"):
        load_from_netbox(URL, TOKEN, api=stub_api([nb_device("sw1", platform="junos")]))
    with pytest.raises(NetMedicError, match="no usable active devices"):
        load_from_netbox(URL, TOKEN, api=stub_api())


class FakeRequestError(Exception):
    def __init__(self, status):
        super().__init__(f"The request failed with code {status}: token {TOKEN} invalid")
        self.req = SimpleNamespace(status_code=status, url=f"{URL}/api/dcim/devices/")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (FakeRequestError(403), f"NetBox at {URL} rejected the token (HTTP 403)"),
        (FakeRequestError(500), f"NetBox API error at {URL} (HTTP 500)"),
        (ConnectionError(f"cannot reach {URL} with Token {TOKEN}"),
         f"could not query NetBox at {URL}: ConnectionError"),
    ],
)
def test_query_errors_are_short_and_never_show_the_token(exc, expected):
    with pytest.raises(NetMedicError) as info:
        load_from_netbox(URL, TOKEN, api=stub_api(exc=exc))
    assert str(info.value) == expected and TOKEN not in str(info.value)
    assert info.value.__cause__ is None


def test_pynetbox_missing_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "pynetbox", None)  # makes `import pynetbox` fail
    with pytest.raises(NetMedicError, match=r'pip install -e "\.\[netbox\]"'):
        load_from_netbox(URL, TOKEN)


def test_real_pynetbox_api_object_is_built_without_network(monkeypatch):
    calls = []
    fake = SimpleNamespace(api=lambda url, token: calls.append((url, token)) or stub_api(
        [nb_device("r1")]))
    monkeypatch.setitem(sys.modules, "pynetbox", fake)
    assert "r1" in load_from_netbox(URL, TOKEN).devices
    assert calls == [(URL, TOKEN)]


# --- selection through NETMEDIC_INVENTORY ---------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    for var in ("NETMEDIC_INVENTORY", "NETBOX_URL", "NETBOX_TOKEN", "NETBOX_TAG"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.mark.parametrize(("missing", "env"), [
    ("NETBOX_URL", {"NETBOX_TOKEN": TOKEN}),
    ("NETBOX_TOKEN", {"NETBOX_URL": URL}),
])
def test_netbox_selection_needs_url_and_token(clean_env, tmp_path, missing, env):
    clean_env.setenv("NETMEDIC_INVENTORY", "netbox")
    for key, value in env.items():
        clean_env.setenv(key, value)
    with pytest.raises(NetMedicError) as info:
        load_inventory(tmp_path)
    assert missing in str(info.value) and TOKEN not in str(info.value)


@pytest.mark.parametrize(("tag_env", "expected_tag"), [(None, "netmedic"), ("", ""),
                                                       ("lab", "lab")])
def test_netbox_selection_passes_tag(clean_env, tmp_path, tag_env, expected_tag):
    seen = {}

    def fake_load(url, token, tag="netmedic", api=None):
        seen.update(url=url, token=token, tag=tag)
        return "inventory"

    clean_env.setattr("netmedic.netbox.load_from_netbox", fake_load)
    clean_env.setenv("NETMEDIC_INVENTORY", "NetBox")
    clean_env.setenv("NETBOX_URL", URL)
    clean_env.setenv("NETBOX_TOKEN", TOKEN)
    if tag_env is not None:
        clean_env.setenv("NETBOX_TAG", tag_env)
    assert load_inventory(tmp_path) == "inventory"
    assert seen == {"url": URL, "token": TOKEN, "tag": expected_tag}
