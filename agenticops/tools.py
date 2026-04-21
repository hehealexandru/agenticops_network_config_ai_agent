#!/usr/bin/env python3

import os
import json
from pathlib import Path

from gns3_client import GNS3Client
from net_config import (
    send_show, send_config, configure_initial_ssh,
    configure_interface, configure_subinterface,
    configure_ospf, configure_eigrp, configure_rip,
    configure_static_route, configure_acl, remove_acl,
    backup_config, send_console_commands, get_interface_name,
)

gns3 = GNS3Client()

TOOL_DEFINITIONS = [
    {
        "name": "gns3_list_projects",
        "description": "Listează toate proiectele din GNS3. Returnează numele, ID-ul și starea fiecărui proiect.",
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    },
    {
        "name": "gns3_analyze_topology",
        "description": (
            "Analizează topologia proiectului GNS3 curent (sau specificat). "
            "Returnează: toate routerele, switch-urile, endpoint-urile (VM/Docker), "
            "legăturile dintre ele, și care router e conectat la NAT. "
            "Folosește ÎNTOTDEAUNA asta la început pentru a înțelege rețeaua."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Numele proiectului GNS3 (opțional, folosește proiectul deschis dacă lipsește)"
                }
            },
        }
    },
    {
        "name": "gns3_start_nodes",
        "description": "Pornește toate nodurile sau un nod specific din proiectul GNS3.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_name": {
                    "type": "string",
                    "description": "Numele nodului de pornit (opțional, pornește toate dacă lipsește)"
                }
            },
        }
    },
    {
        "name": "gns3_stop_nodes",
        "description": "Oprește toate nodurile sau un nod specific din proiectul GNS3.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_name": {
                    "type": "string",
                    "description": "Numele nodului de oprit (opțional, oprește toate dacă lipsește)"
                }
            },
        }
    },
    {
        "name": "configure_ssh_on_device",
        "description": (
            "Configurează SSH pe un router prin consola Telnet GNS3. "
            "Setează hostname, credențiale (admin/cisco), generează chei RSA, "
            "și opțional configurează interfața de management cu IP sau DHCP. "
            "Folosește asta ÎNAINTE de orice altă configurare SSH."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Numele dispozitivului din topologie (ex: R1, R2)"
                },
                "mgmt_interface": {
                    "type": "string",
                    "description": "Interfața de management (ex: FastEthernet2/0) - opțional"
                },
                "mgmt_ip": {
                    "type": "string",
                    "description": "IP-ul de management (opțional, dacă nu e DHCP)"
                },
                "mgmt_mask": {
                    "type": "string",
                    "description": "Masca de rețea (ex: 255.255.255.0)"
                },
                "use_dhcp": {
                    "type": "boolean",
                    "description": "Folosește DHCP pe interfața de management (true/false)"
                }
            },
            "required": ["device_name"]
        }
    },
    {
        "name": "send_show_command",
        "description": (
            "Trimite o comandă show (read-only) pe un echipament prin SSH. "
            "Folosește pentru verificări: show ip ospf neighbor, show access-lists, "
            "show ip interface brief, show running-config, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "IP-ul sau hostname-ul dispozitivului"
                },
                "command": {
                    "type": "string",
                    "description": "Comanda show de executat"
                }
            },
            "required": ["host", "command"]
        }
    },
    {
        "name": "send_config_commands",
        "description": (
            "Trimite comenzi de configurare pe un echipament prin SSH. "
            "Comenzile sunt trimise în configure terminal. Salvează automat cu write memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "IP-ul dispozitivului"
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista de comenzi IOS"
                }
            },
            "required": ["host", "commands"]
        }
    },
    {
        "name": "configure_ip_on_interface",
        "description": "Configurează o adresă IP pe o interfață a unui router.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "IP-ul dispozitivului"},
                "interface": {"type": "string", "description": "Numele interfeței (ex: FastEthernet1/0)"},
                "ip": {"type": "string", "description": "Adresa IP"},
                "mask": {"type": "string", "description": "Masca (ex: 255.255.255.0)"},
                "description": {"type": "string", "description": "Descriere opțională"}
            },
            "required": ["host", "interface", "ip", "mask"]
        }
    },
    {
        "name": "configure_vlan_subinterface",
        "description": "Creează o sub-interfață VLAN (router-on-a-stick) cu encapsulation dot1Q.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "IP-ul dispozitivului"},
                "parent_interface": {"type": "string", "description": "Interfața fizică (ex: FastEthernet0/0)"},
                "vlan_id": {"type": "integer", "description": "ID-ul VLAN-ului"},
                "ip": {"type": "string", "description": "Adresa IP pentru sub-interfață"},
                "mask": {"type": "string", "description": "Masca de rețea"},
                "description": {"type": "string", "description": "Descriere opțională"}
            },
            "required": ["host", "parent_interface", "vlan_id", "ip", "mask"]
        }
    },
    {
        "name": "configure_routing_protocol",
        "description": (
            "Configurează un protocol de rutare pe un router. "
            "Protocoale suportate: ospf, eigrp, rip, static."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "IP-ul dispozitivului"},
                "protocol": {
                    "type": "string",
                    "enum": ["ospf", "eigrp", "rip", "static"],
                    "description": "Protocolul de rutare"
                },
                "ospf_process_id": {"type": "integer", "description": "OSPF process ID (doar pentru OSPF)"},
                "router_id": {"type": "string", "description": "Router ID (doar OSPF/EIGRP)"},
                "as_number": {"type": "integer", "description": "AS number (doar EIGRP)"},
                "rip_version": {"type": "integer", "description": "RIP version 1 sau 2"},
                "networks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "network": {"type": "string"},
                            "wildcard": {"type": "string"},
                            "area": {"type": "integer"}
                        }
                    },
                    "description": "Rețelele de anunțat"
                },
                "static_destination": {"type": "string", "description": "Rețea destinație (doar static)"},
                "static_mask": {"type": "string", "description": "Masca (doar static)"},
                "static_next_hop": {"type": "string", "description": "Next hop (doar static)"},
                "passive_interfaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Interfețe pasive"
                }
            },
            "required": ["host", "protocol"]
        }
    },
    {
        "name": "configure_access_list",
        "description": (
            "Creează sau modifică un Access Control List (ACL) și opțional îl aplică pe o interfață. "
            "Poate bloca: icmp, tcp, udp, ip sau orice protocol. "
            "Poate specifica porturi pentru tcp/udp (ex: eq 80, eq 23, eq 443)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "IP-ul dispozitivului"},
                "acl_name": {"type": "string", "description": "Numele ACL-ului"},
                "acl_type": {
                    "type": "string",
                    "enum": ["extended", "standard"],
                    "description": "Tipul ACL-ului"
                },
                "rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["permit", "deny"]},
                            "protocol": {"type": "string"},
                            "source": {"type": "string"},
                            "source_wildcard": {"type": "string"},
                            "destination": {"type": "string"},
                            "dest_wildcard": {"type": "string"},
                            "port_operator": {"type": "string"},
                            "port": {"type": "string"}
                        },
                        "required": ["action", "protocol", "source", "destination"]
                    },
                    "description": "Regulile ACL"
                },
                "apply_interface": {"type": "string", "description": "Interfața pe care se aplică (opțional)"},
                "direction": {"type": "string", "enum": ["in", "out"], "description": "Direcția (in/out)"}
            },
            "required": ["host", "acl_name", "acl_type", "rules"]
        }
    },
    {
        "name": "remove_access_list",
        "description": "Șterge un ACL de pe un dispozitiv.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "acl_name": {"type": "string"},
                "acl_type": {"type": "string", "enum": ["extended", "standard"]},
                "interface": {"type": "string", "description": "Interfața de pe care se scoate (opțional)"},
                "direction": {"type": "string", "enum": ["in", "out"]}
            },
            "required": ["host", "acl_name"]
        }
    },
    {
        "name": "backup_device_config",
        "description": "Salvează running-config într-un fișier de backup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "IP-ul dispozitivului"},
                "device_name": {"type": "string", "description": "Numele dispozitivului (pt numele fișierului)"}
            },
            "required": ["host", "device_name"]
        }
    },
    {
        "name": "send_console_raw",
        "description": (
            "Trimite comenzi raw prin consola Telnet GNS3. "
            "Folosește doar pentru echipamente care nu au SSH configurat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Numele dispozitivului din topologie"
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Comenzile de trimis"
                }
            },
            "required": ["device_name", "commands"]
        }
    },
]


