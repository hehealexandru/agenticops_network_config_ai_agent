#!/usr/bin/env python3

import os
import telnetlib
import time
from pathlib import Path
from netmiko import ConnectHandler

CONSOLE_HOST = os.environ.get("GNS3_HOST", "127.0.0.1")
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"


def ensure_dirs():
    LOG_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)


def send_console_commands(host, port, commands, wait=1):
    ensure_dirs()
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        host = CONSOLE_HOST
    output_lines = []
    try:
        tn = telnetlib.Telnet(host, port, timeout=15)
        time.sleep(1)
        tn.write(b"\r\n")
        time.sleep(1)

        for cmd in commands:
            tn.write(cmd.encode("ascii") + b"\r\n")
            time.sleep(wait)
            result = tn.read_very_eager().decode("ascii", errors="ignore")
            output_lines.append(result)

        tn.close()
        return {"status": "success", "output": "\n".join(output_lines)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def configure_initial_ssh(console_host, console_port, hostname, mgmt_interface=None, mgmt_ip=None, mgmt_mask=None, use_dhcp=False):
    """Configurează un router de la zero prin consolă Telnet: hostname, SSH, credențiale."""
    commands = [
        "",
        "enable",
        "conf t",
        f"hostname {hostname}",
        "ip domain-name lab.local",
        "crypto key generate rsa modulus 2048",
        "username admin privilege 15 secret cisco",
        "enable secret cisco",
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

    commands.extend(["end", "write"])

    return send_console_commands(console_host, console_port, commands, wait=2)


def ssh_connect(host, username="admin", password="cisco", secret="cisco", port=22):
    ensure_dirs()
    conn = ConnectHandler(
        device_type="cisco_ios",
        host=host,
        username=username,
        password=password,
        secret=secret,
        port=port,
        timeout=30,
        disabled_algorithms={
            "pubkeys": ["rsa-sha2-256", "rsa-sha2-512"],
        },
    )
    conn.enable()
    return conn

def send_show(host, command, **ssh_kwargs):
    """Trimite o comandă show și returnează output-ul."""
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        output = conn.send_command(command)
        conn.disconnect()
        return {"status": "success", "host": host, "command": command, "output": output}
    except Exception as e:
        return {"status": "error", "host": host, "command": command, "error": str(e)}


def send_config(host, commands, **ssh_kwargs):
    """Trimite comenzi de configurare și salvează."""
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        output = conn.send_config_set(commands)
        save = conn.send_command("write memory")
        conn.disconnect()
        return {"status": "success", "host": host, "commands_sent": commands, "output": output, "save": save}
    except Exception as e:
        return {"status": "error", "host": host, "commands": commands, "error": str(e)}


def configure_interface(host, interface, ip, mask, description=None, no_shutdown=True, **ssh_kwargs):
    """Configurează o interfață cu IP."""
    commands = [f"interface {interface}"]
    if description:
        commands.append(f"description {description}")
    commands.append(f"ip address {ip} {mask}")
    if no_shutdown:
        commands.append("no shutdown")
    return send_config(host, commands, **ssh_kwargs)


def configure_subinterface(host, parent_interface, vlan_id, ip, mask, description=None, **ssh_kwargs):
    """Configurează o sub-interfață VLAN (router-on-a-stick)."""
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
    """Configurează OSPF."""
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
    """Configurează EIGRP."""
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
    """Configurează RIPv2."""
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
    """Adaugă o rută statică."""
    commands = [f"ip route {destination} {mask} {next_hop}"]
    return send_config(host, commands, **ssh_kwargs)


def configure_acl(host, acl_name, acl_type, rules, apply_interface=None, direction=None, **ssh_kwargs):
    """Configurează un ACL extins și opțional îl aplică pe o interfață."""
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
    """Șterge un ACL."""
    commands = []
    if interface and direction:
        commands.extend([
            f"interface {interface}",
            f"no ip access-group {acl_name} {direction}",
        ])
    commands.append(f"no ip access-list {acl_type} {acl_name}")
    return send_config(host, commands, **ssh_kwargs)


def backup_config(host, device_name, **ssh_kwargs):
    """Face backup la running-config."""
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
    """Convertește adapter/port GNS3 în nume interfață IOS."""
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
