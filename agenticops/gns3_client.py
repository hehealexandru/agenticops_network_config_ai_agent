#!/usr/bin/env python3

import requests
import json


class GNS3Client:
    def __init__(self, host="127.0.0.1", port=3080):
        self.base_url = f"http://{host}:{port}/v2"
        self.session = requests.Session()

    def _get(self, endpoint):
        try:
            r = self.session.get(f"{self.base_url}{endpoint}", timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}

    def _post(self, endpoint, data=None):
        try:
            r = self.session.post(f"{self.base_url}{endpoint}", json=data or {}, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}

    def get_projects(self):
        return self._get("/projects")

    def get_project_by_name(self, name):
        projects = self.get_projects()
        if isinstance(projects, dict) and "error" in projects:
            return projects
        for p in projects:
            if p["name"].lower() == name.lower():
                return p
        return {"error": f"Proiect '{name}' nu a fost găsit. Disponibile: {[p['name'] for p in projects]}"}

    def get_open_project(self):
        projects = self.get_projects()
        if isinstance(projects, dict) and "error" in projects:
            return projects
        for p in projects:
            if p.get("status") == "opened":
                return p
        return {"error": "Niciun proiect deschis. Deschide un proiect în GNS3."}

    def get_nodes(self, project_id):
        return self._get(f"/projects/{project_id}/nodes")

    def get_links(self, project_id):
        return self._get(f"/projects/{project_id}/links")

    def get_node_links(self, project_id, node_id):
        links = self.get_links(project_id)
        if isinstance(links, dict) and "error" in links:
            return links
        node_links = []
        for link in links:
            for node in link.get("nodes", []):
                if node.get("node_id") == node_id:
                    node_links.append(link)
                    break
        return node_links

    def start_node(self, project_id, node_id):
        return self._post(f"/projects/{project_id}/nodes/{node_id}/start")

    def stop_node(self, project_id, node_id):
        return self._post(f"/projects/{project_id}/nodes/{node_id}/stop")

    def start_all(self, project_id):
        return self._post(f"/projects/{project_id}/nodes/start")

    def stop_all(self, project_id):
        return self._post(f"/projects/{project_id}/nodes/stop")

    def get_console_info(self, project_id, node_id):
        nodes = self.get_nodes(project_id)
        if isinstance(nodes, dict) and "error" in nodes:
            return nodes
        for n in nodes:
            if n["node_id"] == node_id:
                return {
                    "name": n["name"],
                    "console": n.get("console"),
                    "console_type": n.get("console_type"),
                    "console_host": n.get("console_host", "127.0.0.1"),
                }
        return {"error": f"Nod {node_id} nu a fost găsit"}

    def classify_node(self, node):
        name = node.get("name", "").lower()
        node_type = node.get("node_type", "").lower()
        symbol = node.get("symbol", "").lower()
        properties = node.get("properties", {})
        image = properties.get("image", "").lower() if properties else ""

        if node_type == "nat":
            return "nat"
        if node_type == "cloud":
            return "cloud"

        if node_type == "vpcs":
            return "endpoint_vpcs"

        if node_type in ("ethernet_switch", "ethernet_hub"):
            return "switch_simple"

        if node_type == "iou":
            if any(sw in image for sw in ["l2", "switch"]):
                return "switch_iou"
            return "router_iou"

        if node_type == "dynamips":
            return "router_dynamips"

        if node_type == "qemu":
            if any(sw in name for sw in ["switch", "sw", "viosl2"]):
                return "switch_qemu"
            if any(rt in name for rt in ["router", "r", "vios", "csr", "xrv"]):
                return "router_qemu"
            if any(fw in name for fw in ["firewall", "fw", "asa", "pfsense", "fortinet"]):
                return "firewall"
            return "endpoint_vm"

        if node_type == "docker":
            return "endpoint_docker"

        if any(r in name for r in ["router", "r1", "r2", "r3", "r4", "r5"]):
            return "router_dynamips"
        if any(s in name for s in ["switch", "sw", "s1", "s2", "s3"]):
            return "switch_simple"

        return "unknown"

    def analyze_topology(self, project_id):
        nodes = self.get_nodes(project_id)
        links = self.get_links(project_id)

        if isinstance(nodes, dict) and "error" in nodes:
            return nodes
        if isinstance(links, dict) and "error" in links:
            return links

        node_map = {}
        for n in nodes:
            node_map[n["node_id"]] = n

        devices = {
            "routers": [],
            "switches": [],
            "endpoints": [],
            "nat_clouds": [],
            "unknown": [],
        }

        for n in nodes:
            classification = self.classify_node(n)
            info = {
                "name": n["name"],
                "node_id": n["node_id"],
                "node_type": n["node_type"],
                "classification": classification,
                "status": n.get("status", "unknown"),
                "console": n.get("console"),
                "console_type": n.get("console_type"),
                "console_host": n.get("console_host", "127.0.0.1"),
                "ports": n.get("ports", []),
                "properties": n.get("properties", {}),
            }

            if "router" in classification:
                devices["routers"].append(info)
            elif "switch" in classification:
                devices["switches"].append(info)
            elif classification in ("nat", "cloud"):
                devices["nat_clouds"].append(info)
            elif classification in ("endpoint_vm", "endpoint_docker","endpoint_vpcs", "firewall"):
                devices["endpoints"].append(info)
            else:
                devices["unknown"].append(info)

        connections = []
        for link in links:
            link_nodes = link.get("nodes", [])
            if len(link_nodes) == 2:
                n1 = link_nodes[0]
                n2 = link_nodes[1]
                name1 = node_map.get(n1["node_id"], {}).get("name", "?")
                name2 = node_map.get(n2["node_id"], {}).get("name", "?")

                adapter1 = n1.get("adapter_number", 0)
                port1 = n1.get("port_number", 0)
                adapter2 = n2.get("adapter_number", 0)
                port2 = n2.get("port_number", 0)

                label1 = n1.get("label", {}).get("text", f"a{adapter1}/p{port1}")
                label2 = n2.get("label", {}).get("text", f"a{adapter2}/p{port2}")

                connections.append({
                    "node1": name1,
                    "node1_id": n1["node_id"],
                    "node1_interface": label1,
                    "node1_adapter": adapter1,
                    "node1_port": port1,
                    "node2": name2,
                    "node2_id": n2["node_id"],
                    "node2_interface": label2,
                    "node2_adapter": adapter2,
                    "node2_port": port2,
                    "link_type": link.get("link_type", "ethernet"),
                })

        nat_connected_router = None
        nat_interface = None
        for conn in connections:
            for nc in devices["nat_clouds"]:
                if conn["node1"] == nc["name"]:
                    nat_connected_router = conn["node2"]
                    nat_interface = conn["node2_interface"]
                    break
                elif conn["node2"] == nc["name"]:
                    nat_connected_router = conn["node1"]
                    nat_interface = conn["node1_interface"]
                    break

        return {
            "devices": devices,
            "connections": connections,
            "management": {
                "nat_connected_router": nat_connected_router,
                "nat_interface": nat_interface,
            },
            "summary": {
                "total_routers": len(devices["routers"]),
                "total_switches": len(devices["switches"]),
                "total_endpoints": len(devices["endpoints"]),
                "total_nat_clouds": len(devices["nat_clouds"]),
                "total_links": len(connections),
            }
        }
