import pytest

import net_config
from console import ConsoleError, ConsoleLoginRequired, IOSConsole, TelnetTransport
from fake_ios import FakeIOS

LOCAL = {"username": "netadmin", "password": "LocalPass1", "secret": "EnablePass1"}


def console(fake, creds=None):
    return IOSConsole(fake, lambda: creds, poll=0)


# -- reaching a prompt ---------------------------------------------------------------

def test_answers_initial_dialog():
    fake = FakeIOS()
    con = console(fake)
    assert con.wake(timeout=5) == "user"
    assert con.prompt == "Router>"
    assert "no" in fake.typed
    assert "enable" not in fake.typed  # nothing typed into the dialog by mistake


def test_waits_for_boot_to_finish():
    fake = FakeIOS(boot_reads=20)
    assert console(fake).wake(timeout=5) == "user"


def test_boot_timeout_is_reported():
    fake = FakeIOS(boot_reads=10**9)
    with pytest.raises(ConsoleError, match="still be booting"):
        console(fake).wake(timeout=0.3)


def test_console_login_uses_saved_local_admin():
    fake = FakeIOS(dialog=False, login=("netadmin", "LocalPass1"))
    assert console(fake, LOCAL).wake(timeout=5) == "privileged"
    assert fake.typed.count("netadmin") == 1


def test_console_login_without_credentials_stops():
    fake = FakeIOS(dialog=False, login=("netadmin", "LocalPass1"))
    with pytest.raises(ConsoleLoginRequired, match="login local"):
        console(fake, None).wake(timeout=5)
    assert fake.typed == [""]  # no guessed usernames such as admin/admin


def test_console_login_with_wrong_credentials_stops():
    fake = FakeIOS(dialog=False, login=("netadmin", "OtherPass1"))
    with pytest.raises(ConsoleError, match="login failed"):
        console(fake, LOCAL).wake(timeout=5)


# -- running commands ----------------------------------------------------------------

def test_enable_answers_secret_from_store():
    fake = FakeIOS(dialog=False, enable_secret="EnablePass1")
    con = console(fake, LOCAL)
    con.wake(timeout=5)
    con.enable()
    assert con.mode == "privileged"


def test_password_prompt_inside_command_is_aborted():
    # A raw "enable" sent by the model must never get a model-chosen password.
    fake = FakeIOS(dialog=False, enable_secret="EnablePass1")
    con = console(fake, LOCAL)
    con.wake(timeout=5)
    with pytest.raises(ConsoleError, match="asked for a password"):
        con.run("enable", timeout=5)
    assert "EnablePass1" not in fake.typed


def test_run_strips_echo_and_prompt():
    fake = FakeIOS(dialog=False)
    con = console(fake)
    con.wake(timeout=5)
    out = con.run("show ip ssh", timeout=5)
    assert out.startswith("SSH Disabled")
    assert "Router>" not in out
    assert "show ip ssh" not in out.splitlines()[0]


def test_telnet_negotiation_is_refused_and_stripped():
    class FakeSocket:
        def __init__(self):
            self.sent = b""
        def sendall(self, data):
            self.sent += data

    t = TelnetTransport.__new__(TelnetTransport)
    t.sock, t._pending = FakeSocket(), b""
    data = bytes([255, 251, 1, 255, 253, 3]) + b"Router>" + bytes([255, 255])
    assert t._strip_negotiation(data) == b"Router>\xff"
    assert t.sock.sent == bytes([255, 254, 1, 255, 252, 3])  # DONT echo, WONT sga


# -- SSH setup end to end ------------------------------------------------------------

@pytest.fixture
def fake_console(monkeypatch):
    holder = {}

    def opener(host, port):
        return IOSConsole(holder["fake"], lambda: LOCAL, poll=0)

    monkeypatch.setattr(net_config, "_open_ios_console", opener)
    return holder


def setup_ssh(**kwargs):
    return net_config.configure_initial_ssh(
        "127.0.0.1", 5000, "R1", "netadmin", "LocalPass1", "EnablePass1",
        boot_timeout=5, dhcp_timeout=0, **kwargs)


