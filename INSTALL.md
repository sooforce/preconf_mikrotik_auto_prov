# MikroTik Pre-Configuration Auto Provisioner — Installation Guide

This tool automates MikroTik CPE provisioning for `Inet` service using NetBox, MikroTik SSH, and DHCP static reservation files.

It performs the following actions:

- Finds the MikroTik management IP from DHCP by MAC address, or uses a provided management IP.
- Connects to the MikroTik over SSH.
- Enables and configures `pppoe-out-Internet`.
- Sets the MikroTik identity to `NAME-SLA-Mtik`.
- Finds the customer Inet prefix in NetBox.
- Updates the NetBox Inet prefix description to `NAME-SLA-Inet`.
- Adds the public customer IP to `bridge-Internet`.
- Adds or verifies the MikroTik static management DHCP reservation.
- Syncs DHCP configuration to the DHCP servers.
- Creates or updates the MikroTik management `/32` record in NetBox IPAM.
- Prints a final customer summary.

---

## 1. Requirements

Install these before using the tool:

- Python 3.10 or newer
- Git
- Network reachability to:
  - NetBox
  - MikroTik management network
  - DHCP server over SSH
- MikroTik SSH access
- NetBox API token
- DHCP server SSH credentials

Python packages are installed from `requirements.txt`.

---

## 2. Clone the repository

Open **Command Prompt** on Windows or a terminal on Linux/macOS.

```bash
git clone https://github.com/sooforce/preconf_mikrotik_auto_prov.git
cd preconf_mikrotik_auto_prov
```

---

## 3. Create a Python virtual environment

### Windows CMD

```cmd
py -m venv venv
venv\Scripts\activate
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
```

### Linux/macOS

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

---

## 4. Configure `config.yaml`

Create or edit `config.yaml` in the repository directory.

Example:

```yaml
netbox_url: "http://netbox.cablenet-as.net:8000"
netbox_auth_scheme: "Bearer"

mtik_username: "admin"
mtik_port: 22
mtik_timeout: 20

internet_interface: "bridge-Internet"
pppoe_interface: "pppoe-out-Internet"
dhcp_client_find: "[find disabled=no]"

log_dir: "./logs"

ping_count: 3
ping_timeout_seconds: 2
post_renew_wait_seconds: 10
reboot_wait_seconds: 90

dhcp_remote_enabled: true
dhcp_root: "/etc/dhcp"
dhcp_static_filename: "Mikrotik-Static"
dhcp_leases_files:
  - "/var/lib/dhcp/dhcpd.leases"
  - "/var/log/syslog"

dhcp_sync_workdir: "/etc/dhcp"
dhcp_sync_command:
  - "./dhcp-sync.sh"
  - "apply"
dhcp_validate_command:
  - "dhcpd"
  - "-t"
  - "-cf"
  - "/etc/dhcp/dhcpd.conf"

dhcp_ssh_username: "root"
dhcp_ssh_port: 22
dhcp_ssh_timeout: 20
```

Do **not** hardcode passwords or tokens inside `config.yaml` unless this is only for a secure local test environment.

---

## 5. Set environment variables

Set secrets using environment variables.

### Windows CMD

```cmd
set NETBOX_URL=http://netbox.cablenet-as.net:8000
set NETBOX_AUTH_SCHEME=Bearer
set NETBOX_TOKEN=YOUR_NETBOX_TOKEN
set MTIK_PASSWORD=YOUR_MIKROTIK_PASSWORD

set DHCP_REMOTE_ENABLED=true
set DHCP_SSH_HOST=172.23.16.30
set DHCP_SSH_USERNAME=root
set DHCP_SSH_PASSWORD=YOUR_DHCP_PASSWORD
```

Optional, if the DHCP sync script is not under `/etc/dhcp/dhcp-sync.sh`:

```cmd
set DHCP_SYNC_COMMAND=/root/dhcp-sync.sh apply
```

Optional, if the MikroTik public bridge name is different:

```cmd
set INTERNET_INTERFACE=bridge-Internet
```

### Linux/macOS

```bash
export NETBOX_URL="http://netbox.cablenet-as.net:8000"
export NETBOX_AUTH_SCHEME="Bearer"
export NETBOX_TOKEN="YOUR_NETBOX_TOKEN"
export MTIK_PASSWORD="YOUR_MIKROTIK_PASSWORD"

export DHCP_REMOTE_ENABLED="true"
export DHCP_SSH_HOST="172.23.16.30"
export DHCP_SSH_USERNAME="root"
export DHCP_SSH_PASSWORD="YOUR_DHCP_PASSWORD"
```

Optional:

```bash
export DHCP_SYNC_COMMAND="/root/dhcp-sync.sh apply"
export INTERNET_INTERFACE="bridge-Internet"
```

---

## 6. Test NetBox connectivity

Run:

```cmd
python provision_inet.py --check-netbox
```

Expected result:

```text
NETBOX OK. Log file: logs\provision-inet-netbox-check-YYYYMMDD-HHMMSS.log
```

