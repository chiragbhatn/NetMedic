# NetBox for the NetMedic lab

> **Status: UNVERIFIED.** This guide and `seed.py` were written without a NetBox
> instance. NetMedic's NetBox loader is tested only against a stub. Expect to adjust
> small things on first use.

NetMedic can read its devices from NetBox instead of `inventory.yaml`. This guide runs
NetBox locally with the official [netbox-docker](https://github.com/netbox-community/netbox-docker)
project and loads the lab devices into it.

## 1. Run NetBox

Needs Docker with Compose (on Windows, inside WSL2 or Docker Desktop):

```bash
git clone -b release https://github.com/netbox-community/netbox-docker.git
cd netbox-docker
cat > docker-compose.override.yml <<'EOF'
services:
  netbox:
    ports:
      - "8000:8080"
EOF
docker compose pull
docker compose up -d
docker compose exec netbox /opt/netbox/netbox/manage.py createsuperuser
```

NetBox is then at http://localhost:8000. Startup takes a few minutes the first time.

## 2. Create an API token

Log in, open your user menu, then API Tokens, and add a token with write access (the
seed script creates objects). Treat it like a password.

## 3. Seed the lab devices

From the NetMedic project root, with the virtualenv active:

```bash
pip install -e ".[netbox]"
export NETBOX_URL=http://localhost:8000
export NETBOX_TOKEN=<your token>
python lab/netbox/seed.py
```

(PowerShell: `$env:NETBOX_URL="http://localhost:8000"`, `$env:NETBOX_TOKEN="..."`.)

The script reads the devices from `inventory.yaml` and their management IPs from
`lab/topology.clab.yml`, so devices added there are picked up on the next run. It is
idempotent: it looks up each object first and only creates what is missing. It
creates:

- the site `netmedic-lab` and the tag `netmedic`
- a manufacturer, device type and platform per NetMedic platform; the platform slugs
  are `frr`, `arista-eos`, `nokia-srlinux` and `linux`
- the device roles `router` and `host`
- one active device per inventory entry, tagged `netmedic`, with a `mgmt0` interface
  holding its management IPv4 as the primary IP

It targets NetBox 4.x, where a device's role field is `role`.

## 4. Point NetMedic at NetBox

In `.env`:

```
NETMEDIC_INVENTORY=netbox
NETBOX_URL=http://localhost:8000
NETBOX_TOKEN=<your token>
# NETBOX_TAG=netmedic   (default; set it empty to read every active device)
```

Then check what NetMedic sees, without calling the LLM:

```bash
netmedic inventory
```

Device names must match the fixture directory names (`fixtures/<scenario>/<name>/`)
for replay mode to work; NetMedic prints a warning for any NetBox device without
fixtures. Cables are not imported, so the inventory's `links` stay empty with NetBox.
