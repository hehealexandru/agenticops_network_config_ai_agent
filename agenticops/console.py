from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable
from typing import Any, Protocol

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


class ConsoleError(RuntimeError):
    pass


class ConsoleLoginRequired(ConsoleError):
    pass


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...
    def read(self, timeout: float) -> bytes: ...
    def close(self) -> None: ...


class TelnetTransport:
    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self._pending = b""

    def write(self, data: bytes) -> None:
        self.sock.sendall(data.replace(bytes([IAC]), bytes([IAC, IAC])))

    def read(self, timeout: float) -> bytes:
        """Return the data that arrives within `timeout` seconds (may be empty)."""
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, BlockingIOError):
            return b""
        if not chunk:
            raise ConsoleError("console connection closed by the server")
        return self._strip_negotiation(self._pending + chunk)

    def _strip_negotiation(self, data: bytes) -> bytes:
        out = bytearray()
        i = 0
        self._pending = b""
        while i < len(data):
            byte = data[i]
            if byte != IAC:
                out.append(byte)
                i += 1
                continue
            if i + 1 >= len(data):
                self._pending = data[i:]
                break
            cmd = data[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(data):
                    self._pending = data[i:]
                    break
                option = data[i + 2]
                if cmd == DO:
                    self.sock.sendall(bytes([IAC, WONT, option]))
                elif cmd == WILL:
                    self.sock.sendall(bytes([IAC, DONT, option]))
                i += 3
            elif cmd == SB:
                end = data.find(bytes([IAC, SE]), i + 2)
                if end == -1:
                    self._pending = data[i:]
                    break
                i = end + 2
            else:
                i += 2
        return bytes(out)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# Prompts are matched on the last non-empty line of the console buffer.
_PROMPT_RE = re.compile(r"^([A-Za-z0-9._-]+)(\(config[^)]*\))?([>#])\s*$")
_DIALOG_RE = re.compile(r"initial configuration dialog\?\s*\[yes/no\]:\s*$", re.IGNORECASE)
_AUTOINSTALL_RE = re.compile(r"terminate autoinstall\?\s*\[yes\]:\s*$", re.IGNORECASE)
_RETURN_RE = re.compile(r"press return to get started", re.IGNORECASE)
_USERNAME_RE = re.compile(r"(?:username|login):\s*$", re.IGNORECASE)
_PASSWORD_RE = re.compile(r"password:\s*$", re.IGNORECASE)
_LOGIN_FAILED_RE = re.compile(r"% (?:login invalid|authentication failed|bad passwords?)",
                              re.IGNORECASE)
_YES_NO_RE = re.compile(r"\[yes/no\]:?\s*$", re.IGNORECASE)
_CONFIRM_RE = re.compile(r"\[confirm\]\s*$", re.IGNORECASE)
_MORE_RE = re.compile(r"--More--\s*$")
_ERROR_RE = re.compile(r"^% (?:Invalid input|Incomplete command|Ambiguous command|Unknown command)"
                       r".*$", re.MULTILINE)


def _last_line(text: str) -> str:
    for line in reversed(text.replace("\r", "").split("\n")):
        if line.strip():
            return line.strip()
    return ""


class IOSConsole:
    def __init__(
        self,
        transport: Transport,
        credentials: Callable[[], dict[str, Any] | None] = lambda: None,
        *,
        poll: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.t = transport
        self.credentials = credentials
        self.poll = poll
        self._clock = clock
        self.prompt = ""
        self.hostname = ""
        self.transcript: list[str] = []

    # -- low level -------------------------------------------------------------------

    def _send(self, text: str) -> None:
        self.t.write(text.encode("ascii", errors="replace") + b"\r")

    def _read(self, timeout: float) -> str:
        return self.t.read(timeout).decode("ascii", errors="ignore")

    def _drain(self, max_seconds: float = 2.0) -> None:
        """Discard what is already buffered (boot banners, old output).

        Bounded in time: a booting router can print messages without pause.
        """
        deadline = self._clock() + max_seconds
        while self._clock() < deadline and self._read(0.3):
            pass

    def _set_prompt(self, line: str) -> bool:
        m = _PROMPT_RE.match(line)
        if not m:
            return False
        self.prompt = line
        self.hostname = m.group(1)
        return True

    @property
    def mode(self) -> str:
        m = _PROMPT_RE.match(self.prompt)
        if not m:
            return "unknown"
        if m.group(2):
            return "config"
        return "privileged" if m.group(3) == "#" else "user"

    # -- reaching a prompt -----------------------------------------------------------

    def wake(self, timeout: float = 300.0) -> str:
        """Bring the console to an EXEC/config prompt; returns the mode."""
        self._drain()
        deadline = self._clock() + timeout
        buffer = ""
        failures = 0
        self._send("")
        while self._clock() < deadline:
            chunk = self._read(self.poll)
            if chunk:
                buffer = (buffer + chunk)[-4000:]
                continue  # keep reading while output is flowing (boot messages)
            line = _last_line(buffer)
            if self._set_prompt(line):
                return self.mode
            if _LOGIN_FAILED_RE.search(buffer):
                failures += 1
                buffer = ""
                if failures >= 2:
                    raise ConsoleError(
                        "console login failed with the saved local administrator; "
                        "ask the user to run 'login local' with the correct credentials")
                continue
            if _DIALOG_RE.search(line):
                buffer = ""
                self._send("no")
            elif _AUTOINSTALL_RE.search(line):
                buffer = ""
                self._send("yes")
            elif _USERNAME_RE.search(line):
                creds = self._require_credentials()
                buffer = ""
                self._send(creds["username"])
            elif _PASSWORD_RE.search(line):
                # After "Username:" or a plain line password: the local administrator's
                # password is the only console password AgenticOps knows.
                creds = self._require_credentials()
                buffer = ""
                self._send(creds["password"])
            else:
                # Boot still running, "Press RETURN", or a silent console.
                buffer = "" if _RETURN_RE.search(buffer) else buffer
                self._send("")
        raise ConsoleError(f"no IOS prompt on the console after {int(timeout)}s "
                           "(the device may still be booting)")

    def _require_credentials(self) -> dict[str, Any]:
        creds = self.credentials()
        if not creds:
            raise ConsoleLoginRequired(
                "the console asks for a login and no local administrator is saved; "
                "ask the user to run 'login local'. Never send usernames or passwords yourself.")
        return creds

    def enable(self) -> None:
        if self.mode == "config":
            self.run("end")
        if self.mode == "privileged":
            return
        self._send("enable")
        out = self._wait(15.0, answer_password=True)
        if self.mode != "privileged":
            raise ConsoleError(f"could not enter privileged mode: {_last_line(out)!r}")

    # -- commands --------------------------------------------------------------------

    def _wait(self, timeout: float, *, confirm: bool = False,
              answer_password: bool = False) -> str:
        """Read until a prompt; answer pagers and confirmations. Returns the output."""
        deadline = self._clock() + timeout
        out = ""
        while self._clock() < deadline:
            chunk = self._read(self.poll)
            out += chunk
            line = _last_line(out)
            if "\n" in out and self._set_prompt(line):
                return out
            if chunk and not _MORE_RE.search(out):
                continue  # output still flowing
            if _MORE_RE.search(out):
                out = _MORE_RE.sub("", out)
                self.t.write(b" ")
            elif _PASSWORD_RE.search(line):
                if not answer_password:
                    # Never forward anything else to a password prompt: abort it.
                    self.t.write(b"\x03")
                    self._send("")
                    raise ConsoleError("a command asked for a password; it was aborted. "
                                       "Credentials are handled only by the agent.")
                creds = self._require_credentials()
                self._send(creds.get("secret") or creds["password"])
                answer_password = False
            elif _YES_NO_RE.search(line):
                self._send("yes" if confirm else "no")
            elif _CONFIRM_RE.search(line):
                self._send("" if confirm else "n")
        raise ConsoleError(f"timeout after {int(timeout)}s waiting for the prompt")

    def run(self, command: str, timeout: float = 30.0, *, confirm: bool = False) -> str:
        self._send(command)
        out = self._wait(timeout, confirm=confirm)
        # Drop the echoed command and the trailing prompt.
        lines = out.replace("\r", "").split("\n")
        if lines and lines[0].strip().endswith(command.strip()):
            lines = lines[1:]
        if lines and _PROMPT_RE.match(lines[-1].strip()):
            lines = lines[:-1]
        result = "\n".join(lines).strip("\n")
        self.transcript.append(f"{command}\n{result}".rstrip())
        return result

    def close(self) -> None:
        self.t.close()


def command_errors(output: str) -> list[str]:
    return _ERROR_RE.findall(output)


def open_console(host: str, port: int,
                 credentials: Callable[[], dict[str, Any] | None] = lambda: None,
                 **kwargs: Any) -> IOSConsole:
    return IOSConsole(TelnetTransport(host, port), credentials, **kwargs)