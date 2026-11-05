#!/usr/bin/env python3

import ipaddress
import os
import re
import time
from pathlib import Path
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException

import credentials_store
from console import command_errors, open_console
from credentials_store import CredentialsError

CONSOLE_HOST = os.environ.get("GNS3_HOST", "127.0.0.1")
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"

def ensure_dirs():
    LOG_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)


_CRYPTO_TIMEOUT = 180  # RSA 2048 key generation is slow on Dynamips


def _open_ios_console(host, port):
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        host = CONSOLE_HOST
    return open_console(host, port, lambda: credentials_store.get(credentials_store.LOCAL_KEY))


def _interface_matches(name, full_name):
    """'f1/0', 'Fa1/0' and 'FastEthernet1/0' all name the same interface."""
    a = re.match(r"^([A-Za-z-]+)\s*([\d/.:]+)$", name.strip())
    b = re.match(r"^([A-Za-z-]+)\s*([\d/.:]+)$", full_name.strip())
    if not a or not b:
        return False
    return a.group(2) == b.group(2) and b.group(1).lower().startswith(a.group(1).lower())


def interface_ip(brief_output, interface):
    """IP address of `interface` from 'show ip interface brief', or None."""
    for line in brief_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _interface_matches(interface, parts[0]):
            try:
                return str(ipaddress.ip_address(parts[1]))
            except ValueError:
                return None
    return None


def send_console_commands(host, port, commands, boot_timeout=300):
    ensure_dirs()
    con = None
    try:
        con = _open_ios_console(host, port)
        con.wake(boot_timeout)
        errors = []
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue  # waking the console and the login are handled by the agent
            if cmd.lower() in ("en", "enable"):
                con.enable()
                continue
            out = con.run(cmd, timeout=_CRYPTO_TIMEOUT if cmd.startswith("crypto key") else 60)
            errors += [f"{cmd}: {e}" for e in command_errors(out)]
        result = {
            "status": "error" if errors else "success",
            "prompt": con.prompt,
            "output": "\n".join(con.transcript),
        }
        if errors:
            result["error"] = "IOS rejected: " + "; ".join(errors)
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        if con:
            con.close()


def configure_initial_ssh(console_host, console_port, hostname, username, password, secret,
                          mgmt_interface=None, mgmt_ip=None, mgmt_mask=None, use_dhcp=False,
                          regenerate_keys=False, boot_timeout=300, dhcp_timeout=60):
    commands = [
        f"hostname {hostname}",
        "ip domain-name lab.local",
        "crypto key generate rsa modulus 2048",
        "ip ssh version 2",
        f"username {username} privilege 15 secret {password}",
        f"enable secret {secret}",
        "line vty 0 4",
        "login local",
        "transport input ssh",
        "exit",
    ]
    if mgmt_interface:
        commands.append(f"interface {mgmt_interface}")
        if use_dhcp:
            commands.append("ip address dhcp")
        elif mgmt_ip and mgmt_mask:
            commands.append(f"ip address {mgmt_ip} {mgmt_mask}")
        commands.append("no shutdown")
        commands.append("exit")

    ensure_dirs()
    con = None
    try:
        con = _open_ios_console(console_host, console_port)
        con.wake(boot_timeout)
        con.enable()
        con.run("terminal length 0")
        # Regenerating RSA keys changes the SSH host key, so every SSH client would warn
        # about a possible man-in-the-middle. Keep the keys when SSH already works.
        keys_kept = (not regenerate_keys and con.hostname == hostname
                     and "SSH Enabled" in con.run("show ip ssh"))
        if keys_kept:
            commands = [c for c in commands if not c.startswith("crypto key")]
        con.run("configure terminal")
        errors = []
        for cmd in commands:
            is_crypto = cmd.startswith("crypto key")
            out = con.run(cmd, timeout=_CRYPTO_TIMEOUT if is_crypto else 30, confirm=is_crypto)
            errors += [f"{cmd}: {e}" for e in command_errors(out)]
            if "Please create RSA keys" in out:
                errors.append(f"{cmd}: {out.strip()}")
        con.run("end")
        con.run("write memory", timeout=60, confirm=True)

        ssh_status = con.run("show ip ssh")
        ssh_enabled = "SSH Enabled - version 2.0" in ssh_status
        ip = None
        if mgmt_interface:
            deadline = time.monotonic() + (dhcp_timeout if use_dhcp else 0)
            while True:
                ip = interface_ip(con.run("show ip interface brief"), mgmt_interface)
                if ip or time.monotonic() >= deadline:
                    break
                time.sleep(5)

        problems = list(errors)
        if any("768 bits" in e or "RSA keys" in e for e in errors):
            problems.append("the existing RSA keys are too small for SSH v2; "
                            "run the setup again with regenerate_keys=true")
        if not ssh_enabled:
            problems.append("SSH v2 is not enabled after the configuration (check 'show ip ssh')")
        if mgmt_interface and not ip:
            problems.append(f"{mgmt_interface} has no IP address"
                            + (f" after waiting {dhcp_timeout}s for DHCP" if use_dhcp else ""))
        result = {
            "status": "error" if problems else "success",
            "hostname": hostname,
            "ssh_enabled": ssh_enabled,
            "mgmt_interface": mgmt_interface,
            "mgmt_ip": ip,
            "local_admin": username,
            "rsa_keys": "kept (SSH host key unchanged)" if keys_kept else "generated",
            "output": "\n".join(con.transcript),
        }
        if problems:
            result["error"] = "; ".join(problems)
        else:
            result["message"] = (f"SSH is enabled on {hostname}. Use host {ip} for SSH tools."
                                 if ip else f"SSH is enabled on {hostname}.")
        return result
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        if con:
            con.close()


