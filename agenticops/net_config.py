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

def security_audit(host, device_name, **ssh_kwargs):
    try:
        conn = ssh_connect(host, **ssh_kwargs)
        
        running_config = conn.send_command("show running-config")
        ssh_status = conn.send_command("show ip ssh")
        vty_config = conn.send_command("show running-config | section line vty")
        interfaces = conn.send_command("show ip interface brief")
        cdp_status = conn.send_command("show cdp")
        http_status = conn.send_command("show running-config | include ip http")
        logging_status = conn.send_command("show running-config | include logging")
        snmp_status = conn.send_command("show running-config | include snmp")
        console_config = conn.send_command("show running-config | section line con")
        
        conn.disconnect()
        
        findings = []
        
        if "password" in running_config and "secret" not in running_config.split("password")[0][-20:]:
            findings.append({"severity": "CRITIC", "issue": "Parole în plaintext detectate", "fix": "Folosește 'enable secret' și 'username X secret Y' în loc de 'password'"})
        
        if "version 1" in ssh_status and "version 2" not in ssh_status:
            findings.append({"severity": "CRITIC", "issue": "SSH versiune 1 activă (nesigură)", "fix": "ip ssh version 2"})
        elif "1.99" in ssh_status:
            findings.append({"severity": "WARNING", "issue": "SSH versiune 1.99 (suportă și v1)", "fix": "ip ssh version 2"})
        
        if "access-class" not in vty_config:
            findings.append({"severity": "WARNING", "issue": "Linii VTY fără ACL", "fix": "access-class <ACL> in pe line vty 0 4"})
        
        if "exec-timeout" not in vty_config or "exec-timeout 0 0" in vty_config:
            findings.append({"severity": "WARNING", "issue": "VTY fără exec-timeout (sesiuni rămân deschise)", "fix": "exec-timeout 5 0 pe line vty 0 4"})
        
        if "exec-timeout" not in console_config or "exec-timeout 0 0" in console_config:
            findings.append({"severity": "WARNING", "issue": "Consolă fără exec-timeout", "fix": "exec-timeout 5 0 pe line con 0"})
        
        if "CDP is running" in cdp_status or "% CDP is not" not in cdp_status:
            findings.append({"severity": "WARNING", "issue": "CDP activat (expune informații despre rețea)", "fix": "no cdp run"})
        
        if "ip http server" in http_status and "no ip http server" not in http_status:
            findings.append({"severity": "WARNING", "issue": "HTTP server activ (nesecurizat)", "fix": "no ip http server"})
        
        if "no ip http secure-server" in http_status or "ip http secure-server" not in http_status:
            findings.append({"severity": "INFO", "issue": "HTTPS server inactiv", "fix": "ip http secure-server"})
        
        if not logging_status.strip():
            findings.append({"severity": "WARNING", "issue": "Logging dezactivat", "fix": "logging buffered 16384"})
        
        if "snmp" in snmp_status.lower():
            if "public" in snmp_status or "private" in snmp_status:
                findings.append({"severity": "CRITIC", "issue": "SNMP cu community string default (public/private)", "fix": "Schimbă community string-ul SNMP"})
        
        if "no service password-encryption" in running_config or "service password-encryption" not in running_config:
            findings.append({"severity": "WARNING", "issue": "Password encryption dezactivat", "fix": "service password-encryption"})
        
        if "no service timestamps" in running_config:
            findings.append({"severity": "INFO", "issue": "Timestamps dezactivate pe loguri", "fix": "service timestamps log datetime msec"})
        
        iface_lines = interfaces.splitlines()
        for line in iface_lines:
            if "up" in line.lower() and "unassigned" in line.lower():
                iface_name = line.split()[0]
                findings.append({"severity": "INFO", "issue": f"Interfața {iface_name} e up dar fără IP", "fix": f"shutdown pe {iface_name} dacă nu e folosită"})
        
        critical = len([f for f in findings if f["severity"] == "CRITIC"])
        warnings = len([f for f in findings if f["severity"] == "WARNING"])
        info = len([f for f in findings if f["severity"] == "INFO"])
        total = len(findings)
        max_score = 10
        score = max(0, max_score - (critical * 3) - (warnings * 1))
        
        report = {
            "device": device_name,
            "host": host,
            "score": f"{score}/{max_score}",
            "summary": f"{critical} critice, {warnings} warnings, {info} info",
            "findings": findings,
        }
        
        return {"status": "success", "audit": report}
    except Exception as e:
        return {"status": "error", "host": host, "error": str(e)}
