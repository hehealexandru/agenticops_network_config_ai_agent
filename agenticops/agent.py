#!/usr/bin/env python3

import os
import sys
import json
from pathlib import Path
from openai import OpenAI
from colorama import Fore, Style, init

init(autoreset=True)

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not API_KEY:
    print(f"{Fore.RED}Eroare: Setează OPENROUTER_API_KEY{Style.RESET_ALL}")
    print(f"  export OPENROUTER_API_KEY=\"sk-or-v1-...\"")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import TOOL_DEFINITIONS, execute_tool

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=API_KEY,
)

MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")

TOOLS_OPENAI = []
for tool in TOOL_DEFINITIONS:
    TOOLS_OPENAI.append({
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        }
    })

SYSTEM_PROMPT = """Ești un Network Engineer AI Assistant specializat pe Cisco IOS și GNS3.
Ai acces la un laborator GNS3 prin API-ul local și poți:
1. Citi și analiza orice topologie GNS3
2. Configura routere și switch-uri de la zero (SSH, IP-uri, interfețe)
3. Configura protocoale de rutare: OSPF, EIGRP, RIPv2, rute statice
4. Crea și gestiona ACL-uri (blochează ICMP, TCP, UDP, porturi specifice)
5. Gestiona VLAN-uri (sub-interfețe router-on-a-stick)
6. Face backup la configurații

Reguli STRICTE:
1. ÎNTOTDEAUNA începe cu gns3_analyze_topology pentru a înțelege rețeaua.
2. Folosește DOAR tools pentru a interacționa cu echipamentele. NU inventa output.
3. Înainte de a configura, verifică starea curentă cu send_show_command.
4. După configurare, verifică rezultatul cu o comandă show.
5. Routerul conectat la NAT este gateway-ul de management. Configurează-i interfața NAT cu DHCP.
6. Celelalte echipamente sunt accesibile prin routerul de management (adaugă rute dacă e nevoie).
7. Când configurezi SSH, folosește configure_ssh_on_device care merge prin consola Telnet GNS3.
8. Când configurezi SSH pe routerul conectat la NAT, ÎNTOTDEAUNA folosește parametrii: mgmt_interface cu interfața NAT (din analiza topologiei, câmpul nat_interface) și use_dhcp=true. Pe celelalte routere, pune IP static pe interfața care duce spre routerul NAT.
9. Adaptează-te la tipul echipamentului:
   - dynamips (c7200, c3725): FastEthernet
   - IOU: Ethernet
   - qemu (vIOS, CSR): GigabitEthernet
10. VM-urile și containerele Docker sunt endpoint-uri. Nu le configura, dar menționează-le.
11. Răspunde în română dacă utilizatorul scrie în română.
12. Dacă ceva e periculos (ștergere config, reload), cere confirmare.
13. Când alegi IP-uri automat, folosește scheme logice (ex: 10.0.X.0/24 pt link-uri, 192.168.X.0/24 pt VLANs).
14. Dacă un router are SSH configurat și un IP accesibil, folosește ÎNTOTDEAUNA SSH (send_show_command, send_config_commands) în loc de consolă Telnet. Consola Telnet (send_console_raw, configure_ssh_on_device) se folosește DOAR pentru configurare inițială când routerul nu are SSH.
"""