def ssh_connect(host, username=None, password=None, secret=None, port=22):
    if username and password:
        options = [("explicit", {"username": username, "password": password, "secret": secret})]
    else:
        options = credentials_store.candidates(host)
    if not options:
        raise CredentialsError(
            f"No credentials for {host}. Ask the user to run the 'login' command "
            "(scope 'aaa', 'local' or the device IP). Never ask for passwords in the chat."
        )
    ensure_dirs()
    tried = []
    for label, creds in options:
        try:
            conn = ConnectHandler(
                device_type="cisco_ios",
                host=host,
                username=creds["username"],
                password=creds["password"],
                secret=creds.get("secret") or "",
                port=port,
                timeout=30,
                disabled_algorithms={
                    "pubkeys": ["rsa-sha2-256", "rsa-sha2-512"],
                },
            )
        except NetmikoAuthenticationException:
            tried.append(f"{label} ({creds['username']})")
            continue
        if not conn.check_enable_mode():
            conn.enable()
        return conn
    raise CredentialsError(
        f"SSH authentication failed for {host} with: {', '.join(tried)}. "
        "Ask the user to run the 'login' command with the correct credentials."
    )

def send_show(host, command, **ssh_kwargs):
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        output = conn.send_command(command)
        conn.disconnect()
        return {"status": "success", "host": host, "command": command, "output": output}
    except Exception as e:
        return {"status": "error", "host": host, "command": command, "error": str(e)}


def send_config(host, commands, **ssh_kwargs):
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        output = conn.send_config_set(commands)
        save = conn.send_command("write memory")
        conn.disconnect()
        return {"status": "success", "host": host, "commands_sent": commands, "output": output, "save": save}
    except Exception as e:
        return {"status": "error", "host": host, "commands": commands, "error": str(e)}


def configure_interface(host, interface, ip, mask, description=None, no_shutdown=True, **ssh_kwargs):
    commands = [f"interface {interface}"]
    if description:
        commands.append(f"description {description}")
    commands.append(f"ip address {ip} {mask}")
    if no_shutdown:
        commands.append("no shutdown")
    return send_config(host, commands, **ssh_kwargs)


def configure_subinterface(host, parent_interface, vlan_id, ip, mask, description=None, **ssh_kwargs):
    commands = [
        f"interface {parent_interface}",
        "no shutdown",
        f"interface {parent_interface}.{vlan_id}",
    ]
    if description:
        commands.append(f"description {description}")
    commands.append(f"encapsulation dot1Q {vlan_id}")
    commands.append(f"ip address {ip} {mask}")
    commands.append("no shutdown")
    return send_config(host, commands, **ssh_kwargs)


def configure_ospf(host, process_id, router_id, networks, passive_interfaces=None, **ssh_kwargs):
    commands = [
        f"router ospf {process_id}",
        f"router-id {router_id}",
    ]
    for net in networks:
        commands.append(f"network {net['network']} {net['wildcard']} area {net['area']}")
    if passive_interfaces:
        for iface in passive_interfaces:
            commands.append(f"passive-interface {iface}")
    return send_config(host, commands, **ssh_kwargs)


def configure_eigrp(host, as_number, router_id, networks, passive_interfaces=None, **ssh_kwargs):
    commands = [
        f"router eigrp {as_number}",
        f"eigrp router-id {router_id}",
        "no auto-summary",
    ]
    for net in networks:
        commands.append(f"network {net['network']} {net['wildcard']}")
    if passive_interfaces:
        for iface in passive_interfaces:
            commands.append(f"passive-interface {iface}")
    return send_config(host, commands, **ssh_kwargs)


