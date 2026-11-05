import pytest
from netmiko.exceptions import NetmikoAuthenticationException

import credentials_store as cs
import net_config
from credentials_store import CredentialsError


class FakeConnection:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.enable_called = False

    def check_enable_mode(self):
        return self.enabled

    def enable(self):
        self.enable_called = True


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    original = cs.candidates
    monkeypatch.setattr(net_config.credentials_store, "candidates",
                        lambda host, path_=path: original(host, path_))
    return path


def fake_connect_handler(monkeypatch, accept_user, enabled=True):
    calls = []

    def handler(**kwargs):
        calls.append(kwargs["username"])
        if kwargs["username"] != accept_user:
            raise NetmikoAuthenticationException("Authentication failed")
        return FakeConnection(enabled)

    monkeypatch.setattr(net_config, "ConnectHandler", handler)
    return calls


def test_no_credentials_raises_clear_error(store, monkeypatch):
    calls = fake_connect_handler(monkeypatch, accept_user="admin")
    with pytest.raises(CredentialsError, match="login"):
        net_config.ssh_connect("10.0.100.1")
    assert calls == []  # never tries admin/cisco


def test_falls_back_from_aaa_to_local(store, monkeypatch):
    cs.save(cs.AAA_KEY, "ai-agent", "AgentPass99", None, store)
    cs.save(cs.LOCAL_KEY, "netadmin", "LocalPass1", "LocalPass1", store)
    calls = fake_connect_handler(monkeypatch, accept_user="netadmin")
    net_config.ssh_connect("10.0.100.1")
    assert calls == ["ai-agent", "netadmin"]


def test_all_rejected_lists_tried_users_without_passwords(store, monkeypatch):
    cs.save(cs.AAA_KEY, "ai-agent", "AgentPass99", None, store)
    fake_connect_handler(monkeypatch, accept_user="nobody")
    with pytest.raises(CredentialsError) as e:
        net_config.ssh_connect("10.0.100.1")
    assert "ai-agent" in str(e.value)
    assert "AgentPass99" not in str(e.value)


def test_radius_priv15_user_skips_enable(store, monkeypatch):
    cs.save(cs.AAA_KEY, "ai-agent", "AgentPass99", None, store)
    conn = FakeConnection(enabled=True)
    monkeypatch.setattr(net_config, "ConnectHandler", lambda **kw: conn)
    net_config.ssh_connect("10.0.100.1")
    assert conn.enable_called is False


def test_send_show_returns_error_status(store, monkeypatch):
    fake_connect_handler(monkeypatch, accept_user="admin")
    result = net_config.send_show("10.0.100.1", "show version")
    assert result["status"] == "error"
    assert "login" in result["error"]


def test_initial_ssh_uses_given_credentials(monkeypatch):
    sent = {}
    monkeypatch.setattr(net_config, "send_console_commands",
                        lambda host, port, commands, wait=1: sent.setdefault("cmds", commands))
    net_config.configure_initial_ssh("127.0.0.1", 5000, "R1", "netadmin", "LocalPass1",
                                     "EnablePass1")
    assert "username netadmin privilege 15 secret LocalPass1" in sent["cmds"]
    assert "enable secret EnablePass1" in sent["cmds"]
    assert not any("cisco" in c for c in sent["cmds"])