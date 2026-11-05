import json

import pytest

from llm_provider import REDACTED, LLMRouter, load_env_secrets, redact, redact_message

SECRET = "AgenticOpsLabKey2026"


SECRET_LINES = [
    # IOS 12.4 global commands
    f"radius-server host 10.0.100.10 auth-port 1812 acct-port 1813 key {SECRET}",
    f"radius-server key 7 {SECRET}",
    f"tacacs-server host 10.0.100.11 key {SECRET}",
    f"tacacs-server key 0 {SECRET}",
    # IOS 15.x server blocks
    f" key {SECRET}",
    f" key 0 {SECRET}",
    f" key 7 {SECRET}",
    f" server-private 10.0.100.12 auth-port 1812 key {SECRET}",
    f" server-private 10.0.100.12 key 7 {SECRET}",
    # AAA verification command
    f"test aaa group radius ai-agent {SECRET} legacy",
    f"test aaa group radius server 10.0.100.10 ai-agent {SECRET} new-code",
    # Local accounts and lines
    f"enable secret 5 {SECRET}",
    f"enable password {SECRET}",
    f"username admin privilege 15 secret 5 {SECRET}",
    f"username console-admin secret {SECRET}",
    f" password 7 {SECRET}",
    # SNMP, routing and NTP authentication
    f"snmp-server community {SECRET} RO",
    f" key-string {SECRET}",
    f" ip ospf message-digest-key 1 md5 {SECRET}",
    f" ip ospf authentication-key 7 {SECRET}",
    f"ntp authentication-key 1 md5 {SECRET} 7",
    # VPN
    f"crypto isakmp key {SECRET} address 10.0.0.2",
]


@pytest.mark.parametrize("line", SECRET_LINES)
def test_secret_is_masked(line):
    out = redact(line)
    assert SECRET not in out
    assert REDACTED in out


@pytest.mark.parametrize("line", SECRET_LINES)
def test_secret_is_masked_in_json_tool_output(line):
    # Tool results reach the LLM as json.dumps(): the line follows a literal backslash-n.
    out = redact(json.dumps({"output": f"hostname R1\n{line}\nend"}))
    assert SECRET not in out
    assert json.loads(out)["output"].startswith("hostname R1\n")


@pytest.mark.parametrize("line", [
    "radius-server host 10.0.100.10 auth-port 1812 acct-port 1813",
    "radius server AAA",
    " address ipv4 10.0.100.10 auth-port 1812 acct-port 1813",
    "aaa new-model",
    "aaa authentication login VTY group radius",
    "aaa authorization exec VTY group radius",
    "ip radius source-interface FastEthernet0/0",
    "key chain OSPF-KEYS",
    " key 1",
    "show aaa servers",
    "interface FastEthernet0/0",
    " ip address 10.0.100.1 255.255.255.0",
    "line vty 0 4",
    " login authentication VTY",
])
def test_normal_line_is_unchanged(line):
    assert redact(line) == line
    encoded = json.dumps({"output": f"hostname R1\n{line}\nend"})
    assert redact(encoded) == encoded


def test_full_ios15_block_keeps_structure():
    config = (
        "radius server AAA\n"
        " address ipv4 10.0.100.10 auth-port 1812 acct-port 1813\n"
        f" key {SECRET}\n"
        "aaa group server radius RG\n"
        " server name AAA\n"
    )
    out = redact(config)
    assert SECRET not in out
    assert out.count("\n") == config.count("\n")
    assert " address ipv4 10.0.100.10 auth-port 1812 acct-port 1813\n" in out
    assert " server name AAA\n" in out


def test_tool_result_as_sent_to_llm():
    # agent.py sends tool results as json.dumps(...), so newlines become a literal "\n".
    tool_result = {
        "status": "success",
        "output": f"aaa new-model\nradius server AAA\n key {SECRET}\n"
                  f"test aaa group radius ai-agent {SECRET} legacy\n",
    }
    message = {"role": "tool", "tool_call_id": "abc123XYZ", "content": json.dumps(tool_result)}
    out = redact_message(message)["content"]
    assert SECRET not in out
    assert json.loads(out)["status"] == "success"


def test_tool_call_arguments_are_masked():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "abc123XYZ",
            "type": "function",
            "function": {
                "name": "send_config_commands",
                "arguments": json.dumps({
                    "host": "10.0.100.1",
                    "commands": [f"radius-server host 10.0.100.10 key {SECRET}"],
                }),
            },
        }],
    }
    out = redact_message(message)["tool_calls"][0]["function"]["arguments"]
    assert SECRET not in out
    assert json.loads(out)["host"] == "10.0.100.1"


def test_user_messages_are_not_masked():
    message = {"role": "user", "content": f"the key is {SECRET}"}
    assert redact_message(message)["content"] == f"the key is {SECRET}"


def test_env_secrets_selection():
    env = {
        "AAA_RADIUS_KEY": SECRET,
        "NVIDIA_API_KEY": "nvapi-1234567890",
        "AAA_AGENT_PASSWORD": "AgentPass99",
        "LLM_REDACT_SECRETS": "true",
        "NVIDIA_MODEL": "nvidia/nemotron-3-super-120b-a12b",
        "GNS3_HOST": "127.0.0.1",
        "SHORT_KEY": "abc",
    }
    assert load_env_secrets(env) == {SECRET, "nvapi-1234567890", "AgentPass99"}


def test_env_secret_masked_anywhere(monkeypatch):
    # A known secret is masked even in output no pattern recognizes.
    monkeypatch.setenv("AAA_RADIUS_KEY", SECRET)
    router = LLMRouter.__new__(LLMRouter)
    router.redact_secrets = True
    router.credentials_file = None
    messages = [{"role": "tool", "tool_call_id": "abc123XYZ",
                 "content": f"debug: shared secret {SECRET} rejected"}]
    out = router.prepare_messages(messages)[0]["content"]
    assert SECRET not in out