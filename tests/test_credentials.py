import json
import stat

import pytest

import credentials_store as cs
from credentials_store import CredentialsError


@pytest.fixture
def path(tmp_path):
    return tmp_path / "credentials.json"


def answers(*values):
    """Fake input()/getpass() that returns the given answers in order."""
    it = iter(values)
    return lambda prompt="": next(it)


def test_save_creates_file_with_mode_600(path):
    cs.save(cs.AAA_KEY, "ai-agent", "AgentPass99", None, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())[cs.AAA_KEY] == {
        "username": "ai-agent", "password": "AgentPass99"}


def test_save_keeps_other_entries(path):
    cs.save(cs.LOCAL_KEY, "netadmin", "LocalPass1", "EnablePass1", path)
    cs.save(cs.AAA_KEY, "ai-agent", "AgentPass99", None, path)
    data = json.loads(path.read_text())
    assert set(data) == {cs.LOCAL_KEY, cs.AAA_KEY}


def test_save_leaves_no_temp_files(path):
    cs.save(cs.AAA_KEY, "ai-agent", "AgentPass99", None, path)
    assert [p.name for p in path.parent.iterdir()] == ["credentials.json"]


def test_candidates_order(path):
    cs.save(cs.LOCAL_KEY, "netadmin", "LocalPass1", None, path)
    cs.save(cs.AAA_KEY, "ai-agent", "AgentPass99", None, path)
    cs.save("10.0.100.1", "special", "HostPass11", None, path)
    labels = [label for label, _ in cs.candidates("10.0.100.1", path)]
    assert labels == ["10.0.100.1", cs.AAA_KEY, cs.LOCAL_KEY]
    labels = [label for label, _ in cs.candidates("10.0.100.2", path)]
    assert labels == [cs.AAA_KEY, cs.LOCAL_KEY]


def test_no_default_credentials(path):
    # The old code silently fell back to admin/cisco.
    assert cs.candidates("10.0.100.1", path) == []
    path.write_text("{}")
    assert cs.candidates("10.0.100.1", path) == []
    path.write_text("")
    assert cs.candidates("10.0.100.1", path) == []


def test_invalid_file_gives_clear_error(path):
    path.write_text("{not json")
    with pytest.raises(CredentialsError, match="not valid JSON"):
        cs.load_all(path)


def test_directory_instead_of_file(path):
    path.mkdir()
    with pytest.raises(CredentialsError, match="directory"):
        cs.load_all(path)


@pytest.mark.parametrize("scope,key", [
    ("aaa", cs.AAA_KEY), ("AAA", cs.AAA_KEY), ("local", cs.LOCAL_KEY),
    ("10.0.100.1", "10.0.100.1"), (" 10.0.100.1 ", "10.0.100.1"),
])
def test_scope_key(scope, key):
    assert cs.scope_key(scope) == key


def test_scope_key_rejects_garbage():
    with pytest.raises(CredentialsError):
        cs.scope_key("R1")


def test_interactive_login_aaa(path):
    msg = cs.interactive_login(
        "aaa", input_fn=answers("ai-agent"),
        getpass_fn=answers("AgentPass99", "AgentPass99", ""), path=path)
    assert "aaa_agent" in msg
    assert "AgentPass99" not in msg
    assert cs.get(cs.AAA_KEY, path) == {"username": "ai-agent", "password": "AgentPass99"}


def test_interactive_login_asks_scope(path):
    cs.interactive_login(
        input_fn=answers("10.0.100.1", "special"),
        getpass_fn=answers("HostPass11", "HostPass11", ""), path=path)
    assert cs.get("10.0.100.1", path)["username"] == "special"


def test_interactive_login_local_enable_defaults_to_password(path):
    cs.interactive_login(
        "local", input_fn=answers("netadmin"),
        getpass_fn=answers("LocalPass1", "LocalPass1", ""), path=path)
    assert cs.get(cs.LOCAL_KEY, path)["secret"] == "LocalPass1"


def test_interactive_login_local_separate_enable(path):
    cs.interactive_login(
        "local", input_fn=answers("netadmin"),
        getpass_fn=answers("LocalPass1", "LocalPass1", "EnablePass1", "EnablePass1"), path=path)
    assert cs.get(cs.LOCAL_KEY, path)["secret"] == "EnablePass1"


@pytest.mark.parametrize("passwords,error", [
    (("short", "short"), "at least 8"),
    (("AgentPass99", "Different99"), "do not match"),
])
def test_interactive_login_rejects_bad_password(path, passwords, error):
    with pytest.raises(CredentialsError, match=error):
        cs.interactive_login("aaa", input_fn=answers("ai-agent"),
                             getpass_fn=answers(*passwords), path=path)
    assert not path.exists()


@pytest.mark.parametrize("password", ["Has space1", "Question?1"])
def test_local_password_must_be_ios_safe(path, password):
    with pytest.raises(CredentialsError, match="spaces or"):
        cs.interactive_login("local", input_fn=answers("netadmin"),
                             getpass_fn=answers(password, password), path=path)


def test_aaa_password_may_contain_spaces(path):
    # The AAA password is only sent over SSH, never typed into the IOS CLI.
    cs.interactive_login("aaa", input_fn=answers("ai-agent"),
                         getpass_fn=answers("Has space1", "Has space1", ""), path=path)
    assert cs.get(cs.AAA_KEY, path)["password"] == "Has space1"


def test_invalid_username(path):
    with pytest.raises(CredentialsError, match="username"):
        cs.interactive_login("aaa", input_fn=answers("bad user"),
                             getpass_fn=answers(), path=path)