import pytest

from netmedic.drivers import ReplayDriver, SSHDriver, slugify
from netmedic.errors import FixtureNotFoundError, NetMedicError
from netmedic.inventory import Device

R1 = Device("r1", "clab-netmedic-r1", "frr", "router")


@pytest.mark.parametrize(
    ("command", "slug"),
    [
        ("show ip route 192.168.3.0/24", "show_ip_route_192_168_3_0_24"),
        ("show interface brief", "show_interface_brief"),
        ("show running-config", "show_running_config"),
        ("  Show IP BGP Summary  ", "show_ip_bgp_summary"),
        ("ping 10.255.0.3", "ping_10_255_0_3"),
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
