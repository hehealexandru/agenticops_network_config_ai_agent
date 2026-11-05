#!/usr/bin/env python3

import os
import sys
import json
import logging
from pathlib import Path
from colorama import Fore, Style, init

init(autoreset=True)

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "WARNING").upper(), logging.WARNING)
logging.basicConfig(
    level=_log_level if isinstance(_log_level, int) else logging.WARNING,
    format="  %(levelname)s %(name)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import credentials_store
from credentials_store import CredentialsError
from loop_guard import LoopGuard
from tools import TOOL_DEFINITIONS, execute_tool
from llm_provider import (
    AllProvidersUnavailable,
    LLMRouter,
    ProviderConfigError,
    ProviderRequestError,
    serialize_tool_result,
)

try:
    router = LLMRouter.from_env()
except ProviderConfigError as e:
    print(f"{Fore.RED}LLM configuration error: {e}{Style.RESET_ALL}")
    print("  Fill in the API keys in .env (e.g. OPENROUTER_API_KEY=sk-or-v1-...)")
    sys.exit(1)

MODEL = router.describe()

try:
    MAX_ITERATIONS = max(1, int(os.environ.get("AGENT_MAX_ITERATIONS", "15")))
except ValueError:
    MAX_ITERATIONS = 15

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

MUTATING_TOOLS = {
    "gns3_stop_nodes",
    "configure_ssh_on_device",
    "send_config_commands",
    "configure_ip_on_interface",
    "configure_vlan_subinterface",
    "configure_routing_protocol",
    "configure_access_list",
    "remove_access_list",
    "send_console_raw",
    "configure_stp",
    "docker_exec_command",
}


def run_login(scope=None):
    """Handle the local 'login' command. Nothing here is added to the conversation.

    Returns True when saved, False on invalid input, None when cancelled.
    """
    print(f"  {Fore.YELLOW}Credentials are saved locally and never sent to the LLM.{Style.RESET_ALL}")
    try:
        message = credentials_store.interactive_login(
            scope, warn=lambda text: print(f"  {Fore.YELLOW}⚠ {text}{Style.RESET_ALL}"))
    except (EOFError, KeyboardInterrupt):
        print(f"\n  {Fore.YELLOW}Login cancelled.{Style.RESET_ALL}")
        return None
    except CredentialsError as e:
        print(f"  {Fore.RED}✗ {e}{Style.RESET_ALL}")
        return False
    print(f"  {Fore.GREEN}✓ {message}{Style.RESET_ALL}")
    return True


def prepare_local_admin(device_name):
    """Before an SSH setup: confirm or enter the local administrator it will create.

    Returns None when ready, or an error message for the model.
    """
    saved = credentials_store.get(credentials_store.LOCAL_KEY)
    if saved:
        try:
            answer = input(f"  {Fore.MAGENTA}? Local admin for {device_name}: {saved['username']} "
                           f"(saved in credentials.json). [Enter] use it / [c] change: {Style.RESET_ALL}")
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer.strip().lower() not in ("c", "change"):
            return None
    else:
        print(f"  {Fore.YELLOW}The SSH setup creates a local lab administrator on {device_name}. "
              f"Enter it now:{Style.RESET_ALL}")
    for attempt in range(3):
        saved = run_login("local")
        if saved:
            return None
        if saved is None:
            break
        if attempt < 2:
            print(f"  {Fore.YELLOW}Try again ({2 - attempt} attempts left), or press Ctrl+C to cancel.{Style.RESET_ALL}")
    return ("The local administrator was not provided, so the SSH setup was not executed. "
            "Tell the user to run 'login local' and then ask again. Do not retry on your own.")


def confirm_action(tool_name):
    try:
        answer = input(f"  {Fore.MAGENTA}? Apply {tool_name}? [y]es / [n]o / [a]ll for this request: {Style.RESET_ALL}")
    except (EOFError, KeyboardInterrupt):
        print()
        return "n"
    answer = answer.strip().lower()
    if answer in ("a", "all"):
        return "a"
    if answer in ("y", "yes"):
        return "y"
    return "n"


SYSTEM_PROMPT = """You are a Network Engineer AI Assistant specialized in Cisco IOS and GNS3.
You have access to a GNS3 lab through its local API and you can:
1. Read and analyze any GNS3 topology
2. Configure routers and switches from scratch (SSH, IP addresses, interfaces)
3. Configure routing protocols: OSPF, EIGRP, RIPv2, static routes
4. Create and manage ACLs (block ICMP, TCP, UDP, specific ports)
5. Manage VLANs (router-on-a-stick sub-interfaces)
6. Back up configurations

Language: think, reason and respond ONLY in English, even if the user writes in another language.

STRICT rules:
1. ALWAYS start with gns3_analyze_topology to understand the network.
2. Use ONLY tools to interact with devices. NEVER invent output.
3. Before configuring, check the current state with send_show_command.
4. After configuring, verify the result with a show command.
5. The router connected to the NAT node is the management gateway. Configure its NAT interface with DHCP.
6. The other devices are reachable through the management router (add routes if needed).
7. When configuring SSH, use configure_ssh_on_device, which works through the GNS3 Telnet console.
8. When configuring SSH on the router connected to NAT, ALWAYS use these parameters: mgmt_interface set to the NAT interface (from the topology analysis, field nat_interface) and use_dhcp=true. On the other routers, set a static IP on the interface that leads to the NAT router.
9. Adapt to the device type:
   - dynamips (c7200, c3725): FastEthernet
   - IOU: Ethernet
   - qemu (vIOS, CSR): GigabitEthernet
10. VMs, VPCS and Docker containers are endpoints. Do not configure them, but ALWAYS mention them in the summary with their name and the switch or router they are connected to.
11. Always answer in English.
12. If something is dangerous (erasing the config, reload), ask for confirmation. Every configuration change is also shown to the user for approval before it runs; if the user declines an action, do not retry it, ask how to proceed instead.
13. When choosing IP addresses automatically, use logical schemes (e.g. 10.0.X.0/24 for links, 192.168.X.0/24 for VLANs).
14. SSH tools need the device IP address in "host", never the device name. configure_ssh_on_device returns the management IP in "mgmt_ip": use it. If a router has SSH configured and a reachable IP, ALWAYS use SSH (send_show_command, send_config_commands) instead of the Telnet console. The Telnet console (send_console_raw, configure_ssh_on_device) is used ONLY for the initial configuration, when the router has no SSH.
15. For Docker containers, use docker_exec_command to configure IP, gateway, or to test connectivity. Example commands: 'ip addr add 192.168.1.10/24 dev eth0', 'ip route add default via 192.168.1.1', 'ping -c 3 10.0.1.1'.
16. For troubleshooting, use ping_from_router to test connectivity hop by hop and collect_device_info to collect full information from a router. Analyze interfaces (up/down), routes, ACLs and OSPF neighbors to find the problem.
17. generate_documentation generates ONLY the topology documentation (devices, connections, management). Use it ONLY when the user asks for network or topology documentation.
18. Credentials are managed only by the user, with the local 'login' command. NEVER ask the user to type a password in the chat and never put passwords in tool arguments. If SSH authentication fails or credentials are missing, tell the user to run 'login' (scope 'aaa', 'local' or the device IP). Console logins are handled automatically by the agent: never send usernames or passwords through send_console_raw, and never guess, invent or state credentials (you do not know them).
19. <REDACTED> values in tool output are masked secrets. NEVER use them in configuration commands; if you need the real value, ask the user.
"""


def run_agent():
    conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

    print(f"""
{Fore.CYAN}╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║       AgenticOps – Network Configuration AI Agent            ║
║       Powered by OpenAI-compatible LLM providers             ║
║                                                              ║
║                                                              ║
║                                                              ║
║   Special commands: 'exit', 'clear', 'model', 'login'        ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}

  {Fore.YELLOW}Model: {MODEL}{Style.RESET_ALL}
""")

    while True:
        try:
            user_input = input(f"\n{Fore.GREEN} You: {Style.RESET_ALL}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{Fore.CYAN}Goodbye!{Style.RESET_ALL}")
            break

        if not user_input:
            continue
        if user_input.lower() == "exit":
            print(f"{Fore.CYAN}Goodbye!{Style.RESET_ALL}")
            break
        if user_input.lower() == "clear":
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print(f"{Fore.YELLOW}Conversation reset.{Style.RESET_ALL}")
            continue
        if user_input.lower() == "login" or user_input.lower().startswith("login "):
            run_login(user_input[5:].strip() or None)
            continue
        if user_input.lower() == "model":
            print(f"{Fore.YELLOW}Providers (in priority order):{Style.RESET_ALL}")
            for line in router.status():
                print(f"  {Fore.YELLOW}{line}{Style.RESET_ALL}")
            continue

        turn_start = len(conversation)
        conversation.append({"role": "user", "content": user_input})

        iteration = 0
        guard = LoopGuard()
        approve_all = False

        while iteration < MAX_ITERATIONS and not guard.should_stop:
            iteration += 1
            print(f"\n  {Fore.YELLOW} Processing... (step {iteration}){Style.RESET_ALL}")

            try:
                result = router.chat(conversation, TOOLS_OPENAI)
            except AllProvidersUnavailable as e:
                print(f"\n{Fore.RED}✗ No LLM provider is available right now.{Style.RESET_ALL}")
                for line in str(e).splitlines():
                    print(f"  {Fore.RED}{line}{Style.RESET_ALL}")
                print(f"{Fore.YELLOW}Ending the session. Try again later or add another provider to LLM_PROVIDERS.{Style.RESET_ALL}")
                return
            except ProviderRequestError as e:
                print(f"{Fore.RED}API error ({e.provider}/{e.model}, HTTP {e.status_code}): {e.detail}{Style.RESET_ALL}")
                del conversation[turn_start:]
                break
            except Exception as e:
                print(f"{Fore.RED}API error: {e}{Style.RESET_ALL}")
                del conversation[turn_start:]
                break

            print(f"  {Style.DIM}[{result.provider}/{result.model}]{Style.RESET_ALL}")

            message = result.message
            content = message.get("content") or ""
            if content:
                print(f"\n{Fore.CYAN} Agent:{Style.RESET_ALL} {content}")

            if not result.tool_calls:
                conversation.append({
                    "role": "assistant",
                    "content": content,
                })
                break

            conversation.append(message)

            for tc in result.tool_calls:
                tool_name = tc.name
                tool_call_id = tc.id
                tool_input = tc.arguments or {}

                if guard.should_stop:
                    # Every tool call needs a result in the history, even if it is skipped.
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": serialize_tool_result({"status": "error", "error": "Not executed: the turn was stopped."}),
                    })
                    continue

                print(f"\n  {Fore.YELLOW}⚙ Tool: {tool_name}{Style.RESET_ALL}")
                input_preview = json.dumps(tool_input, indent=2, ensure_ascii=False)
                if len(input_preview) > 300 and tool_name not in MUTATING_TOOLS:
                    input_preview = input_preview[:300] + "..."
                print(f"  {Fore.YELLOW}  Input: {input_preview}{Style.RESET_ALL}")

                blocked = guard.check(tool_name, tool_input)
                if blocked:
                    tool_result = {"status": "error", "error": blocked}
                elif tc.error:
                    tool_result = {"status": "error", "error": f"The arguments for {tool_name} are not valid JSON ({tc.error}). Resend the call with valid JSON arguments."}
                else:
                    decision = "y"
                    if tool_name in MUTATING_TOOLS and not approve_all:
                        decision = confirm_action(tool_name)
                        if decision == "a":
                            approve_all = True
                    credentials_error = None
                    if decision != "n" and tool_name == "configure_ssh_on_device":
                        credentials_error = prepare_local_admin(tool_input.get("device_name", "the router"))
                    if decision == "n":
                        tool_result = {"status": "error", "error": f"The user declined {tool_name}. Do not retry it; ask the user how to proceed."}
                    elif credentials_error:
                        tool_result = {"status": "error", "error": credentials_error}
                    else:
                        tool_result = execute_tool(tool_name, tool_input)
                guard.record(tool_name, tool_result.get("status") == "success")

                if tool_result.get("status") == "success":
                    output = tool_result.get("output", "")
                    if output:
                        lines = output.splitlines()
                        preview = "\n".join(lines[:25])
                        if len(lines) > 25:
                            preview += f"\n  ... ({len(lines) - 25} lines omitted)"
                        print(f"  {Fore.GREEN}✓ Output:{Style.RESET_ALL}")
                        for line in preview.splitlines():
                            print(f"    {line}")
                        if tool_result.get("message"):
                            print(f"  {Fore.GREEN}✓ {tool_result['message']}{Style.RESET_ALL}")
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
                        print(f"  {Fore.GREEN}✓ {summary or 'Success'}{Style.RESET_ALL}")
                else:
                    print(f"  {Fore.RED}✗ {tool_result.get('error', 'Unknown error')}{Style.RESET_ALL}")

                result_str = serialize_tool_result(tool_result, limit=16000)

                conversation.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_str,
                })

        if guard.should_stop:
            print(f"\n{Fore.RED}⚠ Stopped this request: {guard.stop_reason}.{Style.RESET_ALL}")
            print(f"  {Fore.YELLOW}This is the loop guard, not a rate limit. Check the device or rephrase the request.{Style.RESET_ALL}")
        elif iteration >= MAX_ITERATIONS:
            print(f"{Fore.RED}⚠ Reached AGENT_MAX_ITERATIONS ({MAX_ITERATIONS}), stopping.{Style.RESET_ALL}")


if __name__ == "__main__":
    run_agent()