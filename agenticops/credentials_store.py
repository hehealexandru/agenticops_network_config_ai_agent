"""Local credential store for AgenticOps.

Credentials are entered only through the CLI `login` command (hidden input) and kept in
credentials.json with mode 600. They never pass through the conversation or the LLM.

File layout:
    {
      "aaa_agent":   {"username": "ai-agent", "password": "...", "secret": "..."},
      "local_admin": {"username": "netadmin", "password": "...", "secret": "..."},
      "10.0.100.1":  {"username": "...", "password": "...", "secret": "..."}
    }

SSH lookup order for a host: the host's own entry, then aaa_agent, then local_admin.
"""
from __future__ import annotations

import getpass
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "credentials.json"

AAA_KEY = "aaa_agent"
LOCAL_KEY = "local_admin"
SCOPES = {"aaa": AAA_KEY, "local": LOCAL_KEY}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
# Typed into the IOS CLI over the console: whitespace splits the command, "?" opens help.
_IOS_UNSAFE_RE = re.compile(r"[\s?]")
_MIN_PASSWORD_LEN = 8


class CredentialsError(RuntimeError):
    pass


def scope_key(scope: str) -> str:
    """Map a login scope ("aaa", "local" or a host IP) to its key in credentials.json."""
    scope = scope.strip()
    if scope.lower() in SCOPES:
        return SCOPES[scope.lower()]
    try:
        return str(ipaddress.ip_address(scope))
    except ValueError:
        raise CredentialsError(
            f"invalid scope '{scope}': use 'aaa', 'local' or a device IP address"
        ) from None


def load_all(path: Path = DEFAULT_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    if path.is_dir():
        raise CredentialsError(f"{path.name} is a directory, not a file; remove it and run 'login'")
    text = path.read_text().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CredentialsError(f"{path.name} is not valid JSON ({e.msg}, line {e.lineno})") from None
    if not isinstance(data, dict):
        raise CredentialsError(f"{path.name} must contain a JSON object")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def get(key: str, path: Path = DEFAULT_PATH) -> dict[str, Any] | None:
    entry = load_all(path).get(key)
    if entry and entry.get("username") and entry.get("password"):
        return entry
    return None


def candidates(host: str, path: Path = DEFAULT_PATH) -> list[tuple[str, dict[str, Any]]]:
    """Credentials to try for SSH to `host`, in order, as (label, entry) pairs."""
    data = load_all(path)
    result: list[tuple[str, dict[str, Any]]] = []
    for key in dict.fromkeys((host, AAA_KEY, LOCAL_KEY)):
        entry = data.get(key)
        if entry and entry.get("username") and entry.get("password"):
            result.append((key, entry))
    return result


def _write(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, path)
        except OSError:
            # run.sh bind-mounts credentials.json into the container; a mount point
            # cannot be replaced by rename, so write it in place instead.
            os.unlink(tmp)
            with open(path, "w") as f:
                f.write(text)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(path, 0o600)


def save(key: str, username: str, password: str, secret: str | None = None,
         path: Path = DEFAULT_PATH) -> None:
    data = load_all(path)
    entry: dict[str, str] = {"username": username, "password": password}
    if secret:
        entry["secret"] = secret
    data[key] = entry
    _write(path, data)


def _validate_username(username: str) -> str:
    if not _USERNAME_RE.match(username):
        raise CredentialsError("username: letters, digits, '.', '_' or '-', max 32 characters")
    return username


def _validate_password(password: str, label: str, ios_cli: bool) -> str:
    if len(password) < _MIN_PASSWORD_LEN:
        raise CredentialsError(f"{label} must be at least {_MIN_PASSWORD_LEN} characters")
    if ios_cli and _IOS_UNSAFE_RE.search(password):
        raise CredentialsError(f"{label} must not contain spaces or '?' (typed into the IOS CLI)")
    return password


def _ask_password(label: str, getpass_fn: Callable[[str], str], ios_cli: bool) -> str:
    first = getpass_fn(f"  {label}: ")
    _validate_password(first, label, ios_cli)
    if getpass_fn(f"  Repeat {label.lower()}: ") != first:
        raise CredentialsError(f"{label} values do not match")
    return first


_OVERWRITE_WARNINGS = {
    LOCAL_KEY: ("Routers that were already set up keep the old local administrator. "
                "Run the SSH setup again on them, or change the user on each router."),
    AAA_KEY: ("The AAA server keeps its own copy of the agent password. Change it there too: "
              "radius-user passwd {username}"),
}


def overwrite_warning(key: str, old: dict[str, Any] | None, username: str, password: str,
                      secret: str | None) -> str | None:
    """Warning shown when a login changes credentials that devices may already use."""
    if not old:
        return None
    if (old.get("username"), old.get("password"), old.get("secret")) == (username, password, secret):
        return None
    text = _OVERWRITE_WARNINGS.get(
        key, "The device is not changed: update the password on {key} itself as well.")
    return ("This only updates " + DEFAULT_PATH.name + ". "
            + text.format(username=old.get("username", username), key=key))


def interactive_login(
    scope: str | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    path: Path = DEFAULT_PATH,
    warn: Callable[[str], None] | None = None,
) -> str:
    """Ask for credentials with hidden input and save them. Returns a status message.

    `warn` receives a warning when existing credentials are changed (devices keep the old
    ones). Raises CredentialsError on invalid input; EOFError/KeyboardInterrupt propagate.
    """
    if not scope:
        scope = input_fn("  Scope [aaa = RADIUS agent profile / local = lab admin / device IP]: ")
    key = scope_key(scope)
    # The local admin is configured on routers by typing it into the IOS CLI.
    ios_cli = key == LOCAL_KEY

    username = _validate_username(input_fn("  Username: ").strip())
    password = _ask_password("Password", getpass_fn, ios_cli)

    if key == LOCAL_KEY:
        secret = getpass_fn("  Enable secret (Enter = same as password): ") or password
        if secret != password:
            _validate_password(secret, "Enable secret", ios_cli)
            if getpass_fn("  Repeat enable secret: ") != secret:
                raise CredentialsError("Enable secret values do not match")
    else:
        secret = getpass_fn("  Enable secret (Enter = none): ") or None

    old = load_all(path).get(key)
    save(key, username, password, secret, path)
    warning = overwrite_warning(key, old, username, password, secret)
    if warning and warn:
        warn(warning)
    return f"Saved credentials for '{key}' (user {username}) in {path.name}"