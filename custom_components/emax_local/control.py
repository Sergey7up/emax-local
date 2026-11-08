"""Bounded HF-LPT230 management, used only after a user requests setup/restart.

Only SOCKB and AT+Z may be written. Recovery files precede any mutation.
The synchronous functions run in HA's executor, never on its event loop.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import socket
import time

CONTROL_PORT = 48899
DISCOVERY = b"HF-A11ASSISTHREAD"
READ_COMMANDS = {"VER", "MID", "NETP", "SOCKB", "TMODE", "UART", "TCPLKB"}


class ControlError(Exception):
    """A bounded management operation failed or could not be verified."""


class IdentityError(ControlError):
    """The station is not the expected device."""


class ConflictError(ControlError):
    """Settings changed after the setup form was displayed."""


def normalize(value: str) -> str:
    return ",".join(part.strip().upper() for part in value.split(","))


def ipv4(value: str) -> str:
    address = IPv4Address(value.strip())
    if address.is_loopback or address.is_multicast or address.is_unspecified or int(address) == 0xFFFFFFFF:
        raise ValueError("A unicast LAN IPv4 address is required")
    return str(address)


def socket_b(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
        raise ControlError("Invalid Socket B value")
    if value.strip().upper() == "NONE":
        return "NONE"
    parts = value.split(",")
    if len(parts) != 3 or parts[0].upper() not in {"TCP", "UDP", "UDPS"}:
        raise ControlError("Unsupported Socket B format")
    if not parts[1].isdigit() or not 0 < int(parts[1]) < 65535:
        raise ControlError("Invalid Socket B port")
    destination = parts[2].strip()
    if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", destination):
        raise ControlError("Invalid Socket B destination")
    return f"{parts[0].upper()},{int(parts[1])},{destination}"


@dataclass(frozen=True)
class StationInfo:
    host: str
    mac: str
    module: str
    firmware: str
    netp: str
    sockb: str
    tmode: str
    uart: str


class ControlSession:
    def __init__(self, host: str, expected_mac: str | None = None, *,
                 timeout: float = 1.5, remote_port: int = CONTROL_PORT) -> None:
        self.host = host
        self.expected_mac = expected_mac
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(("0.0.0.0", 0))
            self.sock.connect((host, remote_port))
        except BaseException:
            self.sock.close()
            raise
        self.clean = False

    def __enter__(self):
        try:
            self.mac, self.module = self.discover()
            return self
        except BaseException:
            self.sock.close()
            raise

    def __exit__(self, *_):
        self.sock.close()

    def receive(self, timeout: float, quiet: float = 0.12) -> list[bytes]:
        packets = []
        deadline = quiet_deadline = time.monotonic() + timeout
        size = 0
        while len(packets) < 16 and size < 8192:
            remaining = min(deadline, quiet_deadline) - time.monotonic()
            if remaining <= 0:
                break
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(8193)
            except socket.timeout:
                break
            size += len(data)
            packets.append(data)
            quiet_deadline = time.monotonic() + quiet
        return packets

    def discover(self) -> tuple[str, str]:
        for _ in range(2):
            self.sock.send(DISCOVERY)
            for packet in self.receive(self.timeout):
                try:
                    host, mac, module = packet.decode("ascii").strip("\r\n\0 ").split(",", 2)
                except (ValueError, UnicodeDecodeError):
                    continue
                mac = mac.replace(":", "").replace("-", "").upper()
                if host != self.host or not re.fullmatch(r"[0-9A-F]{12}", mac):
                    continue
                if module.upper() != "HF-LPT230" or (self.expected_mac and mac != self.expected_mac):
                    raise IdentityError("Unexpected MAC address or Wi-Fi module")
                self.sock.send(b"+ok")
                self.receive(0.15)
                self.clean = True
                return mac, module
        raise ControlError("No HF-LPT230 reply on UDP 48899")

    def exchange(self, command: str, *, setter: bool = False) -> str:
        if not self.clean:
            raise ControlError("A new control session is required")
        self.clean = False
        if self.receive(0.03, 0.03):
            raise ControlError("Late control response; refusing ambiguous session")
        self.sock.send(command.encode("ascii") + b"\r")
        raw = b"".join(self.receive(self.timeout)).decode("ascii", "replace")
        lines = [s.strip("\0 ") for s in raw.replace("\r", "\n").split("\n")]
        lines = [s for s in lines if s and s.upper() != command.upper()]
        if not lines:
            raise ControlError("No response to " + command.split("=")[0])
        if setter:
            if any(s.lower() != "+ok" for s in lines):
                raise ControlError("Socket B write was not acknowledged")
            value = ""
        else:
            values = {s[4:].strip() for s in lines if s.lower().startswith("+ok=")}
            if len(values) != 1 or not next(iter(values)) or any(not s.lower().startswith("+ok=") for s in lines):
                raise ControlError("Ambiguous control response")
            value = values.pop()
        self.clean = True
        return value

    def query(self, name: str) -> str:
        if name not in READ_COMMANDS:
            raise ValueError("Command is outside the read allowlist")
        return self.exchange("AT+" + name)

    def set_b(self, value: str) -> None:
        self.exchange("AT+SOCKB=" + socket_b(value), setter=True)

    def restart(self) -> None:
        if not self.clean or self.receive(0.03, 0.03):
            raise ControlError("Ambiguous session before restart")
        self.clean = False
        self.sock.send(b"AT+Z\r")
        # A missing ACK is normal if the module restarts immediately. Never replay.
        try:
            replies = self.receive(0.4)
        except OSError:
            replies = []
        if any(b"err" in reply.lower() for reply in replies):
            raise ControlError("AT+Z was rejected")


def inspect_station(host: str, expected_mac: str | None = None) -> StationInfo:
    host = ipv4(host)
    with ControlSession(host, expected_mac) as session:
        values = {name: session.query(name) for name in ("VER", "MID", "NETP", "SOCKB", "TMODE", "UART")}
        if values["MID"].upper() != session.module.upper():
            raise IdentityError("Module identification does not match discovery")
        if values["TMODE"].lower() != "throughput":
            raise ControlError("Station is not in transparent UART mode")
        return StationInfo(host, session.mac, session.module, values["VER"],
                           values["NETP"], socket_b(values["SOCKB"]), values["TMODE"], values["UART"])


def _save_exclusive(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())


def configure_station(info: StationInfo, target: str, port: int, backup_dir: str,
                      operation_id: str, *, restart_only: bool = False,
                      session_factory=ControlSession, wait_seconds: float = 30,
                      settle_seconds: float = 2) -> dict:
    """Snapshot-check, save recovery, write B, restart once, verify A/B and MAC.

    Operation IDs are single-use. A failed or interrupted operation is retried
    only by a new user action. A listener must already be running at target:port.
    """
    target = ipv4(target)
    if target == info.host or isinstance(port, bool) or not 1024 <= port <= 65534:
        raise ControlError("Invalid Home Assistant receiver address or port")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", operation_id):
        raise ValueError("Invalid operation id")
    desired = info.sockb if restart_only else f"TCP,{port},{target}"

    def check(session, allowed_b):
        b, a = session.query("SOCKB"), session.query("NETP")
        if normalize(a) != normalize(info.netp) or normalize(b) not in {normalize(x) for x in allowed_b}:
            raise ConflictError("A or B changed since discovery; no further changes made")
        return b, a

    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{info.mac}_{operation_id}.json"
    with session_factory(info.host, info.mac) as session:
        check(session, (info.sockb,))
    recovery = {"schema": "emax_b_setup_restore_v1", "created_utc": datetime.now(timezone.utc).isoformat(),
                "host": info.host, "mac": info.mac, "firmware": info.firmware,
                "original_netp": info.netp, "original_sockb": info.sockb, "target_sockb": desired}
    try:
        _save_exclusive(path, recovery)
    except FileExistsError as exc:
        raise ControlError("This setup operation already ran; use a new user action") from exc

    def phase(name: str, **extra):
        _save_exclusive(directory / f"{info.mac}_{operation_id}_{name}.json",
                        {"phase": name, "utc": datetime.now(timezone.utc).isoformat(), **extra})

    try:
        if normalize(info.sockb) != normalize(desired):
            with session_factory(info.host, info.mac) as session:
                check(session, (info.sockb,))
                phase("write_requested")
                try:
                    session.set_b(desired)
                except (OSError, ControlError):
                    pass  # Read back in a fresh session to resolve a lost ACK.
        with session_factory(info.host, info.mac) as session:
            check(session, (desired,))
            phase("restart_requested")
            session.restart()
        time.sleep(settle_seconds)
        deadline = time.monotonic() + wait_seconds
        error = "Module did not return after AT+Z"
        while time.monotonic() < deadline:
            try:
                with session_factory(info.host, info.mac) as session:
                    b, a = check(session, (desired,))
                    link = session.query("TCPLKB")
                result = {"result": "verified", "sockb": b, "netp": a, "tcp_link": link,
                          "recovery_file": str(path), "firmware": info.firmware}
                phase("verified", **result)
                return result
            except (IdentityError, ConflictError):
                raise
            except (OSError, ControlError) as exc:
                error = str(exc)
            time.sleep(0.5)
        raise ControlError(error)
    except (OSError, ControlError) as exc:
        phase("unverified", error=str(exc)[:200])
        raise ControlError(f"{exc}; recovery: {path.name}") from exc
