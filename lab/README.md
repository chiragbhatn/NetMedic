# NetMedic lab setup (Windows 11)

> **Status: NOT YET VERIFIED ON A LIVE LAB.** This guide, `topology.clab.yml` and the
> golden configs were written without access to Docker or containerlab. Expect to fix
> small things on first deploy, then re-record the fixtures (`netmedic record`).

```
host1 --- r1 (FRR) --- r2 (FRR) --- r3 (cEOS) --- host2
                        |
                       r4 (SR Linux) --- host3
```

| Node | Image | Mgmt IP | Config |
|---|---|---|---|
| r1 | `quay.io/frrouting/frr:10.2.1` | 172.20.20.11 | `golden/r1.cfg` (bound to `/etc/frr/frr.conf`) |
| r2 | `quay.io/frrouting/frr:10.2.1` | 172.20.20.12 | `golden/r2.cfg` |
| r3 | `ceos:4.32.0F` (you import it) | 172.20.20.13 | `golden/r3.cfg` (startup-config) |
| r4 | `ghcr.io/nokia/srlinux:24.10.1` | 172.20.20.14 | `golden/r4.cfg` (startup-config, CLI `set` lines) |
| host1 | `ghcr.io/hellt/network-multitool` | 172.20.20.21 | exec steps in the topology |
| host2 | `ghcr.io/hellt/network-multitool` | 172.20.20.22 | exec steps in the topology |
| host3 | `ghcr.io/hellt/network-multitool` | 172.20.20.23 | exec steps in the topology |

Containerlab also adds `/etc/hosts` entries such as `clab-netmedic-r1`, which
`inventory.yaml` uses.

## 1. WSL2 and Ubuntu

In an elevated PowerShell:

```powershell
wsl --install -d Ubuntu
```

Reboot if asked, open "Ubuntu" from the Start menu and create your Linux user. Do
everything below **inside the Ubuntu shell**.

## 2. Docker Engine inside WSL

Install Docker Engine in Ubuntu (not Docker Desktop, which also works but is not
covered here):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Close and reopen the Ubuntu shell, then check with `docker run --rm hello-world`. If
the Docker service is not running, enable systemd in WSL (`/etc/wsl.conf`:
`[boot]` / `systemd=true`, then `wsl --shutdown` from PowerShell) or start it with
`sudo service docker start`.

## 3. containerlab

```bash
bash -c "$(curl -sL https://get.containerlab.dev)"
containerlab version
```

## 4. Arista cEOS image

1. Create a free account at arista.com and download **cEOS-lab** 4.32.0F (64-bit),
   file `cEOS64-lab-4.32.0F.tar.xz` (Software Downloads > EOS > cEOS-lab).
2. Copy it into WSL (Windows drives are under `/mnt/c/...`) and import it:

```bash
docker import cEOS64-lab-4.32.0F.tar.xz ceos:4.32.0F
docker images | grep ceos
```

Another 4.3x version works too if you tag it `ceos:4.32.0F` or change the image in
`topology.clab.yml`.

## 5. Nokia SR Linux image

SR Linux is free and needs no account. Pull it before deploying (containerlab would
also pull it on first deploy):

```bash
docker pull ghcr.io/nokia/srlinux:24.10.1
```

## 6. Deploy

Clone the repo inside the WSL filesystem (for example `~/netmedic`, not `/mnt/c`),
then:

```bash
cd ~/netmedic/lab
sudo containerlab deploy -t topology.clab.yml
```

cEOS and SR Linux take 1 to 3 minutes to boot. The FRR and host exec steps run `apk add`, so the
machine needs internet access during deploy.

## 7. Check it works

```bash
# OSPF and BGP on r2
docker exec clab-netmedic-r2 vtysh -c "show ip ospf neighbor" -c "show ip bgp summary"
# end-to-end
docker exec clab-netmedic-host1 ping -c 3 192.168.3.10
docker exec clab-netmedic-host1 traceroute 192.168.3.10
# cEOS
docker exec -it clab-netmedic-r3 Cli -c "show ip bgp summary"
# SR Linux
docker exec -it clab-netmedic-r4 sr_cli "show network-instance default protocols bgp neighbor"
docker exec clab-netmedic-host3 ping -c 3 192.168.1.10
```