def configure_rip(host, version, networks, passive_interfaces=None, **ssh_kwargs):
    commands = [
        "router rip",
        f"version {version}",
        "no auto-summary",
    ]
    for net in networks:
        commands.append(f"network {net}")
    if passive_interfaces:
        for iface in passive_interfaces:
            commands.append(f"passive-interface {iface}")
    return send_config(host, commands, **ssh_kwargs)

def configure_stp(host, mode="rapid-pvst", vlan_priorities=None, root_bridge_vlan=None, **ssh_kwargs):
    commands = [f"spanning-tree mode {mode}"]
    if root_bridge_vlan:
        commands.append(f"spanning-tree vlan {root_bridge_vlan} root primary")
    if vlan_priorities:
        for vlan, priority in vlan_priorities.items():
            commands.append(f"spanning-tree vlan {vlan} priority {priority}")
    return send_config(host, commands, **ssh_kwargs)

def configure_static_route(host, destination, mask, next_hop, **ssh_kwargs):
    commands = [f"ip route {destination} {mask} {next_hop}"]
    return send_config(host, commands, **ssh_kwargs)


def configure_acl(host, acl_name, acl_type, rules, apply_interface=None, direction=None, **ssh_kwargs):
    commands = [f"ip access-list {acl_type} {acl_name}"]
    seq = 10
    for rule in rules:
        action = rule["action"]
        protocol = rule["protocol"]
        src = rule["source"]
        dst = rule["destination"]

        if src == "any" and dst == "any":
            commands.append(f"{seq} {action} {protocol} any any")
        else:
            src_wc = rule.get("source_wildcard", "")
            dst_wc = rule.get("dest_wildcard", "")
            line = f"{seq} {action} {protocol} {src}"
            if src_wc:
                line += f" {src_wc}"
            line += f" {dst}"
            if dst_wc:
                line += f" {dst_wc}"
            if rule.get("port_operator") and rule.get("port"):
                line += f" {rule['port_operator']} {rule['port']}"
            commands.append(line)
        seq += 10

    if apply_interface and direction:
        commands.append(f"interface {apply_interface}")
        commands.append(f"ip access-group {acl_name} {direction}")

    return send_config(host, commands, **ssh_kwargs)


def remove_acl(host, acl_name, acl_type="extended", interface=None, direction=None, **ssh_kwargs):
    commands = []
    if interface and direction:
        commands.extend([
            f"interface {interface}",
            f"no ip access-group {acl_name} {direction}",
        ])
    commands.append(f"no ip access-list {acl_type} {acl_name}")
    return send_config(host, commands, **ssh_kwargs)


def backup_config(host, device_name, **ssh_kwargs):
    ensure_dirs()
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        config = conn.send_command("show running-config")
        conn.disconnect()

        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = BACKUP_DIR / f"{device_name}_{ts}.cfg"
        with open(path, "w") as f:
            f.write(config)

        return {"status": "success", "device": device_name, "backup_file": str(path)}
    except Exception as e:
        return {"status": "error", "device": device_name, "error": str(e)}


def get_interface_name(node_type, adapter, port):
    if "dynamips" in node_type:
        return f"FastEthernet{adapter}/{port}"
    elif "iou" in node_type:
        return f"Ethernet{adapter}/{port}"
    elif "qemu" in node_type:
        return f"GigabitEthernet{adapter}/{port}"
    return f"FastEthernet{adapter}/{port}"

def ping_from_device(host, target, count=3, **ssh_kwargs):
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        output = conn.send_command(f"ping {target} repeat {count}", read_timeout=30)
        conn.disconnect()
        success = "!" in output
        return {"status": "success", "host": host, "target": target, "reachable": success, "output": output}
    except Exception as e:
        return {"status": "error", "host": host, "target": target, "error": str(e)}


def collect_device_info(host, **ssh_kwargs):
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        info = {}
        info["interfaces"] = conn.send_command("show ip interface brief")
        info["routes"] = conn.send_command("show ip route")
        info["ospf_neighbors"] = conn.send_command("show ip ospf neighbor")
        info["acls"] = conn.send_command("show access-lists")
        info["running_config"] = conn.send_command("show running-config")
        conn.disconnect()
        return {"status": "success", "host": host, "info": info}
    except Exception as e:
        return {"status": "error", "host": host, "error": str(e)}