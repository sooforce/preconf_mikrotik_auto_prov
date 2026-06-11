# MikroTik Inet Provisioner v5

This version supports the same Inet workflow as v4 plus optional **remote DHCP mode**.

Remote DHCP mode lets you run the script from Windows or another workstation while the script reads/edits `/etc/dhcp`, validates DHCP, and runs `dhcp-sync.sh apply` over SSH on the DHCP server.

## Install on Windows

```cmd
py -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
```

## Environment variables

Do not hardcode passwords/tokens in the script.

```cmd
set NETBOX_URL=http://netbox.cablenet-as.net:8000
set NETBOX_AUTH_SCHEME=Bearer
set NETBOX_TOKEN=YOUR_NETBOX_TOKEN
set MTIK_PASSWORD=YOUR_MIKROTIK_PASSWORD

set DHCP_REMOTE_ENABLED=true
set DHCP_SSH_HOST=172.23.16.30
set DHCP_SSH_USERNAME=root
set DHCP_SSH_PASSWORD=YOUR_DHCP_SSH_PASSWORD
```

PowerShell equivalent:

```powershell
$env:NETBOX_URL="http://netbox.cablenet-as.net:8000"
$env:NETBOX_AUTH_SCHEME="Bearer"
$env:NETBOX_TOKEN="YOUR_NETBOX_TOKEN"
$env:MTIK_PASSWORD="YOUR_MIKROTIK_PASSWORD"

$env:DHCP_REMOTE_ENABLED="true"
$env:DHCP_SSH_HOST="172.23.16.30"
$env:DHCP_SSH_USERNAME="root"
$env:DHCP_SSH_PASSWORD="YOUR_DHCP_SSH_PASSWORD"
```

## Test NetBox only

```cmd
python provision_inet.py --check-netbox
```

## Dry-run with remote DHCP enabled

```cmd
python provision_inet.py --mgmt-ip 10.205.255.78 --mac REAL_MTIK_MAC --sla 20675166 --service Inet --name TESTCUSTOMER
```

Dry-run still reads DHCP files remotely to identify the correct DHCP static file and next available IP, but it does not write changes or run DHCP sync.

## Apply for real

Use only after checking the dry-run log.

```cmd
python provision_inet.py --mgmt-ip 10.205.255.78 --mac REAL_MTIK_MAC --sla 20675166 --service Inet --name TESTCUSTOMER --apply
```

This actually changes MikroTik, updates NetBox, writes the remote DHCP reservation, validates DHCP, runs `dhcp-sync.sh apply`, releases/renews DHCP client on the MikroTik, and verifies reachability.

## Safety notes

- Dry-run is default. `--apply` is required for real changes.
- Use the real MikroTik MAC before applying.
- If a real password/token was pasted in chat or shared accidentally, rotate it.
- Long term, replace password SSH with SSH keys and disable root password login where possible.
