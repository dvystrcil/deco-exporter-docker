"""Polls the Deco's local admin API on a fixed interval.

Each poll is one short session: log in, read, log out. The Deco allows a
single admin session, so holding one open between polls would lock the owner
out of the Deco app; and in AC1 (homelab#1931) a read failed with an empty
response about 20 s after login, so a session should not be assumed to last
until the next poll anyway.

Only two endpoints are read by default:

  admin/client?form=client_list   per-client up/down rates
  admin/network?form=performance  router CPU and memory

admin/device?form=device_list (mesh topology, AC8) is read only when
DECO_MESH_STATS_ENABLED is set, on its own slow interval, and never retried.
It is the call reported to interrupt wireless backhaul on some firmware.
Nothing here calls the library's get_status(), get_firmware() or
get_mesh_nodes(): each of those makes extra reads we don't want.
"""

import base64
import binascii
import json
import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("deco_exporter")

CLIENT_LIST = "admin/client?form=client_list"
PERFORMANCE = "admin/network?form=performance"
DEVICE_LIST = "admin/device?form=device_list"

CLIENT_LIST_BODY = json.dumps({"operation": "read", "params": {"device_mac": "default"}})
READ_BODY = json.dumps({"operation": "read"})


def make_deco_client(host, password, username, timeout):
    """The real client: tplinkrouterc6u's TPLinkDecoClient (RSA/AES `luci` login)."""
    from tplinkrouterc6u import TPLinkDecoClient

    return TPLinkDecoClient(host, password, username=username, timeout=timeout)


@dataclass
class Client:
    mac: str
    ip: str
    name: str
    connection: str
    interface: str
    online: bool
    up: float | None
    down: float | None


@dataclass
class MeshNode:
    mac: str
    name: str
    role: str
    model: str
    parent_mac: str
    firmware: str
    connected: bool
    signal: dict = field(default_factory=dict)   # band -> dBm
    rx_rate: dict = field(default_factory=dict)  # band -> raw
    tx_rate: dict = field(default_factory=dict)  # band -> raw


@dataclass
class State:
    success: bool = False
    last_attempt: float | None = None
    last_success: float | None = None
    duration: float | None = None
    clients: list = field(default_factory=list)
    cpu: float | None = None
    mem: float | None = None
    mesh_success: bool | None = None  # None = not attempted yet
    mesh_nodes: list = field(default_factory=list)


def _mac(raw):
    if not raw:
        return ""
    return raw.strip().lower().replace("-", ":")


def _ip(raw):
    if not raw or raw.upper() == "UNKNOWN":
        return ""
    return raw


def _decode_name(raw):
    """Client names arrive base64-encoded; keep the raw text if they aren't."""
    if not raw:
        return ""
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return raw


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _online(item):
    if "online" in item:
        return bool(item["online"])
    if "active" in item:
        return bool(item["active"])
    return True


def parse_clients(result):
    out = []
    for item in (result or {}).get("client_list", []) or []:
        wired = item.get("wire_type") == "wired"
        out.append(Client(
            mac=_mac(item.get("mac")),
            ip=_ip(item.get("ip")),
            name=_decode_name(item.get("name")),
            connection="wired" if wired else (item.get("connection_type") or ""),
            interface=item.get("interface") or "",
            online=_online(item),
            up=_num(item.get("up_speed")),
            down=_num(item.get("down_speed")),
        ))
    return out


def _band_map(d):
    out = {}
    for band, v in (d or {}).items():
        n = _num(v)
        if n is not None:
            out[band] = n
    return out


def parse_mesh(result):
    nodes = []
    for item in (result or {}).get("device_list", []) or []:
        nodes.append(MeshNode(
            mac=_mac(item.get("mac")),
            name=item.get("nickname") or "",
            role=item.get("role") or "",
            model=item.get("device_model") or "",
            parent_mac=_mac(item.get("previous")),
            firmware=item.get("software_ver") or "",
            connected=item.get("group_status") == "connected",
            signal=_band_map(item.get("signal_strength")),
            rx_rate=_band_map(item.get("rx_rate_list")),
            tx_rate=_band_map(item.get("tx_rate_list")),
        ))
    return nodes


class Poller:
    def __init__(self, config, client_factory=make_deco_client, clock=time.time):
        self.config = config
        self.factory = client_factory
        self.clock = clock
        self.state = State()
        self._lock = threading.Lock()
        self._next_mesh = None

    # -- one session ------------------------------------------------------

    def _new_client(self):
        c = self.config
        client = self.factory(c.host, c.password, c.username, c.timeout)
        client.authorize()
        return client

    def _logout(self, client):
        try:
            client.logout()
        except Exception as e:  # a failed logout never fails a good read
            log.debug("logout failed: %s", self._scrub(e))

    def _read(self, client):
        clients = parse_clients(client.request(CLIENT_LIST, CLIENT_LIST_BODY))
        perf = client.request(PERFORMANCE, READ_BODY) or {}
        return clients, _num(perf.get("cpu_usage")), _num(perf.get("mem_usage"))

    def _scrub(self, err):
        msg = f"{type(err).__name__}: {err}"
        if self.config.password:
            msg = msg.replace(self.config.password, "<redacted>")
        return msg[:300]

    # -- a poll -----------------------------------------------------------

    def poll_once(self):
        start = self.clock()
        mono = time.monotonic()
        client = None
        result = None
        last_err = None

        # Re-authenticate on any error: a fresh client is a fresh session.
        # One retry, not a loop -- a router that is refusing us should be left
        # alone until the next interval.
        for attempt in (1, 2):
            try:
                client = self._new_client()
                result = self._read(client)
                break
            except Exception as e:
                last_err = e
                log.warning("deco read failed (attempt %d/2): %s", attempt, self._scrub(e))
                if client is not None:
                    self._logout(client)
                client = None

        mesh = None
        if result is not None and self._mesh_due(start):
            mesh = self._read_mesh(client)
        if client is not None:
            self._logout(client)

        duration = time.monotonic() - mono
        with self._lock:
            s = self.state
            s.last_attempt = start
            s.duration = duration
            if result is None:
                s.success = False
                s.clients, s.cpu, s.mem = [], None, None
                log.error("deco scrape failed: %s", self._scrub(last_err))
            else:
                s.success = True
                s.last_success = start
                s.clients, s.cpu, s.mem = result
            if mesh is not None:
                s.mesh_success, s.mesh_nodes = mesh

    def _mesh_due(self, now):
        if not self.config.mesh_enabled:
            return False
        if self._next_mesh is None or now >= self._next_mesh:
            self._next_mesh = now + self.config.mesh_interval
            return True
        return False

    def _read_mesh(self, client):
        # Never retried and never re-logged-in for: this is the risky call.
        try:
            return True, parse_mesh(client.request(DEVICE_LIST, READ_BODY))
        except Exception as e:
            log.warning("deco mesh read failed: %s", self._scrub(e))
            return False, []

    def snapshot(self):
        with self._lock:
            s = self.state
            return State(s.success, s.last_attempt, s.last_success, s.duration,
                         list(s.clients), s.cpu, s.mem, s.mesh_success, list(s.mesh_nodes))

    def run_forever(self, stop_event):
        while not stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # never let the loop die; the collector flags staleness
                log.exception("poll loop error")
            stop_event.wait(self.config.poll_interval)
