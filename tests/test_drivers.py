import pytest

from netmedic.drivers import ReplayDriver, SSHDriver, slugify
from netmedic.drivers.ssh import DEVICE_TYPES, srl_command, srl_restore_commands
from netmedic.errors import FixtureNotFoundError, NetMedicError
from netmedic.inventory import Device

R1 = Device("r1", "clab-netmedic-r1", "frr", "router")
R4 = Device("r4", "clab-netmedic-r4", "nokia_srlinux", "router")


@pytest.mark.parametrize(
    ("command", "slug"),
    [
        ("show ip route 192.168.3.0/24", "show_ip_route_192_168_3_0_24"),
        ("show interface brief", "show_interface_brief"),
        ("show running-config", "show_running_config"),
        ("  Show IP BGP Summary  ", "show_ip_bgp_summary"),
        ("ping 10.255.0.3", "ping_10_255_0_3"),
        ("info flat", "info_flat"),
        ("show interface ethernet-1/1", "show_interface_ethernet_1_1"),
        ("ping 10.255.0.1 network-instance default", "ping_10_255_0_1_network_instance_default"),
        ("show network-instance default route-table ipv4-unicast prefix 10.255.0.4/32",
         "show_network_instance_default_route_table_ipv4_unicast_prefix_10_255_0_4_32"),
    ],
)
def test_slugify(command, slug):
    assert slugify(command) == slug


@pytest.fixture
def fixtures(tmp_path):
    for scenario, text in [("healthy", "healthy routes"), ("interface_down", "broken routes")]:
        d = tmp_path / scenario / "r1"
        d.mkdir(parents=True)
        (d / "show_ip_route.txt").write_text(text)
    (tmp_path / "healthy" / "r1" / "show_interface_brief.txt").write_text("all up")
    return tmp_path


def test_scenario_file_wins(fixtures):
    assert ReplayDriver(fixtures, "interface_down").run(R1, "show ip route") == "broken routes"


def test_falls_back_to_healthy(fixtures):
    assert ReplayDriver(fixtures, "interface_down").run(R1, "show interface brief") == "all up"


def test_default_scenario_is_healthy(fixtures):
    assert ReplayDriver(fixtures).run(R1, "show ip route") == "healthy routes"


def test_missing_fixture_raises(fixtures):
    with pytest.raises(FixtureNotFoundError):
        ReplayDriver(fixtures, "interface_down").run(R1, "show ip ospf neighbor")


def test_configure_records_commands(fixtures):
    driver = ReplayDriver(fixtures)
    result = driver.configure(R1, ["interface eth2", "no shutdown"])
    assert driver.applied == [("r1", ["interface eth2", "no shutdown"])]
    assert "r1" in result


def test_restore_config_records_config(fixtures):
    driver = ReplayDriver(fixtures)
    driver.restore_config(R1, "hostname r1\n")
    assert driver.restored == [("r1", "hostname r1\n")]
    assert driver.applied == []


def test_ssh_restore_config_rejects_linux():
    host = Device("host1", "clab-netmedic-host1", "linux", "host")
    with pytest.raises(NetMedicError):
        SSHDriver("user", "pass").restore_config(host, "x")


def test_ssh_driver_constructs_without_network():
    driver = SSHDriver("user", "pass")
    assert driver.username == "user"


class FakeConn:
    def __init__(self):
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_command(self, command, **kwargs):
        self.calls.append(("send_command", command))
        return f"<{command}>"

    def send_config_set(self, commands, **kwargs):
        self.calls.append(("send_config_set", list(commands)))
        return "<config>"


@pytest.fixture
def fake_ssh(monkeypatch):
    conn = FakeConn()
    driver = SSHDriver("user", "pass")
    monkeypatch.setattr(driver, "_connect", lambda device: conn)
    return driver, conn


def test_srl_device_type():
    assert DEVICE_TYPES["nokia_srlinux"] == "nokia_srl"


def test_srl_ping_gets_a_count():
    assert srl_command("ping 10.255.0.1 network-instance default") == (
        "ping 10.255.0.1 network-instance default -c 3")
    assert srl_command("info flat") == "info flat"
    assert srl_command("traceroute 10.255.0.1 network-instance default").endswith("default")


def test_srl_run_sends_cli_command(fake_ssh):
    driver, conn = fake_ssh
    driver.run(R4, "show interface brief")
    driver.run(R4, "ping 10.255.0.1 network-instance default")
    assert conn.calls == [("send_command", "show interface brief"),
                          ("send_command", "ping 10.255.0.1 network-instance default -c 3")]


def test_srl_configure_commits(fake_ssh):
    driver, conn = fake_ssh
    driver.configure(R4, ["set / interface ethernet-1/1 admin-state enable"])
    assert conn.calls == [
        ("send_config_set", ["set / interface ethernet-1/1 admin-state enable"]),
        ("send_command", "commit now"),
    ]


def test_srl_restore_replaces_routing_trees(fake_ssh):
    driver, conn = fake_ssh
    config = "set / interface ethernet-1/1 admin-state enable\nset / system name host-name r4\n"
    driver.restore_config(R4, config)
    sent = conn.calls[0][1]
    assert sent[:3] == ["delete / interface", "delete / network-instance",
                        "delete / routing-policy"]
    assert sent[3:] == config.splitlines()
    assert conn.calls[-1] == ("send_command", "commit now")


def test_srl_restore_rejects_config_without_set_lines():
    with pytest.raises(NetMedicError):
        srl_restore_commands("hostname r1\n")