def test_ssh_setup_on_fresh_router(fake_console):
    fake = fake_console["fake"] = FakeIOS(boot_reads=5)
    result = setup_ssh(mgmt_interface="f1/0", use_dhcp=True)
    assert result["status"] == "success", result.get("error")
    assert result["ssh_enabled"] is True
    assert result["mgmt_ip"] == "192.168.122.227"
    assert "192.168.122.227" in result["message"]
    assert fake.hostname == "R1" and fake.saved
    assert "username netadmin privilege 15 secret LocalPass1" in fake.config_lines
    assert "enable secret EnablePass1" in fake.config_lines
    assert not any("cisco" in line for line in fake.config_lines)
    assert "Launching IOS image" not in result["output"]  # boot banner is not sent to the LLM


def test_ssh_setup_replaces_existing_rsa_keys(fake_console):
    fake_console["fake"] = FakeIOS(dialog=False, has_rsa_keys=True)
    assert setup_ssh()["status"] == "success"


def test_ssh_setup_keeps_working_rsa_keys(fake_console):
    # Regenerating keys would change the SSH host key and trigger
    # "REMOTE HOST IDENTIFICATION HAS CHANGED" on every client.
    fake = fake_console["fake"] = FakeIOS(dialog=False, hostname="R1", has_rsa_keys=True)
    result = setup_ssh(mgmt_interface="f1/0", use_dhcp=True)
    assert result["status"] == "success", result.get("error")
    assert result["rsa_keys"].startswith("kept")
    assert not any(c.startswith("crypto key") for c in fake.config_lines)
    assert fake.ssh_version_2


def test_ssh_setup_regenerates_keys_when_asked(fake_console):
    fake = fake_console["fake"] = FakeIOS(dialog=False, hostname="R1", has_rsa_keys=True)
    result = setup_ssh(regenerate_keys=True)
    assert result["status"] == "success", result.get("error")
    assert result["rsa_keys"] == "generated"
    assert "crypto key generate rsa modulus 2048" in fake.config_lines


def test_ssh_setup_generates_keys_after_hostname_change(fake_console):
    # Keys are named after the hostname, so a renamed router gets new keys.
    fake = fake_console["fake"] = FakeIOS(dialog=False, hostname="OldName", has_rsa_keys=True)
    assert setup_ssh()["rsa_keys"] == "generated"
    assert "crypto key generate rsa modulus 2048" in fake.config_lines


def test_ssh_setup_reports_keys_too_small_for_v2(fake_console):
    fake_console["fake"] = FakeIOS(dialog=False, hostname="R1", has_rsa_keys=True, rsa_bits=512)
    result = setup_ssh()
    assert result["status"] == "error"
    assert "regenerate_keys=true" in result["error"]
    assert result["ssh_enabled"] is False


def test_ssh_setup_reports_missing_dhcp_address(fake_console):
    fake_console["fake"] = FakeIOS(dialog=False, dhcp_ip=None)
    result = setup_ssh(mgmt_interface="f1/0", use_dhcp=True)
    assert result["status"] == "error"
    assert "no IP address" in result["error"]
    assert result["ssh_enabled"] is True


def test_ssh_setup_reports_ios_errors(fake_console):
    fake_console["fake"] = FakeIOS(dialog=False)
    result = setup_ssh(mgmt_interface="Serial9/9", use_dhcp=True)
    assert result["status"] == "error"
    assert "interface Serial9/9" in result["error"]


def test_console_commands_report_rejected_command(fake_console):
    fake_console["fake"] = FakeIOS(dialog=False)
    result = net_config.send_console_commands("127.0.0.1", 5000,
                                              ["enable", "show ip interface brief", "bogus"],
                                              boot_timeout=5)
    assert result["status"] == "error"
    assert "bogus" in result["error"]
    assert "FastEthernet1/0" in result["output"]


@pytest.mark.parametrize("name,full,match", [
    ("f1/0", "FastEthernet1/0", True),
    ("Fa1/0", "FastEthernet1/0", True),
    ("FastEthernet1/0", "FastEthernet1/0", True),
    ("f1/0", "FastEthernet0/0", False),
    ("g0/0", "FastEthernet0/0", False),
])
def test_interface_matches(name, full, match):
    assert net_config._interface_matches(name, full) is match