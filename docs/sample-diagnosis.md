# Sample diagnosis

Real output of `netmedic diagnose "host1 cannot reach host2" --scenario interface_down`
with `claude-haiku-5-5`, recorded 2026-11-16 in replay mode (synthetic fixtures).
Only terminal line-wrapping has been undone; the content is unchanged.

```text
LLM: anthropic / claude-haiku-5-5
 1 list_devices {} -> 370 chars
 1 traceroute {"device": "host1", "target": "192.168.3.10"} -> 141 chars
 2 run_show {"device": "host2", "command": "ip addr"} -> 770 chars
 2 run_show {"device": "host1", "command": "ip addr"} -> 770 chars
 3 get_routes {"device": "r1"} -> 985 chars
 3 get_neighbors {"device": "r1", "protocol": "ospf"} -> 260 chars
 3 get_interfaces {"device": "r1"} -> 319 chars
 4 get_routes {"device": "r2"} -> 937 chars
 4 get_neighbors {"device": "r2", "protocol": "ospf"} -> 260 chars
 4 get_interfaces {"device": "r2"} -> 317 chars
 4 diff_against_golden {"device": "r2"} -> 186 chars
 5 propose_change {"device": "r2", "commands": ["interface eth2", "no shutdown"], "reason": "r2 eth2 (link to r3, 10.0.23.1/30) was administratively shut down; golden config has no shutdown. Removing it restores the r2... -> 114 chars
 5 submit_report fault_type=interface_down device=r2 -> Report accepted.
────────────────────────────────── Diagnosis ──────────────────────────────────
Root cause: r2's eth2 interface (link to r3, 10.0.23.1/30) is administratively shut down. The running config has a "shutdown" line that is absent from the golden config, so r2 never learns the 192.168.3.0/24 route and r1 returns network-unreachable for host2.
Device: r2    Fault type: interface_down    Confidence: high
Evidence:
  - host1 traceroute to 192.168.3.10: 1  192.168.1.1 (192.168.1.1)  0.071 ms !N  0.092 ms !N  0.116 ms !N
  - host2 eth1: inet 192.168.3.10/24 (target address confirmed)
  - r1 get_routes: no route for 192.168.3.0/24 (only 10.0.12.0/30, 10.255.0.1/32, 10.255.0.2/32, 172.20.20.0/24, 192.168.1.0/24)
  - r2 get_interfaces: eth2  down  default  10.0.23.1/30
  - r2 diff_against_golden: +  shutdown  (under interface eth2, description to-r3, ip address 10.0.23.1/30)
  - r2 OSPF neighbor with r1 (10.255.0.1) is Full/DROther, so the r1-r2 core is healthy
Proposed fix:
  interface eth2
  no shutdown
5 steps, 21360 input (14809 cached) / 2110 output tokens
```