def _find_node(device_name):
    project = gns3.get_open_project()
    if "error" in project:
        return None, None, project
    nodes = gns3.get_nodes(project["project_id"])
    if isinstance(nodes, dict) and "error" in nodes:
        return None, None, nodes
    for n in nodes:
        if n["name"].lower() == device_name.lower():
            return project["project_id"], n, None
    return None, None, {"error": f"Dispozitiv '{device_name}' nu a fost găsit"}


def execute_tool(tool_name, tool_input):
    try:
        if tool_name == "gns3_list_projects":
            projects = gns3.get_projects()
            if isinstance(projects, dict) and "error" in projects:
                return projects
            return {"status": "success", "projects": [
                {"name": p["name"], "project_id": p["project_id"], "status": p.get("status", "?")}
                for p in projects
            ]}

        elif tool_name == "gns3_analyze_topology":
            project_name = tool_input.get("project_name")
            if project_name:
                project = gns3.get_project_by_name(project_name)
            else:
                project = gns3.get_open_project()
            if isinstance(project, dict) and "error" in project:
                return project
            result = gns3.analyze_topology(project["project_id"])
            if isinstance(result, dict) and "error" in result:
                return result
            return {"status": "success", "topology": result}

        elif tool_name == "gns3_start_nodes":
            node_name = tool_input.get("node_name")
            project = gns3.get_open_project()
            if isinstance(project, dict) and "error" in project:
                return project
            pid = project["project_id"]
            if node_name:
                _, node, err = _find_node(node_name)
                if err:
                    return err
                gns3.start_node(pid, node["node_id"])
                return {"status": "success", "message": f"{node_name} pornit"}
            else:
                gns3.start_all(pid)
                return {"status": "success", "message": "Toate nodurile pornite"}

        elif tool_name == "gns3_stop_nodes":
            node_name = tool_input.get("node_name")
            project = gns3.get_open_project()
            if isinstance(project, dict) and "error" in project:
                return project
            pid = project["project_id"]
            if node_name:
                _, node, err = _find_node(node_name)
                if err:
                    return err
                gns3.stop_node(pid, node["node_id"])
                return {"status": "success", "message": f"{node_name} oprit"}
            else:
                gns3.stop_all(pid)
                return {"status": "success", "message": "Toate nodurile oprite"}

        elif tool_name == "configure_ssh_on_device":
            device_name = tool_input["device_name"]
            _, node, err = _find_node(device_name)
            if err:
                return err
            return configure_initial_ssh(
                console_host=os.environ.get("GNS3_HOST", node.get("console_host", "127.0.0.1")),
                console_port=node["console"],
                hostname=device_name,
                mgmt_interface=tool_input.get("mgmt_interface"),
                mgmt_ip=tool_input.get("mgmt_ip"),
                mgmt_mask=tool_input.get("mgmt_mask"),
                use_dhcp=tool_input.get("use_dhcp", False),
            )

        elif tool_name == "send_show_command":
            return send_show(tool_input["host"], tool_input["command"])

        elif tool_name == "send_config_commands":
            return send_config(tool_input["host"], tool_input["commands"])

        elif tool_name == "configure_ip_on_interface":
            return configure_interface(
                tool_input["host"], tool_input["interface"],
                tool_input["ip"], tool_input["mask"],
                description=tool_input.get("description"),
            )

        elif tool_name == "configure_vlan_subinterface":
            return configure_subinterface(
                tool_input["host"], tool_input["parent_interface"],
                tool_input["vlan_id"], tool_input["ip"], tool_input["mask"],
                description=tool_input.get("description"),
            )

        elif tool_name == "configure_routing_protocol":
            host = tool_input["host"]
            protocol = tool_input["protocol"]
            passive = tool_input.get("passive_interfaces")

            if protocol == "ospf":
                return configure_ospf(
                    host,
                    tool_input.get("ospf_process_id", 1),
                    tool_input.get("router_id", "1.1.1.1"),
                    tool_input.get("networks", []),
                    passive,
                )
            elif protocol == "eigrp":
                return configure_eigrp(
                    host,
                    tool_input.get("as_number", 100),
                    tool_input.get("router_id", "1.1.1.1"),
                    tool_input.get("networks", []),
                    passive,
                )
            elif protocol == "rip":
                networks = [n.get("network", n) if isinstance(n, dict) else n for n in tool_input.get("networks", [])]
                return configure_rip(
                    host,
                    tool_input.get("rip_version", 2),
                    networks,
                    passive,
                )
            elif protocol == "static":
                return configure_static_route(
                    host,
                    tool_input["static_destination"],
                    tool_input["static_mask"],
                    tool_input["static_next_hop"],
                )
            return {"status": "error", "error": f"Protocol necunoscut: {protocol}"}

        elif tool_name == "configure_access_list":
            return configure_acl(
                tool_input["host"], tool_input["acl_name"],
                tool_input["acl_type"], tool_input["rules"],
                tool_input.get("apply_interface"),
                tool_input.get("direction"),
            )

        elif tool_name == "remove_access_list":
            return remove_acl(
                tool_input["host"], tool_input["acl_name"],
                tool_input.get("acl_type", "extended"),
                tool_input.get("interface"),
                tool_input.get("direction"),
            )

        elif tool_name == "backup_device_config":
            return backup_config(tool_input["host"], tool_input["device_name"])

        elif tool_name == "send_console_raw":
            device_name = tool_input["device_name"]
            _, node, err = _find_node(device_name)
            if err:
                return err
            return send_console_commands(
                node.get("console_host", "127.0.0.1"),
                node["console"],
                tool_input["commands"],
            )

        return {"status": "error", "error": f"Tool necunoscut: {tool_name}"}

    except Exception as e:
        return {"status": "error", "error": str(e)}
