<h1 align="center">AgenticOps – AI Network Configuration Agent </h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/OS-Linux-yellow?logo=linux&logoColor=white" alt="OS">
  <img src="https://img.shields.io/badge/GNS3-Supported-00a98f?logo=cisco&logoColor=white" alt="GNS3">
  <img src="https://img.shields.io/badge/Docker-Container-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/License-GPL--3.0-blue" alt="License">
  <img src="https://img.shields.io/badge/Netmiko-SSH%20Automation-green" alt="Netmiko">
  <img src="https://img.shields.io/badge/OpenRouter-Free%20AI-purple?logo=openai&logoColor=white" alt="OpenRouter">
  <img src="https://img.shields.io/badge/Cisco-IOS-1BA0D7?logo=cisco&logoColor=white" alt="Cisco IOS">
</p>


## Features

- **Automatic topology discovery** — reads any GNS3 topology via REST API (localhost:3080)
- **Device classification** — identifies routers (Dynamips/IOU/QEMU), switches, VMs, Docker containers, VPCS, NAT nodes
- **Zero-touch SSH setup** — configures SSH from scratch via GNS3 Telnet console
- **IP assignment** — auto-assigns IPs on interfaces and sub-interfaces
- **Routing protocols** — OSPF, EIGRP, RIPv2, static routes
- **Access Control Lists** — create/remove ACLs blocking any traffic type (ICMP, TCP, UDP, specific ports)
- **VLAN management** — router-on-a-stick with dot1Q sub-interfaces
- **Configuration backup** — saves running-config to local files
- **Containerized** - the agent is installed in a Docker Container, good for portability
- **Bilingual** — responds in Romanian or English based on user input
- **Portable** — works with any GNS3 topology, no hardcoded IPs or device names


## Structure

```

agenticOps/              
├── Dockerfile           
├── .dockerignore        
├── .gitignore
├── README.md
├── requirements.txt
├── agenticops/
│   ├── agent.py
│   ├── tools.py
│   ├── gns3_client.py
│   └── net_config.py
├── logs/
└── backups/     

```

## Install with Docker

Build the image:

```bash
docker build -t agenticops .
```

Run the container:

```bash
export OPENROUTER_API_KEY="sk-or-v1-your-key-here"
chmod +x run.sh 
./run.sh
```

## Run the Container manually:

```bash
docker run -it --rm --network host \
  --name agenticops \
  -e OPENROUTER_API_KEY="sk-or-v1-your-key-here" \
  agenticops
```

Change Model (optional):
```bash
export OPENROUTER_MODEL="google/gemma-4-26b-a4b-it:free"
```
