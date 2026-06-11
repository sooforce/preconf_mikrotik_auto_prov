#!/usr/bin/env python3
"""
MikroTik Inet provisioning automation. v15 NetBox management IP record.

Inputs:
  --mac or --mgmt-ip
  --sla 8-digit number
  --service Inet only in this first version
  --name customer/site name

Safe behavior:
  * dry-run by default
  * detailed logs to file and console
  * DHCP static file backup before editing
  * DHCP config validation before sync command
  * NetBox token and MikroTik password are read from env/config, not hardcoded
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import ipaddress
import logging
import os
import platform
import re
import shutil
import socket
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import paramiko
import requests
import yaml

MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
HOST_BLOCK_RE = re.compile(
    r"host\s+(?P<host>\S+)\s*\{\s*hardware\s+ethernet\s+(?P<mac>[0-9A-Fa-f:]{17});\s*fixed-address\s+(?P<ip>[0-9.]+);\s*\}",
    re.IGNORECASE | re.MULTILINE,
)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass
class AppConfig:
    dhcp_root: str  # POSIX-style path for remote DHCP; converted to Path only for local operations
    dhcp_static_filename: str
    dhcp_leases_files: list[str]
    dhcp_sync_command: list[str]
    dhcp_sync_workdir: str
    dhcp_validate_command: list[str]
    dhcp_remote_enabled: bool
    dhcp_ssh_host: str
    dhcp_ssh_username: str
    dhcp_ssh_password: str
    dhcp_ssh_port: int
    dhcp_ssh_timeout: int
    log_dir: Path
    netbox_url: str
    netbox_token: str
    netbox_auth_scheme: str
    mtik_username: str
    mtik_password: str
    mtik_port: int
    mtik_timeout: int
    internet_interface: str
    pppoe_interface: str
    dhcp_client_find: str
    ping_count: int
    ping_timeout_seconds: int
    reboot_wait_seconds: int
    post_renew_wait_seconds: int


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    def env_or_cfg(env_name: str, cfg_key: str, default: Optional[str] = None) -> str:
        value = os.getenv(env_name) or raw.get(cfg_key) or default
        if value is None or value == "":
            raise ValueError(f"Missing required config: {cfg_key} or env {env_name}")
        return str(value)

    return AppConfig(
        dhcp_root=posix_path(os.getenv("DHCP_ROOT", raw.get("dhcp_root", "/etc/dhcp"))),
        dhcp_static_filename=str(raw.get("dhcp_static_filename", "Mikrotik-Static")),
        dhcp_leases_files=[posix_path(p) for p in raw.get("dhcp_leases_files", ["/var/lib/dhcp/dhcpd.leases", "/var/log/syslog"])],
        dhcp_sync_command=shlex.split(os.getenv("DHCP_SYNC_COMMAND", "")) or list(raw.get("dhcp_sync_command", ["./dhcp-sync.sh", "apply"])),
        dhcp_sync_workdir=posix_path(os.getenv("DHCP_SYNC_WORKDIR", raw.get("dhcp_sync_workdir", raw.get("dhcp_root", "/etc/dhcp")))),
        dhcp_validate_command=shlex.split(os.getenv("DHCP_VALIDATE_COMMAND", "")) or list(raw.get("dhcp_validate_command", ["dhcpd", "-t", "-cf", "/etc/dhcp/dhcpd.conf"])),
        dhcp_remote_enabled=str(os.getenv("DHCP_REMOTE_ENABLED", raw.get("dhcp_remote_enabled", "false"))).lower() in ("1", "true", "yes", "on"),
        dhcp_ssh_host=str(os.getenv("DHCP_SSH_HOST", raw.get("dhcp_ssh_host", ""))),
        dhcp_ssh_username=str(os.getenv("DHCP_SSH_USERNAME", raw.get("dhcp_ssh_username", "root"))),
        dhcp_ssh_password=str(os.getenv("DHCP_SSH_PASSWORD", raw.get("dhcp_ssh_password", ""))),
        dhcp_ssh_port=int(os.getenv("DHCP_SSH_PORT", raw.get("dhcp_ssh_port", 22))),
        dhcp_ssh_timeout=int(os.getenv("DHCP_SSH_TIMEOUT", raw.get("dhcp_ssh_timeout", 20))),
        log_dir=Path(raw.get("log_dir", "./logs")),
        netbox_url=env_or_cfg("NETBOX_URL", "netbox_url").rstrip("/"),
        netbox_token=env_or_cfg("NETBOX_TOKEN", "netbox_token"),
        netbox_auth_scheme=str(raw.get("netbox_auth_scheme", os.getenv("NETBOX_AUTH_SCHEME", "Bearer"))).strip(),
        mtik_username=str(raw.get("mtik_username", "admin")),
        mtik_password=env_or_cfg("MTIK_PASSWORD", "mtik_password"),
        mtik_port=int(raw.get("mtik_port", 22)),
        mtik_timeout=int(raw.get("mtik_timeout", 20)),
        internet_interface=str(os.getenv("INTERNET_INTERFACE", raw.get("internet_interface", "bridge-Internet"))),
        pppoe_interface=str(raw.get("pppoe_interface", "pppoe-out-Internet")),
        dhcp_client_find=str(raw.get("dhcp_client_find", "[find disabled=no]")),
        ping_count=int(raw.get("ping_count", 3)),
        ping_timeout_seconds=int(raw.get("ping_timeout_seconds", 2)),
        reboot_wait_seconds=int(raw.get("reboot_wait_seconds", 90)),
        post_renew_wait_seconds=int(raw.get("post_renew_wait_seconds", 10)),
    )


def setup_logging(log_dir: Path, sla: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = log_dir / f"provision-inet-{sla}-{stamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    return log_file


def posix_path(value: Any) -> str:
    """Return a Linux/POSIX path even when the script is executed on Windows.

    Important for remote DHCP commands: /etc/dhcp must not become \etc\dhcp.
    """
    path = str(value).strip()
    path = path.replace("\\", "/")
    if path.startswith("//"):
        # Keep UNC-like strings intact only if someone intentionally provides them.
        return path
    return path


def local_path(value: Any) -> Path:
    """Convert config value to local Path only for local filesystem operations."""
    return Path(str(value))


def run_local(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    logging.info("LOCAL CMD: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    if proc.stdout.strip():
        logging.info("LOCAL STDOUT: %s", proc.stdout.strip())
    if proc.stderr.strip():
        logging.info("LOCAL STDERR: %s", proc.stderr.strip())
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return proc


def ssh_connect_dhcp(cfg: AppConfig) -> paramiko.SSHClient:
    if not cfg.dhcp_ssh_host:
        raise RuntimeError("DHCP remote mode is enabled but DHCP_SSH_HOST/dhcp_ssh_host is empty")
    if not cfg.dhcp_ssh_password:
        raise RuntimeError("DHCP remote mode is enabled but DHCP_SSH_PASSWORD/dhcp_ssh_password is empty")
    logging.info("Connecting to DHCP server %s@%s:%s via SSH", cfg.dhcp_ssh_username, cfg.dhcp_ssh_host, cfg.dhcp_ssh_port)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.dhcp_ssh_host,
        port=cfg.dhcp_ssh_port,
        username=cfg.dhcp_ssh_username,
        password=cfg.dhcp_ssh_password,
        timeout=cfg.dhcp_ssh_timeout,
        banner_timeout=cfg.dhcp_ssh_timeout,
        auth_timeout=cfg.dhcp_ssh_timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def remote_cmd(cfg: AppConfig, command: str, check: bool = True) -> str:
    logging.info("DHCP REMOTE CMD: %s", command)
    client = ssh_connect_dhcp(cfg)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=120)
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()
        if out:
            logging.info("DHCP REMOTE STDOUT: %s", out[:4000])
        if err:
            logging.info("DHCP REMOTE STDERR: %s", err[:4000])
        rc = stdout.channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(f"Remote DHCP command failed rc={rc}: {command}; stderr={err}")
        return out
    finally:
        client.close()


def remote_find_static_files(cfg: AppConfig) -> list[str]:
    root = shlex.quote(posix_path(cfg.dhcp_root))
    name = shlex.quote(cfg.dhcp_static_filename)
    out = remote_cmd(cfg, f"find {root} -type f -name {name} 2>/dev/null", check=True)
    return [line.strip() for line in out.splitlines() if line.strip()]


def remote_static_grep_for_mac(cfg: AppConfig, mac: str) -> Optional[tuple[str, str, str, str]]:
    """Fast remote lookup: grep all Mikrotik-Static files for one MAC without SFTP-reading every file."""
    root = shlex.quote(posix_path(cfg.dhcp_root))
    name = shlex.quote(cfg.dhcp_static_filename)
    mac_q = shlex.quote(mac)
    cmd = f"find {root} -type f -name {name} -exec grep -iH {mac_q} {{}} \; 2>/dev/null | head -n 20"
    out = remote_cmd(cfg, cmd, check=False)
    for line in out.splitlines():
        if ":" not in line:
            continue
        file, host_line = line.split(":", 1)
        m = HOST_BLOCK_RE.search(host_line)
        if m and m.group("mac").lower() == mac:
            return file.strip(), m.group("host"), m.group("mac").lower(), m.group("ip")
    return None


def remote_static_files_for_prefix(cfg: AppConfig, prefixes: list[str]) -> list[str]:
    """Find static files containing fixed-address values matching one of the requested prefixes."""
    root = shlex.quote(posix_path(cfg.dhcp_root))
    name = shlex.quote(cfg.dhcp_static_filename)
    escaped = [re.escape(x) for x in prefixes]
    pattern = "fixed-address (" + "|".join(escaped) + ")"
    cmd = f"find {root} -type f -name {name} -exec grep -lE {shlex.quote(pattern)} {{}} \; 2>/dev/null"
    out = remote_cmd(cfg, cmd, check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def remote_read_text(cfg: AppConfig, path: str) -> str:
    # Use remote cat instead of SFTP read. This respects exec_command timeout and avoids
    # Paramiko SFTP hanging indefinitely on large or slow files.
    path = posix_path(path)
    return remote_cmd(cfg, f"cat {shlex.quote(path)}", check=True)


def remote_write_text(cfg: AppConfig, path: str, text: str) -> None:
    """Write a remote file using base64 over SSH instead of SFTP.

    This avoids Paramiko SFTP hangs and keeps the write atomic via a temporary file + mv.
    """
    path = posix_path(path)
    tmp = f"{path}.tmp-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    cmd = f"cat > {shlex.quote(tmp)} <<'__MTIK_PROVISIONER_B64__'\n{encoded}\n__MTIK_PROVISIONER_B64__\nbase64 -d {shlex.quote(tmp)} > {shlex.quote(tmp)}.decoded && mv {shlex.quote(tmp)}.decoded {shlex.quote(path)} && rm -f {shlex.quote(tmp)}"
    remote_cmd(cfg, cmd, check=True)


def shell_join(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def remote_collect_static_entries(cfg: AppConfig) -> dict[str, list[tuple[str, str, str]]]:
    found: dict[str, list[tuple[str, str, str]]] = {}
    for file in remote_find_static_files(cfg):
        text = remote_read_text(cfg, file)
        entries = [(m.group("host"), m.group("mac").lower(), m.group("ip")) for m in HOST_BLOCK_RE.finditer(text)]
        if entries:
            found[file] = entries
    return found


def find_mgmt_ip_from_mac_remote(mac: str, cfg: AppConfig) -> str:
    mac = normalize_mac(mac)
    logging.info("Searching remote DHCP server for MAC %s", mac)

    # Fast path: search static reservation files with remote grep. This avoids SFTP-reading every
    # Mikrotik-Static file and prevents hangs on large/slow files.
    static_hit = remote_static_grep_for_mac(cfg, mac)
    if static_hit:
        static_file, _host, _existing_mac, ip = static_hit
        logging.info("Found existing remote static reservation %s in %s", ip, static_file)
        return ip

    # Then search live syslog/leases using finite grep commands on the remote DHCP server.
    # Do NOT use tail -f in automation because it never exits. The equivalent finite lookup is:
    # grep -i <mac> /var/log/syslog | tail -n 20
    for remote_path in cfg.dhcp_leases_files:
        remote_path = posix_path(remote_path)
        try:
            if remote_path.endswith("syslog") or "/log/" in remote_path:
                cmd = f"grep -i {shlex.quote(mac)} {shlex.quote(remote_path)} 2>/dev/null | tail -n 20"
                text = remote_cmd(cfg, cmd, check=False)
            else:
                # dhcpd.leases can be large, so grep for the MAC and nearby lease lines instead of SFTP-reading it.
                cmd = f"grep -i -B 8 -A 8 {shlex.quote(mac)} {shlex.quote(remote_path)} 2>/dev/null | tail -n 80"
                text = remote_cmd(cfg, cmd, check=False)
        except Exception as exc:
            logging.warning("Remote DHCP lookup source not readable: %s (%s)", remote_path, exc)
            continue

        candidates: list[str] = []

        if remote_path.endswith("syslog") or "/log/" in remote_path:
            # Syslog lookup is already filtered with grep -i <MAC>, so every returned line
            # should be related to the MAC. Pick the last valid IPv4 seen in those matching lines.
            for line in text.splitlines():
                if mac in line.lower():
                    candidates.extend(IP_RE.findall(line))
        else:
            # dhcpd.leases lookup uses grep -B/-A context. That context may include the next
            # lease header, so DO NOT collect every line that says "lease". Only trust a full
            # lease block whose body contains the MAC. This prevents selecting an adjacent lease.
            lease_block_re = re.compile(r"lease\s+(?P<ip>[0-9.]+)\s*\{(?P<body>.*?)\}", re.IGNORECASE | re.DOTALL)
            for block in lease_block_re.finditer(text):
                if mac in block.group("body").lower():
                    candidates.append(block.group("ip"))

        if candidates:
            valid = []
            for candidate in candidates:
                try:
                    ipaddress.ip_address(candidate)
                    valid.append(candidate)
                except ValueError:
                    pass
            if valid:
                ip = valid[-1]
                logging.info("Found dynamic management IP %s in remote DHCP log/source %s", ip, remote_path)
                return ip

    raise RuntimeError(f"Could not find management IP for MAC {mac} on remote DHCP server")


def choose_next_static_management_ip(base_octets: list[str], used_ips: list[str]) -> str:
    """Choose a static management IP from x.x.255.X first, then x.x.254.X, x.x.253.X, etc.

    Allocation is sequential within each /24: use one higher than the highest used IP
    in that third octet. This avoids filling old gaps unless the sequential range is full.
    """
    first = base_octets[0]
    second = base_octets[1]
    used: set[ipaddress.IPv4Address] = set()
    for ip in used_ips:
        try:
            used.add(ipaddress.ip_address(ip))
        except ValueError:
            continue

    for third in range(255, -1, -1):
        network = ipaddress.ip_network(f"{first}.{second}.{third}.0/24", strict=False)
        used_in_pool = sorted(ip for ip in used if ip in network)
        if used_in_pool:
            start_last = int(str(used_in_pool[-1]).split(".")[-1]) + 1
        else:
            start_last = 2

        for last in range(max(start_last, 2), 254):
            candidate = ipaddress.ip_address(f"{first}.{second}.{third}.{last}")
            if candidate not in used:
                logging.info("Next available static management IP from %s.%s.%s.X pool: %s", first, second, third, candidate)
                return str(candidate)

        # If sequential allocation reached the top, fill any older gap in this third octet
        # before moving down to the next lower third octet.
        for last in range(2, 254):
            candidate = ipaddress.ip_address(f"{first}.{second}.{third}.{last}")
            if candidate not in used:
                logging.warning("Static pool %s.%s.%s.X reached the top; using free gap: %s", first, second, third, candidate)
                return str(candidate)

        logging.warning("Static pool %s.%s.%s.X is full; trying %s.%s.%s.X", first, second, third, first, second, third - 1 if third > 0 else 0)

    raise RuntimeError(f"No available static management IP found under {first}.{second}.0.0/16")


def choose_dhcp_static_file_and_ip_remote(cfg: AppConfig, mgmt_ip: str, mac: str) -> tuple[str, str]:
    mac = normalize_mac(mac)
    octets = mgmt_ip.split(".")
    first2 = ".".join(octets[:2]) + "."
    first3 = ".".join(octets[:3]) + "."
    static_prefix = f"{octets[0]}.{octets[1]}.255."

    # Fast duplicate guard: search only for the MAC remotely.
    static_hit = remote_static_grep_for_mac(cfg, mac)
    if static_hit:
        file, _host, _existing_mac, existing_ip = static_hit
        logging.info("MAC already exists in remote %s with fixed-address %s", file, existing_ip)
        return file, existing_ip

    # Select the correct region file. For MikroTik static management reservations,
    # prefer the x.x.255.X pool for the same first two octets, even when the current
    # dynamic lease is x.x.253.X or x.x.254.X.
    candidate_files = remote_static_files_for_prefix(cfg, [static_prefix])
    if not candidate_files:
        candidate_files = remote_static_files_for_prefix(cfg, [first3])
    if not candidate_files:
        candidate_files = remote_static_files_for_prefix(cfg, [first2])
    if not candidate_files:
        raise RuntimeError(f"No remote {cfg.dhcp_static_filename} file found containing region prefix {static_prefix}, {first3}, or {first2}")

    chosen_file = candidate_files[0]
    logging.info("Selected remote DHCP static file: %s", chosen_file)

    # Read only the chosen file, not every region file.
    text = remote_read_text(cfg, chosen_file)
    entries = [(m.group("host"), m.group("mac").lower(), m.group("ip")) for m in HOST_BLOCK_RE.finditer(text)]
    used_ips = [ip for _h, _m, ip in entries]
    used_macs = {m for _h, m, _ip in entries}

    if mac in used_macs:
        for _host, existing_mac, existing_ip in entries:
            if existing_mac == mac:
                logging.info("MAC already exists in remote %s with fixed-address %s", chosen_file, existing_ip)
                return chosen_file, existing_ip

    # Allocate static management IP from x.x.255.X first. If 255 is full,
    # automatically try x.x.254.X, then x.x.253.X, etc.
    next_ip = choose_next_static_management_ip(octets, used_ips)
    return chosen_file, next_ip

def add_dhcp_static_reservation_remote(cfg: AppConfig, file: str, hostname: str, mac: str, fixed_ip: str, dry_run: bool) -> None:
    """Add a DHCP static reservation on the remote DHCP server only when needed.

    If the MAC already exists with the same fixed-address, this is a clean no-op.
    This prevents dry-runs and apply-runs from saying they will add a duplicate entry.
    """
    mac = normalize_mac(mac)
    text = remote_read_text(cfg, file)

    for entry in HOST_BLOCK_RE.finditer(text):
        existing_host = entry.group("host")
        existing_mac = normalize_mac(entry.group("mac"))
        existing_ip = entry.group("ip")

        if existing_mac == mac:
            if existing_ip == fixed_ip:
                placeholder_hosts = {"DRYRUNSERIAL", "DRY-RUN", "UNKNOWN-SERIAL"}
                if existing_host.upper() in placeholder_hosts and hostname.upper() not in placeholder_hosts and not dry_run:
                    logging.info(
                        "Existing remote DHCP reservation uses placeholder host %s. Updating host name to real serial %s.",
                        existing_host, hostname,
                    )
                    backup = f"{file}.bak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
                    remote_cmd(cfg, f"cp -p {shlex.quote(file)} {shlex.quote(backup)}", check=True)
                    pattern = re.compile(rf"host\s+{re.escape(existing_host)}\s*\{{\s*hardware\s+ethernet\s+{re.escape(entry.group('mac'))};\s*fixed-address\s+{re.escape(existing_ip)};\s*\}}", re.IGNORECASE)
                    replacement = f"host {hostname} {{ hardware ethernet {mac}; fixed-address {fixed_ip}; }}"
                    new_text, count = pattern.subn(replacement, text, count=1)
                    if count != 1:
                        raise RuntimeError(f"Could not safely update placeholder DHCP host {existing_host} in {file}")
                    remote_write_text(cfg, file, new_text)
                    return
                logging.info(
                    "Existing remote DHCP reservation found in %s: host %s already maps MAC %s to fixed-address %s. No DHCP file change needed.",
                    file, existing_host, mac, fixed_ip,
                )
                return
            raise RuntimeError(
                f"Remote DHCP MAC conflict in {file}: MAC {mac} already exists as host {existing_host} with fixed-address {existing_ip}, not requested {fixed_ip}"
            )

        if existing_ip == fixed_ip:
            raise RuntimeError(
                f"Remote DHCP IP conflict in {file}: fixed-address {fixed_ip} already belongs to host {existing_host} with MAC {existing_mac}"
            )

    line = f"host {hostname} {{ hardware ethernet {mac}; fixed-address {fixed_ip}; }}\n"
    logging.info("Remote DHCP reservation to add: %s", line.strip())
    if dry_run:
        logging.info("DRY-RUN: no remote DHCP file change performed")
        return

    backup = f"{file}.bak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    remote_cmd(cfg, f"cp -p {shlex.quote(file)} {shlex.quote(backup)}", check=True)
    logging.info("Created remote DHCP file backup: %s", backup)
    if not text.endswith("\n"):
        text += "\n"
    text += line
    remote_write_text(cfg, file, text)


def resolve_remote_dhcp_sync_command(cfg: AppConfig) -> tuple[str, str]:
    """Return (workdir, command) for DHCP sync.

    If config still says ./dhcp-sync.sh but it is not under /etc/dhcp, try to find it
    in common admin locations. This prevents partial apply failures caused by a missing
    relative script path.
    """
    workdir = posix_path(cfg.dhcp_sync_workdir or cfg.dhcp_root)
    cmd_parts = list(cfg.dhcp_sync_command)
    if not cmd_parts:
        raise RuntimeError("dhcp_sync_command is empty")

    first = str(cmd_parts[0])
    if first == "./dhcp-sync.sh":
        expected = f"{workdir.rstrip('/')}/dhcp-sync.sh"
        test = remote_cmd(cfg, f"test -f {shlex.quote(expected)} && echo {shlex.quote(expected)} || true", check=False).strip()
        if test:
            cmd_parts[0] = test.splitlines()[-1].strip()
        else:
            found = remote_cmd(
                cfg,
                "find /etc/dhcp /root /usr/local/bin /opt -maxdepth 4 -type f -name dhcp-sync.sh 2>/dev/null | head -n 1",
                check=False,
            ).strip()
            if found:
                cmd_parts[0] = found.splitlines()[-1].strip()
                logging.info("Auto-detected DHCP sync script at %s", cmd_parts[0])
            else:
                raise RuntimeError(
                    f"DHCP sync script not found. Expected {expected}. "
                    "Set dhcp_sync_command in config.yaml or set env DHCP_SYNC_COMMAND to the full command."
                )

    return workdir, shell_join(cmd_parts)


def validate_and_sync_dhcp_remote(cfg: AppConfig, dry_run: bool) -> None:
    logging.info("Validating DHCP configuration on remote DHCP server")
    if cfg.dhcp_validate_command:
        remote_cmd(cfg, shell_join(cfg.dhcp_validate_command), check=True)
    logging.info("Applying DHCP sync on remote DHCP server")
    workdir, sync_cmd = resolve_remote_dhcp_sync_command(cfg)
    full_cmd = f"cd {shlex.quote(workdir)} && {sync_cmd}"
    if dry_run:
        logging.info("DRY-RUN: would execute remote DHCP sync command: %s", full_cmd)
    else:
        remote_cmd(cfg, full_cmd, check=True)


def normalize_mac(mac: str) -> str:
    mac = mac.strip().replace("-", ":").lower()
    if not MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC address: {mac}")
    return mac


def find_mgmt_ip_from_mac(mac: str, cfg: AppConfig) -> str:
    mac = normalize_mac(mac)
    logging.info("Searching DHCP files for MAC %s", mac)

    # First search static reservation files.
    for static_file in local_path(cfg.dhcp_root).rglob(cfg.dhcp_static_filename):
        text = static_file.read_text(errors="ignore")
        for m in HOST_BLOCK_RE.finditer(text):
            if m.group("mac").lower() == mac:
                ip = m.group("ip")
                logging.info("Found existing static reservation %s in %s", ip, static_file)
                return ip

    # Then search leases/syslog-style files. This supports common dhcpd.leases blocks and syslog lines.
    for lease_file in cfg.dhcp_leases_files:
        lease_path = local_path(lease_file)
        if not lease_path.exists():
            logging.warning("DHCP lookup source not found: %s", lease_path)
            continue
        text = lease_path.read_text(errors="ignore")
        candidates: list[str] = []
        # dhcpd.leases format: lease 10.x.x.x { ... hardware ethernet aa:bb:...; ... }
        lease_block_re = re.compile(r"lease\s+(?P<ip>[0-9.]+)\s*\{(?P<body>.*?)\}", re.IGNORECASE | re.DOTALL)
        for block in lease_block_re.finditer(text):
            if mac in block.group("body").lower():
                candidates.append(block.group("ip"))
        # syslog fallback: same line containing MAC and IP
        for line in text.splitlines():
            if mac in line.lower():
                ips = IP_RE.findall(line)
                candidates.extend(ips)
        if candidates:
            ip = candidates[-1]
            logging.info("Found dynamic management IP %s in %s", ip, lease_path)
            return ip

    raise RuntimeError(f"Could not find management IP for MAC {mac}")


def ssh_connect(ip: str, cfg: AppConfig) -> paramiko.SSHClient:
    logging.info("Connecting to MikroTik %s via SSH", ip)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=ip,
        port=cfg.mtik_port,
        username=cfg.mtik_username,
        password=cfg.mtik_password,
        timeout=cfg.mtik_timeout,
        banner_timeout=cfg.mtik_timeout,
        auth_timeout=cfg.mtik_timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def mtik_cmd(client: paramiko.SSHClient, command: str, dry_run: bool = False, allow_fail: bool = False) -> str:
    logging.info("MTIK CMD: %s", command)
    if dry_run:
        return "DRY-RUN"
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=30)
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()
        if out:
            logging.info("MTIK STDOUT: %s", out)
        if err:
            logging.info("MTIK STDERR: %s", err)
        rc = stdout.channel.recv_exit_status()
    except Exception as exc:
        # During DHCP release/renew, RouterOS may drop the SSH connection because the management IP changes.
        if allow_fail:
            logging.warning("MikroTik command did not complete but allow_fail=True: %s (%s)", command, exc)
            return ""
        raise

    # RouterOS SSH sometimes returns CLI errors in stdout while rc is still 0.
    combined = f"{out}\n{err}".lower()
    routeros_error_markers = (
        "input does not match",
        "no such item",
        "failure:",
        "bad command",
        "syntax error",
        "expected end of command",
    )
    has_routeros_error = any(marker in combined for marker in routeros_error_markers)
    if (rc != 0 or has_routeros_error) and not allow_fail:
        raise RuntimeError(f"MikroTik command failed: {command}; stdout={out}; stderr={err}")
    return out


def routeros_quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def get_routerboard_serial(client: paramiko.SSHClient, dry_run: bool = False) -> str:
    if dry_run:
        logging.info("RouterBOARD serial number: DRY-RUN")
        return "DRY-RUN"

    commands = [
        ':put [/system routerboard get serial-number]',
        '/system routerboard get serial-number',
        ':put [/system routerboard print as-value]',
        '/system routerboard print as-value',
        '/system routerboard print',
    ]
    serial = ""
    for cmd in commands:
        out = mtik_cmd(client, cmd, dry_run=False, allow_fail=True)
        text = out.strip()
        if not text:
            continue
        m = re.search(r"serial-number[=:]([^\s;]+)", text, re.IGNORECASE)
        if m:
            serial = m.group(1)
            break
        # For ':put [/system routerboard get serial-number]' the output should just be the serial.
        last = text.splitlines()[-1].strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{5,}", last) and "routerboard" not in last.lower():
            serial = last
            break

    serial = re.sub(r"[^A-Za-z0-9_-]", "", serial)
    if not serial or serial.upper() in {"DRYRUNSERIAL", "DRY-RUN", "UNKNOWN-SERIAL"}:
        raise RuntimeError("Could not read real RouterBOARD serial number; refusing to write DHCP host with placeholder serial")
    logging.info("RouterBOARD serial number: %s", serial)
    return serial


def netbox_headers(cfg: AppConfig) -> dict[str, str]:
    # Your NetBox example uses: Authorization: Bearer <token>.
    # Some NetBox deployments use: Authorization: Token <token>.
    # Configure with netbox_auth_scheme or NETBOX_AUTH_SCHEME.
    scheme = cfg.netbox_auth_scheme or "Bearer"
    return {"Authorization": f"{scheme} {cfg.netbox_token}", "Accept": "application/json", "Content-Type": "application/json"}


def test_netbox_status(cfg: AppConfig) -> None:
    url = f"{cfg.netbox_url}/api/status/"
    logging.info("NETBOX STATUS GET: %s", url)
    r = requests.get(url, headers=netbox_headers(cfg), timeout=30)
    r.raise_for_status()
    logging.info("NETBOX STATUS OK: %s", r.text[:500])


def nb_get(cfg: AppConfig, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{cfg.netbox_url}/api/{endpoint.lstrip('/')}"
    results: list[dict[str, Any]] = []
    while url:
        logging.info("NETBOX GET: %s params=%s", url, params)
        r = requests.get(url, headers=netbox_headers(cfg), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "results" in data:
            results.extend(data["results"])
            url = data.get("next")
            params = {}
        elif isinstance(data, list):
            results.extend(data)
            url = ""
        else:
            raise RuntimeError(f"Unexpected NetBox response from {endpoint}: {data}")
    return results




def nb_get_safe(cfg: AppConfig, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """NetBox GET wrapper used for optional filters. Logs and continues on 400/404-style incompatibilities."""
    try:
        return nb_get(cfg, endpoint, params)
    except requests.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else "unknown"
        body = response.text[:300] if response is not None else str(exc)
        logging.warning("NETBOX optional query failed endpoint=%s params=%s status=%s body=%s", endpoint, params, status, body)
        return []

def nb_patch(cfg: AppConfig, url_or_endpoint: str, payload: dict[str, Any], dry_run: bool) -> None:
    if url_or_endpoint.startswith("http"):
        url = url_or_endpoint
    else:
        url = f"{cfg.netbox_url}/api/{url_or_endpoint.lstrip('/')}"
    logging.info("NETBOX PATCH: %s payload=%s", url, payload)
    if dry_run:
        return
    r = requests.patch(url, headers=netbox_headers(cfg), json=payload, timeout=30)
    r.raise_for_status()
    logging.info("NETBOX PATCH OK")




def nb_post(cfg: AppConfig, endpoint: str, payload: dict[str, Any], dry_run: bool) -> Optional[dict[str, Any]]:
    """Create an object in NetBox unless this is a dry-run."""
    url = f"{cfg.netbox_url}/api/{endpoint.lstrip('/')}"
    logging.info("NETBOX POST: %s payload=%s", url, payload)
    if dry_run:
        return None

    r = requests.post(url, headers=netbox_headers(cfg), json=payload, timeout=30)
    r.raise_for_status()
    logging.info("NETBOX POST OK")
    return r.json()


def ensure_netbox_mtik_ip(cfg: AppConfig, static_ip: str, sla: str, name: str, dry_run: bool) -> None:
    """
    Ensure NetBox IPAM -> IP Addresses has the MikroTik management /32.

    Example for SLA 20677566 and name BOC:
      address: 10.205.255.82/32
      description: BOC-20677566-Mtik
    """
    address = f"{static_ip}/32"
    description = f"{name}-{sla}-Mtik"

    logging.info("Ensuring NetBox MikroTik management IP exists: %s description=%s", address, description)

    # NetBox accepts address exact filtering in recent versions. Fallbacks below avoid duplicate creation
    # if a deployment behaves differently.
    existing: list[dict[str, Any]] = []
    for params in ({"address": address}, {"q": static_ip}, {"description__ic": f"{sla}-Mtik"}):
        existing.extend(nb_get_safe(cfg, "ipam/ip-addresses/", params))

    seen: set[str] = set()
    unique_existing: list[dict[str, Any]] = []
    for item in existing:
        key = str(item.get("id") or item.get("url") or item.get("address"))
        if key in seen:
            continue
        seen.add(key)
        unique_existing.append(item)

    for item in unique_existing:
        if str(item.get("address")) == address:
            current_desc = str(item.get("description", ""))
            logging.info("NetBox MikroTik management IP already exists: %s description=%s", address, current_desc)
            if current_desc != description:
                nb_patch(
                    cfg,
                    item.get("url") or f"ipam/ip-addresses/{item['id']}/",
                    {"description": description},
                    dry_run,
                )
            return

    payload = {
        "address": address,
        "description": description,
        "status": "active",
    }
    nb_post(cfg, "ipam/ip-addresses/", payload, dry_run)


def find_netbox_prefix_for_sla(cfg: AppConfig, sla: str, service: str, name: str, dry_run: bool) -> str:
    logging.info("Searching NetBox for SLA %s", sla)
    wanted_desc = f"{name}-{sla}-{service}"

    # Try multiple NetBox lookup styles. Different NetBox versions expose slightly different search behavior.
    # Your UI global search finds Description values like BOC-Inet-20675166, so description__ic is important.
    ip_results: list[dict[str, Any]] = []
    prefix_results: list[dict[str, Any]] = []

    for params in ({"q": sla}, {"description__ic": sla}, {"description": sla}):
        ip_results.extend(nb_get_safe(cfg, "ipam/ip-addresses/", params))
        prefix_results.extend(nb_get_safe(cfg, "ipam/prefixes/", params))

    # De-duplicate by object ID/URL.
    def unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            key = str(item.get("url") or item.get("id") or item)
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    ip_results = unique(ip_results)
    prefix_results = unique(prefix_results)
    logging.info("NetBox candidate counts: ip_addresses=%s prefixes=%s", len(ip_results), len(prefix_results))

    # For Inet, prefer Prefix objects with description containing the service, e.g. BOC-Inet-20675166.
    def has_sla(item: dict[str, Any]) -> bool:
        blob = " ".join(str(item.get(k, "")) for k in ("description", "display", "address", "prefix"))
        return sla in blob

    def service_score(item: dict[str, Any]) -> int:
        desc = str(item.get("description", "")).lower()
        if service.lower() in desc:
            return 100
        if "inet" in desc or "internet" in desc:
            return 90
        if "mtik" in desc:
            return -100
        return 0

    related_prefixes = [x for x in prefix_results if has_sla(x)]
    if related_prefixes:
        related_prefixes.sort(key=service_score, reverse=True)
        item = related_prefixes[0]
        prefix = item.get("prefix") or item.get("display")
        logging.info("Selected NetBox prefix registration: %s description=%s", prefix, item.get("description"))
        nb_patch(cfg, item.get("url") or f"ipam/prefixes/{item['id']}/", {"description": wanted_desc}, dry_run)
        return str(prefix)

    # Fallback: some deployments store the Inet allocation as an IPAddress object.
    # For Inet, never choose a management/Mtik /32 when a service-specific Inet object is missing.
    related_ips = [
        x for x in ip_results
        if has_sla(x)
        and "/" in str(x.get("address") or x.get("display"))
        and "mtik" not in str(x.get("description", "")).lower()
    ]
    if related_ips:
        related_ips.sort(key=service_score, reverse=True)
        item = related_ips[0]
        prefix = item.get("address") or item.get("display")
        logging.info("Selected NetBox IP address registration: %s description=%s", prefix, item.get("description"))
        nb_patch(cfg, item.get("url") or f"ipam/ip-addresses/{item['id']}/", {"description": wanted_desc}, dry_run)
        return str(prefix)

    mtik_ips = [x for x in ip_results if has_sla(x) and "mtik" in str(x.get("description", "")).lower()]
    if mtik_ips:
        logging.warning("Found SLA only as Mtik management IP object(s), not as Inet prefix/IP. Refusing to configure management /32 as Internet IP.")

    raise RuntimeError(f"No NetBox IP/prefix result found for SLA {sla}. Check that the SLA exists in Prefix/IP description and that you used the correct 8-digit SLA.")


def mikrotik_address_from_netbox(value: str) -> tuple[str, str]:
    # For Inet /30 records, NetBox stores the network/base IP, for example 212.10.231.26/30,
    # so MikroTik receives the next usable IP: 212.10.231.27/30 and network=212.10.231.26.
    # For /32 records, preserve the exact IP. Example: 10.205.255.78/32 stays 10.205.255.78/32.
    ip_part, mask = value.split("/", 1)
    base = ipaddress.ip_address(ip_part)
    if mask == "32":
        return f"{base}/{mask}", ip_part
    mtik_ip = ipaddress.ip_address(int(base) + 1)
    return f"{mtik_ip}/{mask}", ip_part


def collect_static_entries(cfg: AppConfig) -> dict[Path, list[tuple[str, str, str]]]:
    found: dict[Path, list[tuple[str, str, str]]] = {}
    for file in local_path(cfg.dhcp_root).rglob(cfg.dhcp_static_filename):
        text = file.read_text(errors="ignore")
        entries = [(m.group("host"), m.group("mac").lower(), m.group("ip")) for m in HOST_BLOCK_RE.finditer(text)]
        if entries:
            found[file] = entries
    return found


def choose_dhcp_static_file_and_ip(cfg: AppConfig, mgmt_ip: str, mac: str) -> tuple[Path, str]:
    mac = normalize_mac(mac)
    octets = mgmt_ip.split(".")
    first2 = ".".join(octets[:2]) + "."
    first3 = ".".join(octets[:3]) + "."
    static_prefix = f"{octets[0]}.{octets[1]}.255."
    entries_by_file = collect_static_entries(cfg)

    # Duplicate guard.
    for file, entries in entries_by_file.items():
        for _host, existing_mac, existing_ip in entries:
            if existing_mac == mac:
                logging.info("MAC already exists in %s with fixed-address %s", file, existing_ip)
                return file, existing_ip

    candidates: list[tuple[int, Path, list[str]]] = []
    for file, entries in entries_by_file.items():
        ips = [ip for _h, _m, ip in entries]
        score = 0
        if any(ip.startswith(static_prefix) for ip in ips):
            score = 4
        elif any(ip.startswith(first3) for ip in ips):
            score = 3
        elif any(ip.startswith(first2) for ip in ips):
            score = 2
        if score:
            candidates.append((score, file, ips))

    if not candidates:
        raise RuntimeError(f"No {cfg.dhcp_static_filename} file found containing region prefix {static_prefix}, {first3}, or {first2}")

    candidates.sort(key=lambda x: x[0], reverse=True)
    chosen_file, used_ips = candidates[0][1], candidates[0][2]
    logging.info("Selected DHCP static file: %s", chosen_file)

    # Allocate static management IP from x.x.255.X first. If 255 is full,
    # automatically try x.x.254.X, then x.x.253.X, etc.
    next_ip = choose_next_static_management_ip(octets, used_ips)
    return chosen_file, next_ip

def add_dhcp_static_reservation(file: Path, hostname: str, mac: str, fixed_ip: str, dry_run: bool) -> None:
    """Add a local DHCP static reservation only when needed.

    If the MAC already exists with the same fixed-address, this is a clean no-op.
    """
    mac = normalize_mac(mac)
    text = file.read_text(errors="ignore")

    for entry in HOST_BLOCK_RE.finditer(text):
        existing_host = entry.group("host")
        existing_mac = normalize_mac(entry.group("mac"))
        existing_ip = entry.group("ip")

        if existing_mac == mac:
            if existing_ip == fixed_ip:
                logging.info(
                    "Existing DHCP reservation found in %s: host %s already maps MAC %s to fixed-address %s. No DHCP file change needed.",
                    file, existing_host, mac, fixed_ip,
                )
                return
            raise RuntimeError(
                f"DHCP MAC conflict in {file}: MAC {mac} already exists as host {existing_host} with fixed-address {existing_ip}, not requested {fixed_ip}"
            )

        if existing_ip == fixed_ip:
            raise RuntimeError(
                f"DHCP IP conflict in {file}: fixed-address {fixed_ip} already belongs to host {existing_host} with MAC {existing_mac}"
            )

    line = f"host {hostname} {{ hardware ethernet {mac}; fixed-address {fixed_ip}; }}\n"
    logging.info("DHCP reservation to add: %s", line.strip())
    if dry_run:
        logging.info("DRY-RUN: no local DHCP file change performed")
        return

    backup = file.with_suffix(file.suffix + f".bak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(file, backup)
    logging.info("Created DHCP file backup: %s", backup)
    with open(file, "a", encoding="utf-8") as f:
        if not text.endswith("\n"):
            f.write("\n")
        f.write(line)


def ping(ip: str, cfg: AppConfig) -> bool:
    # Linux/macOS use -c count. Windows uses -n count and -w timeout_ms.
    if platform.system().lower().startswith("win"):
        cmd = ["ping", "-n", str(cfg.ping_count), "-w", str(cfg.ping_timeout_seconds * 1000), ip]
    else:
        cmd = ["ping", "-c", str(cfg.ping_count), "-W", str(cfg.ping_timeout_seconds), ip]
    proc = run_local(cmd, check=False)
    ok = proc.returncode == 0
    logging.info("Ping %s result: %s", ip, "reachable" if ok else "not reachable")
    return ok


def validate_args(args: argparse.Namespace) -> None:
    if not args.mac and not args.mgmt_ip:
        raise ValueError("Provide either --mac or --mgmt-ip")
    if not args.sla or not re.match(r"^\d{8}$", args.sla):
        raise ValueError("--sla must be exactly 8 digits")
    if args.service != "Inet":
        raise ValueError("This first version supports only --service Inet")
    if not args.name or not re.match(r"^[A-Za-z0-9_.-]+$", args.name):
        raise ValueError("--name may contain only letters, numbers, underscore, dot, and dash")


def print_done_summary(port_number: str, customer_service: str, public_subnet: str, mtik_identity: str, mtik_mgmt_ip: str, serial: str) -> None:
    """Print a clean operator summary at the end of a successful run."""
    print("Done.")
    print()
    print(f"Port {port_number} --> {customer_service} --> Subnet: {public_subnet}")
    print(f"{mtik_identity} ---> {mtik_mgmt_ip}  SN: {serial}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision MikroTik CPE for Inet service")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--mac", help="MikroTik MAC address")
    parser.add_argument("--mgmt-ip", help="Current MikroTik management IP")
    parser.add_argument("--sla", help="8-digit customer SLA")
    parser.add_argument("--service", choices=["Inet"], help="Only Inet is supported in this version")
    parser.add_argument("--name", help="Customer/site name used for identity and NetBox description")
    parser.add_argument("--port", default="1", help="Port number shown in the final operator summary. Default: 1")
    parser.add_argument("--check-netbox", action="store_true", help="Only test NetBox /api/status/ authentication, then exit.")
    parser.add_argument("--skip-dhcp", action="store_true", help="Skip DHCP static-file selection/sync. Useful for Windows/API-only dry-runs.")
    parser.add_argument("--apply", action="store_true", help="Actually change MikroTik/NetBox/DHCP. Without this, dry-run mode is used.")
    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.check_netbox:
        log_file = setup_logging(cfg.log_dir, args.sla if args.sla else "netbox-check")
        try:
            test_netbox_status(cfg)
            print(f"NETBOX OK. Log file: {log_file}")
            return 0
        except Exception as exc:
            logging.exception("NetBox status check failed: %s", exc)
            print(f"NETBOX FAILED. Check log file: {log_file}", file=sys.stderr)
            return 1

    validate_args(args)
    log_file = setup_logging(cfg.log_dir, args.sla)
    dry_run = not args.apply
    logging.info("Starting Inet provisioning. dry_run=%s log_file=%s", dry_run, log_file)
    if cfg.dhcp_remote_enabled:
        logging.info("Remote DHCP mode enabled: %s@%s:%s root=%s", cfg.dhcp_ssh_username, cfg.dhcp_ssh_host, cfg.dhcp_ssh_port, cfg.dhcp_root)

    client: Optional[paramiko.SSHClient] = None
    try:
        mac = normalize_mac(args.mac) if args.mac else None
        mgmt_ip = args.mgmt_ip or (find_mgmt_ip_from_mac_remote(mac, cfg) if cfg.dhcp_remote_enabled else find_mgmt_ip_from_mac(mac, cfg))  # type: ignore[arg-type]
        ipaddress.ip_address(mgmt_ip)
        logging.info("Current management IP: %s", mgmt_ip)

        if not ping(mgmt_ip, cfg) and not dry_run:
            raise RuntimeError(f"MikroTik is not reachable on current management IP {mgmt_ip}")

        client = ssh_connect(mgmt_ip, cfg)
        serial = get_routerboard_serial(client, dry_run=dry_run)
        if not mac:
            logging.warning("No MAC was provided. DHCP static assignment will be skipped because MAC is required.")

        pppoe_user = f"{args.sla}-{args.service}"
        identity = f"{args.name}-{args.sla}-Mtik"

        logging.info("Configuring PPPoE interface and system identity")
        mtik_cmd(client, f"/interface pppoe-client set [find name={routeros_quote(cfg.pppoe_interface)}] disabled=no user={routeros_quote(pppoe_user)} password={routeros_quote(pppoe_user)}", dry_run=dry_run)
        mtik_cmd(client, f"/system identity set name={routeros_quote(identity)}", dry_run=dry_run)

        nb_prefix = find_netbox_prefix_for_sla(cfg, args.sla, args.service, args.name, dry_run=dry_run)
        address, network = mikrotik_address_from_netbox(nb_prefix)
        logging.info("MikroTik public address=%s network=%s interface=%s", address, network, cfg.internet_interface)
        if dry_run:
            mtik_cmd(client, f"/ip address add address={address} network={network} interface={routeros_quote(cfg.internet_interface)} comment={routeros_quote(pppoe_user)}", dry_run=dry_run, allow_fail=False)
        else:
            existing_addr = mtik_cmd(client, f"/ip address print as-value where address={routeros_quote(address)}", dry_run=False, allow_fail=True)
            if address in existing_addr:
                logging.info("MikroTik public address %s already exists. No duplicate address will be added.", address)
            else:
                mtik_cmd(client, f"/ip address add address={address} network={network} interface={routeros_quote(cfg.internet_interface)} comment={routeros_quote(pppoe_user)}", dry_run=False, allow_fail=False)

        new_static_ip = None
        if args.skip_dhcp:
            logging.info("Skipping DHCP static-file selection/sync because --skip-dhcp was provided")
        elif mac:
            if cfg.dhcp_remote_enabled:
                dhcp_file, new_static_ip = choose_dhcp_static_file_and_ip_remote(cfg, mgmt_ip, mac)
                add_dhcp_static_reservation_remote(cfg, dhcp_file, serial, mac, new_static_ip, dry_run=dry_run)
                validate_and_sync_dhcp_remote(cfg, dry_run=dry_run)
            else:
                dhcp_file, new_static_ip = choose_dhcp_static_file_and_ip(cfg, mgmt_ip, mac)
                add_dhcp_static_reservation(dhcp_file, serial, mac, new_static_ip, dry_run=dry_run)

                logging.info("Validating DHCP configuration")
                if cfg.dhcp_validate_command:
                    run_local(cfg.dhcp_validate_command, check=True)

                logging.info("Applying DHCP sync")
                if dry_run:
                    logging.info("DRY-RUN: would execute DHCP sync command: %s", " ".join(cfg.dhcp_sync_command))
                else:
                    run_local(cfg.dhcp_sync_command, cwd=local_path(cfg.dhcp_root), check=True)

            logging.info("Releasing and renewing MikroTik DHCP client")
            mtik_cmd(client, f"/ip dhcp-client release {cfg.dhcp_client_find}", dry_run=dry_run, allow_fail=True)
            time.sleep(cfg.post_renew_wait_seconds if not dry_run else 0)
            mtik_cmd(client, f"/ip dhcp-client renew {cfg.dhcp_client_find}", dry_run=dry_run, allow_fail=True)

            # A DHCP release/renew can change the management IP and drop the SSH session.
            # Close the old session and verify the CPE on the new static IP instead of treating that disconnect as failure.
            if not dry_run:
                try:
                    client.close()
                except Exception:
                    pass
                client = None
            time.sleep(cfg.post_renew_wait_seconds if not dry_run else 0)

            if dry_run:
                logging.info("DRY-RUN: would verify reachability on new static IP %s", new_static_ip)
            else:
                reachable = False
                for attempt in range(1, 7):
                    logging.info("Checking new static management IP %s, attempt %s/6", new_static_ip, attempt)
                    if ping(new_static_ip, cfg):
                        reachable = True
                        break
                    time.sleep(10)
                if reachable:
                    logging.info("MikroTik reachable on new static management IP %s", new_static_ip)
                else:
                    logging.warning("MikroTik not reachable on new static IP after renew attempts. Trying to reconnect to old management IP for reboot fallback.")
                    try:
                        fallback_client = ssh_connect(mgmt_ip, cfg)
                        mtik_cmd(fallback_client, "/system reboot", dry_run=False, allow_fail=True)
                        fallback_client.close()
                    except Exception as exc:
                        logging.warning("Could not reconnect to old management IP %s for reboot fallback: %s", mgmt_ip, exc)
                    time.sleep(cfg.reboot_wait_seconds)
                    if not ping(new_static_ip, cfg):
                        raise RuntimeError(f"MikroTik still not reachable on static IP {new_static_ip} after DHCP renew/reboot fallback")
                    logging.info("MikroTik reachable on new static management IP %s after reboot/wait", new_static_ip)

        if new_static_ip:
            ensure_netbox_mtik_ip(cfg, new_static_ip, args.sla, args.name, dry_run=dry_run)

        verify_ip = new_static_ip or mgmt_ip
        logging.info("Final verification against %s", verify_ip)
        if not dry_run and client is None:
            client = ssh_connect(verify_ip, cfg)
        if client:
            mtik_cmd(client, f"/interface pppoe-client print detail where name={routeros_quote(cfg.pppoe_interface)}", dry_run=dry_run, allow_fail=True)
            mtik_cmd(client, f"/ip address print detail where interface={routeros_quote(cfg.internet_interface)}", dry_run=dry_run, allow_fail=True)
            mtik_cmd(client, "/system identity print", dry_run=dry_run, allow_fail=True)

        logging.info("Provisioning completed successfully")
        print_done_summary(
            port_number=str(args.port),
            customer_service=f"{args.name}-{args.sla}-{args.service}",
            public_subnet=str(nb_prefix),
            mtik_identity=identity,
            mtik_mgmt_ip=str(verify_ip),
            serial=serial,
        )
        print(f"SUCCESS. Log file: {log_file}")
        return 0
    except Exception as exc:
        logging.exception("Provisioning failed: %s", exc)
        print(f"FAILED. Check log file: {log_file}", file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
