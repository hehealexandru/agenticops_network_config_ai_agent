"""A small simulated Cisco IOS console, used as the transport in console tests."""
from __future__ import annotations


class FakeIOS:
    def __init__(self, *, boot_reads=0, dialog=True, login=None, enable_secret=None,
                 has_rsa_keys=False, rsa_bits=2048, dhcp_ip="192.168.122.227",
                 hostname="Router"):
        self.boot_reads = boot_reads
        self.state = "boot" if boot_reads else ("dialog" if dialog else "return")
        self.login = login                      # (username, password) or None
        self.enable_secret = enable_secret
        self.has_rsa_keys = has_rsa_keys
        self.rsa_bits = rsa_bits
        self.dhcp_ip = dhcp_ip
        self.hostname = hostname
        self.mode = "user"
        self.config_iface = None
        self.ifaces = {"FastEthernet0/0": None, "FastEthernet1/0": None}
        self.iface_up = set()
        self.ssh_version_2 = False
        self.config_lines = []                  # everything typed in config mode
        self.saved = False
        self.pending = None                     # what a [yes/no] or Password: is waiting for
        self.login_user = None
        self.closed = False
        self.typed = []                         # raw lines received
        self.out = []
        if self.state == "dialog" and not boot_reads:
            self._emit("\r\n--- System Configuration Dialog ---\r\n\r\n"
                       "Would you like to enter the initial configuration dialog? [yes/no]: ")

    # -- transport interface ---------------------------------------------------------

    def write(self, data: bytes) -> None:
        text = data.decode("ascii", errors="ignore")
        if text in (" ", "\x03"):
            return
        for line in text.split("\r")[:-1] or [text]:
            self.typed.append(line)
            self._line(line)

    def read(self, timeout: float) -> bytes:
        if self.state == "boot":
            self.boot_reads -= 1
            if self.boot_reads <= 0:
                self.state = "dialog"
                return (b"Cisco IOS Software, 7200 Software, Version 12.4(24)T5\r\n"
                        b"--- System Configuration Dialog ---\r\n\r\n"
                        b"Would you like to enter the initial configuration dialog? [yes/no]: ")
            return b"Launching IOS image at 0x80008000...\r\n"
        if not self.out:
            return b""
        data, self.out = "".join(self.out), []
        return data.encode()

    def close(self) -> None:
        self.closed = True

    # -- simulation ------------------------------------------------------------------

    def _emit(self, text):
        self.out.append(text)

    def prompt(self):
        if self.mode == "config":
            sub = "config-if" if self.config_iface else ("config-line" if self.pending == "line"
                                                         else "config")
            return f"{self.hostname}({sub})#"
        return f"{self.hostname}{'#' if self.mode == 'privileged' else '>'}"

    def _line(self, line):
        cmd = line.strip()
        if self.state == "boot":
            return
        if self.state == "dialog":
            if cmd.lower() == "no":
                self.state = "return"
                self._emit("no\r\n\r\nPress RETURN to get started!\r\n\r\n")
            else:
                self._emit(f"{cmd}\r\n% Please answer 'yes' or 'no'.\r\n"
                           "Would you like to enter the initial configuration dialog? [yes/no]: ")
            return
        if self.state == "return":
            self.state = "login" if self.login else "cli"
            if self.login:
                self._emit("\r\nUser Access Verification\r\n\r\nUsername: ")
            else:
                self._emit("\r\n" + self.prompt())
            return
        if self.state == "login":
            if self.login_user is None:
                self.login_user = cmd
                self._emit(f"{cmd}\r\nPassword: ")
            else:
                ok = (self.login_user, cmd) == self.login
                self.login_user = None
                if ok:
                    self.state = "cli"
                    self.mode = "privileged"
                    self._emit("\r\n" + self.prompt())
                else:
                    self._emit("\r\n% Login invalid\r\n\r\nUsername: ")
            return
        if self.pending == "enable":
            self.pending = None
            if cmd == self.enable_secret:
                self.mode = "privileged"
                self._emit("\r\n" + self.prompt())
            else:
                self._emit("\r\n% Bad secrets\r\n\r\n" + self.prompt())
            return
        if self.pending == "rsa":
            self.pending = None
            self._emit(f"{cmd}\r\n")
            if cmd == "yes":
                self.has_rsa_keys, self.rsa_bits = True, 2048
                self._emit("% Generating 2048 bit RSA keys, keys will be non-exportable...[OK]\r\n")
            self._emit(self.prompt())
            return
        self._emit(f"{cmd}\r\n")
        self._emit(self._command(cmd))
        if self.pending in ("enable", "rsa"):
            return
        self._emit(("\r\n" if not self.out[-1].endswith("\n") else "") + self.prompt())

    def _command(self, cmd):
        if not cmd:
            return ""
        if self.mode != "config":
            if cmd in ("enable", "en"):
                if self.mode == "privileged":
                    return ""
                if self.enable_secret:
                    self.pending = "enable"
                    return "Password: "
                self.mode = "privileged"
                return ""
            if self.mode == "user" and not cmd.startswith(("show", "ping", "terminal")):
                return "% Invalid input detected at '^' marker.\r\n"
            if cmd in ("configure terminal", "conf t"):
                self.mode = "config"
                return "Enter configuration commands, one per line.  End with CNTL/Z.\r\n"
            if cmd == "terminal length 0":
                return ""
            if cmd in ("write memory", "write"):
                self.saved = True
                return "Building configuration...\r\n[OK]\r\n"
            if cmd == "show ip ssh":
                if self.has_rsa_keys:
                    ver = "2.0" if self.ssh_version_2 else "1.99"
                    return f"SSH Enabled - version {ver}\r\nAuthentication timeout: 120 secs\r\n"
                return "SSH Disabled - version 1.99\r\n%Please create RSA keys to enable SSH.\r\n"
            if cmd == "show ip interface brief":
                rows = ["Interface                  IP-Address      OK? Method Status                Protocol"]
                for name, ip in self.ifaces.items():
                    up = name in self.iface_up
                    rows.append(f"{name:<27}{ip or 'unassigned':<16}YES {'DHCP' if ip else 'unset':<6} "
                                f"{'up' if up else 'administratively down':<22}{'up' if up else 'down'}")
                return "\r\n".join(rows) + "\r\n"
            if cmd.startswith("show"):
                return ""
            return "% Invalid input detected at '^' marker.\r\n"

        # configuration mode
        self.config_lines.append(cmd)
        if cmd == "end":
            self.mode, self.config_iface = "privileged", None
            return ""
        if cmd == "exit":
            if self.config_iface or self.pending == "line":
                self.config_iface = None
                self.pending = None
            else:
                self.mode = "privileged"
            return ""
        if cmd.startswith("hostname "):
            self.hostname = cmd.split()[1]
            return ""
        if cmd.startswith("crypto key generate rsa"):
            if self.has_rsa_keys:
                self.pending = "rsa"
                return (f"% You already have RSA keys defined named {self.hostname}.lab.local.\r\n"
                        "% Do you really want to replace them? [yes/no]: ")
            self.has_rsa_keys, self.rsa_bits = True, 2048
            return ("The name for the keys will be: R1.lab.local\r\n"
                    "% Generating 2048 bit RSA keys, keys will be non-exportable...[OK]\r\n")
        if cmd == "ip ssh version 2":
            if not self.has_rsa_keys or self.rsa_bits < 768:
                return "Please create RSA keys (of atleast 768 bits size) to enable SSH v2.\r\n"
            self.ssh_version_2 = True
            return ""
        if cmd.startswith("line vty"):
            self.pending = "line"
            return ""
        if cmd.startswith("interface "):
            name = cmd.split()[1]
            full = {"f1/0": "FastEthernet1/0", "f0/0": "FastEthernet0/0"}.get(name.lower(), name)
            if full not in self.ifaces:
                return "% Invalid input detected at '^' marker.\r\n"
            self.config_iface = full
            return ""
        if cmd == "ip address dhcp" and self.config_iface:
            self.ifaces[self.config_iface] = self.dhcp_ip
            return ""
        if cmd.startswith("ip address ") and self.config_iface:
            self.ifaces[self.config_iface] = cmd.split()[2]
            return ""
        if cmd == "no shutdown" and self.config_iface:
            self.iface_up.add(self.config_iface)
            return ""
        if cmd.startswith(("ip domain-name", "username ", "enable secret", "login local",
                           "transport input")):
            return ""
        return "% Invalid input detected at '^' marker.\r\n"