---

## 7. Dry-run provisioning

Dry-run is the default mode. It shows what the script will do without applying changes.

Example using MAC address:

```cmd
python provision_inet.py --mac d0:ea:11:41:98:6f --sla 20677566 --service Inet --name BOC
```

Example using a known management IP:

```cmd
python provision_inet.py --mgmt-ip 10.205.255.82 --sla 20677566 --service Inet --name BOC
```

Optional port label for the final summary:

```cmd
python provision_inet.py --mac d0:ea:11:41:98:6f --sla 20677566 --service Inet --name BOC --port 1
```

Review the log carefully before applying.

---

## 8. Apply provisioning

Only run `--apply` after the dry-run output is correct.

```cmd
python provision_inet.py --mac d0:ea:11:41:98:6f --sla 20677566 --service Inet --name BOC --port 1 --apply
```

Expected final output:

```text
Done.

Port 1 --> BOC-20677566-Inet --> Subnet: 212.50.106.240/30
BOC-20677566-Mtik ---> 10.205.255.82  SN: HM60BAAJRVS

SUCCESS. Log file: logs\provision-inet-20677566-YYYYMMDD-HHMMSS.log
```

---

## 9. What the script changes

For an example command:

```cmd
python provision_inet.py --mac d0:ea:11:41:98:6f --sla 20677566 --service Inet --name BOC --apply
```

The script configures:

### MikroTik

```text
PPPoE interface: pppoe-out-Internet
PPPoE username: 20677566-Inet
PPPoE password: 20677566-Inet
Identity: BOC-20677566-Mtik
Public IP: first usable IP from the NetBox /30 prefix
Interface: bridge-Internet
```

### NetBox

```text
Inet prefix description: BOC-20677566-Inet
MikroTik management IP object: 10.x.x.x/32
MikroTik management IP description: BOC-20677566-Mtik
```

### DHCP

```text
host <ROUTERBOARD_SERIAL> { hardware ethernet <MAC>; fixed-address <STATIC_MGMT_IP>; }
```

A backup of the DHCP static file is created before changes are written.

---

## 10. Logs

Every run creates a log file under `logs/`.

Example:

```text
logs/provision-inet-20677566-20260611-122144.log
```

Use the log file to confirm:

- Current management IP
- MikroTik SSH connection
- RouterBOARD serial number
- NetBox selected prefix
- MikroTik public IP
- DHCP static reservation
- DHCP validation
- DHCP sync
- Final verification

---

## 11. Troubleshooting

### NetBox authentication fails

Check:

```cmd
set NETBOX_TOKEN
set NETBOX_AUTH_SCHEME
set NETBOX_URL
```

Most deployments use either:

```text
Bearer <token>
```

or:

```text
Token <token>
```

Set the correct one with:

```cmd
set NETBOX_AUTH_SCHEME=Bearer
```

or:

```cmd
set NETBOX_AUTH_SCHEME=Token
```

---

### MikroTik public IP fails with interface error

Error example:

```text
input does not match any value of interface
```

The configured interface name is wrong. Check the MikroTik interface name and set:

```cmd
set INTERNET_INTERFACE=bridge-Internet
```

or edit `config.yaml`:

```yaml
internet_interface: "bridge-Internet"
```

---

### DHCP sync script not found

Find the script on the DHCP server:

```bash
find / -name dhcp-sync.sh -type f 2>/dev/null
```

Then set:

```cmd
set DHCP_SYNC_COMMAND=/root/dhcp-sync.sh apply
```

---

### MikroTik SSH disconnects during DHCP renew

This can be normal. When the MikroTik management IP changes, the old SSH session may drop.

The script waits, pings the new static IP, and reconnects for final verification.

---

### Duplicate public IP on MikroTik

The script checks if the public IP already exists before adding it. If an old duplicate exists manually, remove the incorrect one from MikroTik before rerunning.

---

## 12. Safety notes

- Always run without `--apply` first.
- Keep NetBox tokens and passwords out of Git.
- Use environment variables for secrets.
- Rotate any token or password that was accidentally committed.
- Review the log file after each provisioning run.
- Confirm the final summary before handing the CPE to the installer/customer.

---

## 13. Common commands

Check NetBox:

```cmd
python provision_inet.py --check-netbox
```

Dry-run by MAC:

```cmd
python provision_inet.py --mac aa:bb:cc:dd:ee:ff --sla 20677566 --service Inet --name BOC --port 1
```

Apply by MAC:

```cmd
python provision_inet.py --mac aa:bb:cc:dd:ee:ff --sla 20677566 --service Inet --name BOC --port 1 --apply
```

Dry-run by management IP:

```cmd
python provision_inet.py --mgmt-ip 10.205.255.82 --sla 20677566 --service Inet --name BOC --port 1
```

Apply by management IP:

```cmd
python provision_inet.py --mgmt-ip 10.205.255.82 --sla 20677566 --service Inet --name BOC --port 1 --apply
```