Expected: r2 has three OSPF neighbours in `Full` and three BGP peers with 1 prefix
each; host1 reaches host2 in 4 hops (192.168.1.1, 10.0.12.2, 10.0.23.2, 192.168.3.10);
host3 reaches host1 in 4 hops (192.168.4.1, 10.0.24.1, 10.0.12.1, 192.168.1.10).

## 8. SSH and credentials

NetMedic connects over SSH with credentials from environment variables. The lab uses
the same lab-only credentials on every node:

```bash
export NETMEDIC_SSH_USERNAME=admin
export NETMEDIC_SSH_PASSWORD=admin
```

(or put them in `netmedic/.env`). Run `netmedic --live` **from inside WSL**: the
`172.20.20.0/24` management network and the `clab-netmedic-*` names exist only there,
not on the Windows side.

- **r3 (cEOS)** has SSH built in. `golden/r3.cfg` defines user `admin` (SHA-512 hash of
  `admin`) and enables SSH in the `MGMT` VRF where Management0 lives.
- **r1, r2 (FRR)**: the FRR image does **not** run an SSH server. The topology's
  `exec` steps install `openssh-server` with `apk`, create user `admin` (password
  `admin`, member of `frrvty` so it can run `vtysh`) and start `sshd`. NetMedic then
  runs `vtysh -c "<command>"` in that shell. *Untested.*
- **r4 (SR Linux)** has SSH built in (in the `mgmt` network-instance). Its factory
  credentials are `admin` / `NokiaSrl1!`; because NetMedic uses one username/password
  pair for every device, `golden/r4.cfg` sets the admin password to `admin`
  (`system aaa authentication admin-user password <SHA-512 hash>`). NetMedic logs in to
  the SR Linux CLI with Netmiko's `nokia_srl` driver. *Untested*: if SR Linux rejects
  that hash format, the lab still answers with `admin` / `NokiaSrl1!`; set the password
  by hand (`enter candidate`, `set / system aaa authentication admin-user password
  admin`, `commit now`) and copy the hash from `info flat system aaa` into the golden.
- **host1, host2, host3**: same `exec` approach (`network-multitool` has an SSH client but no
  server). host2's `sshd` is also what the `acl_block` scenario blocks. *Untested.*

To use another password, change the `chpasswd` lines in the topology and replace the
`username admin ... secret` line in `golden/r3.cfg` and the `admin-user password` line in
`golden/r4.cfg` (configure each device, then copy the hash it displays).

## 9. Destroy

```bash
sudo containerlab destroy -t topology.clab.yml --cleanup
```

## Notes and known risks

- Routers delete the Docker default route (`ip route del default`) after setup, so a
  missing route gives "Network unreachable" instead of traffic leaking out via the
  management network. r3 keeps management in VRF `MGMT` for the same reason.
- All links use MTU 1500 so OSPF between FRR, cEOS and SR Linux reaches `Full`.
- `golden/r4.cfg` is in `info flat` form (one `set / ...` line per leaf), which is
  also SR Linux's CLI input format. containerlab may only treat a startup-config as CLI
  commands when the file ends in `.cli`; if deploy fails to apply it, copy it to
  `golden/r4.cli` and point `startup-config` at that copy. *Untested.*
- The golden `info flat` for r4 is trimmed: a real `info flat` also prints the
  containerlab defaults (TLS server profile with a per-deploy key, gNMI/JSON-RPC, the
  default CPM filter ACL), runs to several hundred lines and would exceed NetMedic's
  8000-character tool output limit. After the first live deploy, re-capture
  `golden/r4.cfg` from the device; until then `diff_against_golden` on a live r4 will
  show these extra lines.
- `golden/*.cfg` are bind-mounted read-write; `write memory` on r1/r2 overwrites them.
- The golden files are also the baseline for `diff_against_golden`. They match what
  the devices are expected to print in `show running-config` (FRR without its
  `Building configuration...` preamble), so some lines may need adjusting after the
  first real capture.
- cEOS-lab may not enforce every ACL in the data plane. If the `acl_block` scenario
  does not stop SSH on a live lab, apply the ACL `in` on Ethernet1 instead and update
  `evals/scenarios/acl_block.yaml`.
- Fault injection and restore commands per scenario are in `evals/scenarios/*.yaml`.