def run_agent():
    conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

    print(f"""
{Fore.CYAN}╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║       AgenticOps – Network Configuration AI Agent            ║
║       Powered by OpenRouter                                  ║
║                                                              ║
║                                                              ║
║                                                              ║
║   Special commands: 'exit', 'quit', 'clear', 'model'         ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}

  {Fore.YELLOW}Model: {MODEL}{Style.RESET_ALL}
""")

    while True:
        try:
            user_input = input(f"\n{Fore.GREEN} Tu: {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{Fore.CYAN}La revedere!{Style.RESET_ALL}")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print(f"{Fore.CYAN}La revedere!{Style.RESET_ALL}")
            break
        if user_input.lower() == "clear":
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print(f"{Fore.YELLOW}Conversație resetată.{Style.RESET_ALL}")
            continue
        if user_input.lower() == "model":
            print(f"{Fore.YELLOW}Model curent: {MODEL}{Style.RESET_ALL}")
            continue

        conversation.append({"role": "user", "content": user_input})

        max_iterations = 15
        iteration = 0
        last_tool = None
        same_tool_count = 0

        while iteration < max_iterations:
            iteration += 1
            print(f"\n  {Fore.YELLOW} Se procesează... (pas {iteration}){Style.RESET_ALL}")

            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=conversation,
                    tools=TOOLS_OPENAI,
                    max_tokens=4096,
                    extra_headers={
                        "HTTP-Referer": "https://github.com/netauto-project",
                        "X-Title": "NetAuto AgenticOps v2",
                    },
                )
            except Exception as e:
                print(f"{Fore.RED}Eroare API: {e}{Style.RESET_ALL}")
                conversation.pop()
                break

            choice = response.choices[0]
            message = choice.message

            if message.content:
                content = message.content
                if "</think>" in content:
                    content = content.split("</think>")[-1].strip()
                if content:
                    print(f"\n{Fore.CYAN} Agent:{Style.RESET_ALL} {content}")

            tool_calls = message.tool_calls

            if not tool_calls:
                display_content = message.content or ""
                if "</think>" in display_content:
                    display_content = display_content.split("</think>")[-1].strip()
                conversation.append({
                    "role": "assistant",
                    "content": display_content,
                })
                break

            conversation.append(message)

            for tc in tool_calls:
                tool_name = tc.function.name
                tool_call_id = tc.id

                try:
                    tool_input = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_input = {}

                print(f"\n  {Fore.YELLOW}⚙ Tool: {tool_name}{Style.RESET_ALL}")
                input_preview = json.dumps(tool_input, indent=2, ensure_ascii=False)
                if len(input_preview) > 300:
                    input_preview = input_preview[:300] + "..."
                print(f"  {Fore.YELLOW}  Input: {input_preview}{Style.RESET_ALL}")

                if tool_name == last_tool:
                    same_tool_count += 1
                else:
                    same_tool_count = 0
                last_tool = tool_name

                if same_tool_count >= 2:
                    tool_result = {"status": "error", "error": f"Tool-ul {tool_name} a fost apelat de prea multe ori consecutiv. Procesează rezultatele anterioare și răspunde utilizatorului."}
                else:
                    tool_result = execute_tool(tool_name, tool_input)

                if tool_result.get("status") == "success":
                    output = tool_result.get("output", "")
                    if output:
                        lines = output.splitlines()
                        preview = "\n".join(lines[:25])
                        if len(lines) > 25:
                            preview += f"\n  ... ({len(lines) - 25} linii omise)"
                        print(f"  {Fore.GREEN}✓ Output:{Style.RESET_ALL}")
                        for line in preview.splitlines():
                            print(f"    {line}")
                    else:
                        summary = ""
                        for key in ["message", "topology", "projects", "backup_file"]:
                            if key in tool_result:
                                val = tool_result[key]
                                if isinstance(val, dict):
                                    summary = json.dumps(val.get("summary", val), indent=2, ensure_ascii=False)[:200]
                                elif isinstance(val, list):
                                    summary = str([x.get("name", x) for x in val[:5]])
                                else:
                                    summary = str(val)
                                break
                        print(f"  {Fore.GREEN}✓ {summary or 'Succes'}{Style.RESET_ALL}")
                else:
                    print(f"  {Fore.RED}✗ {tool_result.get('error', 'Eroare necunoscută')}{Style.RESET_ALL}")

                result_str = json.dumps(tool_result, ensure_ascii=False)
                if len(result_str) > 8000:
                    result_str = result_str[:8000] + '..."}'

                conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_str,
                })

        if iteration >= max_iterations:
            print(f"{Fore.RED}⚠ Prea multe iterații, opresc procesarea.{Style.RESET_ALL}")


if __name__ == "__main__":
    run_agent()
