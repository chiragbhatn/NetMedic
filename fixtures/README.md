# Fixtures

> **These fixtures are SYNTHETIC.** They were written by hand (with a small script),
> not captured from devices, because the lab had not been deployed when they were made.
> The output formats imitate FRR 10.2 (vtysh, busybox `ping`/`traceroute`), Arista
> EOS 4.32, Nokia SR Linux 24.10 and Linux (iproute2, iputils, traceroute), but details
> can differ from the real devices. Replace them with real captures from the live lab
> using `netmedic record --scenario <id>`, and then remove this notice.

## Layout

```
fixtures/<scenario>/<device>/<slug>.txt
```

- `<scenario>`: a scenario id from `evals/scenarios/` (`healthy`, `interface_down`,
  `ospf_area_mismatch`, `bgp_wrong_as`, `acl_block`, `injection`, `srl_interface_down`).
- `<device>`: `r1`, `r2` (FRR), `r3` (Arista EOS), `r4` (Nokia SR Linux), `host1`,
  `host2`, `host3` (Linux).
- `<slug>`: the logical command lowercased, every run of characters outside `[a-z0-9]`
  replaced by `_`, leading and trailing `_` stripped.
  `show ip route 192.168.3.0/24` becomes `show_ip_route_192_168_3_0_24.txt`;
  `ping 10.255.0.1 network-instance default` becomes
  `ping_10_255_0_1_network_instance_default.txt`.

The logical commands per platform are defined in `src/netmedic/platforms.py`.
Files hold the command output only (no prompt, no echoed command).

## Lookup rule

The replay driver looks in `fixtures/<scenario>/<device>/` first, then in
`fixtures/healthy/<device>/`. If neither has the file it raises
`FixtureNotFoundError`.

- `healthy/` is complete: every logical command for each device's platform, including
  the one-interface command for every interface, the route lookup for
  192.168.1.0/24, 192.168.3.0/24, 192.168.4.0/24 and the four loopbacks
  (10.255.0.1-4/32), and ping/traceroute to 192.168.1.10, 192.168.3.10, 192.168.4.10
  and 10.255.0.1-4 from all seven devices.
- A fault scenario contains **only the files whose output differs from healthy**.

## Capture conventions in the synthetic data

- FRR, SR Linux and Linux pings are 3 packets (the SSH driver adds `-c 3`); EOS pings
  use the EOS default of 5.
- The snapshot is taken about 2h15m after the lab came up; each fault was injected
  about 4m25s before the capture, so session and interface timers show that.
- The running-config fixture for each router matches `lab/golden/<router>.cfg` in
  `healthy` (FRR adds its `Building configuration...` preamble). For r4 that is
  `info flat`, trimmed of containerlab's system defaults (see `lab/README.md`).
