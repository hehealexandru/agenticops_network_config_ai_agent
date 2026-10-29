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
    backup_config, configure_stp, send_console_commands, get_interface_name,
    ping_from_device, collect_device_info,
    save_credentials,
)

gns3 = GNS3Client()

TOOL_DEFINITIONS = [
    {
        "name": "gns3_list_projects",
        "description": "Lists all GNS3 projects. Returns the name, ID and status of each project.",
        "input_schema": {
            "type": "object",
            "properties": {},
        }
    },
    {
        "name": "gns3_analyze_topology",
        "description": (
            "Analyzes the topology of the current (or given) GNS3 project. "
            "Returns: all routers, switches, endpoints (VM/Docker), "
            "the links between them, and which router is connected to NAT. "
            "ALWAYS use this first to understand the network."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "GNS3 project name (optional, uses the open project if missing)"
                }
            },
        }
    },
    {
        "name": "gns3_start_nodes",
        "description": "Starts all nodes or a specific node in the GNS3 project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_name": {
                    "type": "string",
                    "description": "Name of the node to start (optional, starts all if missing)"
                }
            },
        }
    },
    {
        "name": "gns3_stop_nodes",
        "description": "Stops all nodes or a specific node in the GNS3 project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_name": {
                    "type": "string",
                    "description": "Name of the node to stop (optional, stops all if missing)"
                }
            },
        }
    },
    {
        "name": "configure_ssh_on_device",
        "description": (
            "Configures SSH on a router through the GNS3 Telnet console. "
            "Sets the hostname, credentials (admin/cisco), generates RSA keys, "
            "and optionally configures the management interface with a static IP or DHCP. "
            "Use this BEFORE any other SSH configuration."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Device name in the topology (e.g. R1, R2)"
                },
                "mgmt_interface": {
                    "type": "string",
                    "description": "Management interface (e.g. FastEthernet2/0) - optional"
                },
                "mgmt_ip": {
                    "type": "string",
                    "description": "Management IP (optional, if not DHCP)"
                },
                "mgmt_mask": {
                    "type": "string",
                    "description": "Subnet mask (e.g. 255.255.255.0)"
                },
                "use_dhcp": {
                    "type": "boolean",
                    "description": "Use DHCP on the management interface (true/false)"
                }
            },
            "required": ["device_name"]
        }
    },
    {
        "name": "send_show_command",
        "description": (
            "Sends a show command (read-only) to a device over SSH. "
            "Use it for checks: show ip ospf neighbor, show access-lists, "
            "show ip interface brief, show running-config, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Device IP address or hostname"
                },
                "command": {
                    "type": "string",
                    "description": "Show command to run"
                }
            },
            "required": ["host", "command"]
        }
    },
    {
        "name": "send_config_commands",
        "description": (
            "Sends configuration commands to a device over SSH. "
            "Commands are sent in configure terminal. Saves automatically with write memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Device IP address"
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of IOS commands"
                }
            },
            "required": ["host", "commands"]
        }
    },
    {
        "name": "configure_ip_on_interface",
        "description": "Configures an IP address on a router interface.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address"},
                "interface": {"type": "string", "description": "Interface name (e.g. FastEthernet1/0)"},
                "ip": {"type": "string", "description": "IP address"},
                "mask": {"type": "string", "description": "Mask (e.g. 255.255.255.0)"},
                "description": {"type": "string", "description": "Optional description"}
            },
            "required": ["host", "interface", "ip", "mask"]
        }
    },
    {
        "name": "configure_vlan_subinterface",
        "description": "Creates a VLAN sub-interface (router-on-a-stick) with dot1Q encapsulation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address"},
                "parent_interface": {"type": "string", "description": "Physical interface (e.g. FastEthernet0/0)"},
                "vlan_id": {"type": "integer", "description": "VLAN ID"},
                "ip": {"type": "string", "description": "IP address for the sub-interface"},
                "mask": {"type": "string", "description": "Subnet mask"},
                "description": {"type": "string", "description": "Optional description"}
            },
            "required": ["host", "parent_interface", "vlan_id", "ip", "mask"]
        }
    },
    {
        "name": "configure_routing_protocol",
        "description": (
            "Configures a routing protocol on a router. "
            "Supported protocols: ospf, eigrp, rip, static."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address"},
                "protocol": {
                    "type": "string",
                    "enum": ["ospf", "eigrp", "rip", "static"],
                    "description": "Routing protocol"
                },
                "ospf_process_id": {"type": "integer", "description": "OSPF process ID (OSPF only)"},
                "router_id": {"type": "string", "description": "Router ID (OSPF/EIGRP only)"},
                "as_number": {"type": "integer", "description": "AS number (EIGRP only)"},
                "rip_version": {"type": "integer", "description": "RIP version 1 or 2"},
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
                    "description": "Networks to advertise"
                },
                "static_destination": {"type": "string", "description": "Destination network (static only)"},
                "static_mask": {"type": "string", "description": "Mask (static only)"},
                "static_next_hop": {"type": "string", "description": "Next hop (static only)"},
                "passive_interfaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Passive interfaces"
                }
            },
            "required": ["host", "protocol"]
        }
    },
    {
        "name": "configure_access_list",
        "description": (
            "Creates or modifies an Access Control List (ACL) and optionally applies it to an interface. "
            "Can block: icmp, tcp, udp, ip or any protocol. "
            "Can specify ports for tcp/udp (e.g. eq 80, eq 23, eq 443)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address"},
                "acl_name": {"type": "string", "description": "ACL name"},
                "acl_type": {
                    "type": "string",
                    "enum": ["extended", "standard"],
                    "description": "ACL type"
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
                    "description": "ACL rules"
                },
                "apply_interface": {"type": "string", "description": "Interface to apply it to (optional)"},
                "direction": {"type": "string", "enum": ["in", "out"], "description": "Direction (in/out)"}
            },
            "required": ["host", "acl_name", "acl_type", "rules"]
        }
    },
    {
        "name": "remove_access_list",
        "description": "Removes an ACL from a device.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "acl_name": {"type": "string"},
                "acl_type": {"type": "string", "enum": ["extended", "standard"]},
                "interface": {"type": "string", "description": "Interface to remove it from (optional)"},
                "direction": {"type": "string", "enum": ["in", "out"]}
            },
            "required": ["host", "acl_name"]
        }
    },
    {
        "name": "backup_device_config",
        "description": "Saves the running-config to a backup file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address"},
                "device_name": {"type": "string", "description": "Device name (used for the file name)"}
            },
            "required": ["host", "device_name"]
        }
    },
    {
        "name": "send_console_raw",
        "description": (
            "Sends raw commands through the GNS3 Telnet console. "
            "Use only for devices that do not have SSH configured."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Device name in the topology"
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Commands to send"
                }
            },
            "required": ["device_name", "commands"]
        }
    },
    {
        "name": "configure_stp",
        "description": (
            "Configures Spanning Tree Protocol on a Layer 2/3 switch. "
            "Supported modes: pvst, rapid-pvst, mst. "
            "Can set the root bridge for a specific VLAN."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Switch IP address"},
                "mode": {
                    "type": "string",
                    "enum": ["pvst", "rapid-pvst", "mst"],
                    "description": "STP mode (default: rapid-pvst)"
                },
                "root_bridge_vlan": {
                    "type": "string",
                    "description": "VLAN for which the switch becomes root bridge"
                },
                "vlan_priorities": {
                    "type": "object",
                    "description": "VLAN:priority dictionary for custom priorities"
                }
            },
            "required": ["host"]
        }
    },
    {
        
        "name": "docker_exec_command",
        "description": (
            "Runs a command in a Docker container from the GNS3 topology. "
            "Use it to configure IP, gateway, test connectivity (ping), "
            "install packages, or run any Linux command in a container. "
            "Examples: 'ip addr add 192.168.1.10/24 dev eth0', 'ping -c 3 10.0.1.1'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Docker container name in the topology (e.g. alpine-1)"
                },
                "command": {
                    "type": "string",
                    "description": "Linux command to run in the container"
                }
            },
            "required": ["device_name", "command"]
        }
    },

    {
        "name": "ping_from_router",
        "description": (
            "Pings an IP address from a Cisco router. "
            "Use it to test connectivity hop by hop when troubleshooting. "
            "Returns whether the destination is reachable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "IP address of the router to ping from"},
                "target": {"type": "string", "description": "Destination IP address to ping"},
                "count": {"type": "integer", "description": "Number of packets (default: 3)"}
            },
            "required": ["host", "target"]
        }
    },
    {
        "name": "collect_device_info",
        "description": (
            "Collects full information from a router: interfaces, routing table, "
            "OSPF neighbors, ACLs and running-config. Use it for troubleshooting "
            "or for generating documentation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Router IP address"}
            },
            "required": ["host"]
        }
    },
    {
        "name": "generate_documentation",
        "description": (
            "Generates complete network documentation in a markdown file. "
            "Includes: the topology, a table of devices and IPs, connections, "
            "routing protocols, ACLs, endpoints. "
            "Saves the file in /app/docs/ or the current directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "File name (default: network_documentation.md)"
                	}
            },
        }
    },
    {
        "name": "update_credentials",
        "description": (
            "Saves the SSH credentials for a device. "
            "Use it after the user changes the password on a router. "
            "The agent will use these credentials for future SSH connections."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Device IP address"},
                "username": {"type": "string", "description": "SSH username"},
                "password": {"type": "string", "description": "SSH password"},
                "secret": {"type": "string", "description": "Enable secret (optional, defaults to the password)"}
            },
            "required": ["host", "username", "password"]
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
    return None, None, {"error": f"Device '{device_name}' not found"}


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
            return gns3.get_topology_summary(project["project_id"])

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
                return {"status": "success", "message": f"{node_name} started"}
            else:
                gns3.start_all(pid)
                return {"status": "success", "message": "All nodes started"}

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
                return {"status": "success", "message": f"{node_name} stopped"}
            else:
                gns3.stop_all(pid)
                return {"status": "success", "message": "All nodes stopped"}

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
            return {"status": "error", "error": f"Unknown protocol: {protocol}"}

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

        elif tool_name == "configure_stp":
            return configure_stp(
                tool_input["host"],
                tool_input.get("mode", "rapid-pvst"),
                tool_input.get("vlan_priorities"),
                tool_input.get("root_bridge_vlan"),
            )

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

        elif tool_name == "docker_exec_command":
            device_name = tool_input["device_name"]
            project_id, node, err = _find_node(device_name)
            if err:
                return err
            if node.get("node_type") != "docker":
                return {"status": "error", "error": f"{device_name} is not a Docker container"}
            return gns3.docker_exec(project_id, node["node_id"], tool_input["command"])

        elif tool_name == "ping_from_router":
            return ping_from_device(
                tool_input["host"],
                tool_input["target"],
                tool_input.get("count", 3),
            )

        elif tool_name == "collect_device_info":
            return collect_device_info(tool_input["host"])

        elif tool_name == "generate_documentation":
            project = gns3.get_open_project()
            if isinstance(project, dict) and "error" in project:
                return project
            topo = gns3.analyze_topology(project["project_id"])
            if "error" in topo:
                return topo

            filename = tool_input.get("filename", "network_documentation.md")
            lines = []
            lines.append("# Network Documentation")
            lines.append(f"## Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

            lines.append("## Topology Summary")
            s = topo["summary"]
            lines.append(f"- Routers: {s['total_routers']}")
            lines.append(f"- Switches: {s['total_switches']}")
            lines.append(f"- Endpoints: {s['total_endpoints']}")
            lines.append(f"- NAT/Cloud: {s['total_nat_clouds']}")
            lines.append(f"- Links: {s['total_links']}\n")

            lines.append("## Devices\n")
            lines.append("| Device | Type | Classification | Status |")
            lines.append("|--------|------|---------------|--------|")
            for category in ["routers", "switches", "endpoints", "nat_clouds"]:
                for d in topo["devices"][category]:
                    lines.append(f"| {d['name']} | {d['node_type']} | {d['classification']} | {d['status']} |")

            lines.append("\n## Connections\n")
            lines.append("| From | Interface | To | Interface |")
            lines.append("|------|----------|-----|----------|")
            for c in topo["connections"]:
                lines.append(f"| {c['node1']} | {c['node1_interface']} | {c['node2']} | {c['node2_interface']} |")

            lines.append(f"\n## Management")
            lines.append(f"- NAT Router: {topo['management']['nat_connected_router']}")
            lines.append(f"- NAT Interface: {topo['management']['nat_interface']}\n")

            docs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
            os.makedirs(docs_dir, exist_ok=True)
            filepath = os.path.join(docs_dir, filename)
            with open(filepath, "w") as f:
                f.write("\n".join(lines))

            return {"status": "success", "output": f"Documentation saved to {filepath}", "content": "\n".join(lines)}
            
        elif tool_name == "update_credentials":
            return save_credentials(
                tool_input["host"],
                tool_input["username"],
                tool_input["password"],
                tool_input.get("secret"),
            )
            
        return {"status": "error", "error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
