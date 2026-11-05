"""Renders the poller's latest state as Prometheus metrics.

Units: the Deco's `up_speed`/`down_speed` have no documented unit, and the
router keeps no byte counters (homelab#1931 AC1). Until a calibration against
a known transfer pins the unit, the rates are exported as-is under a `_raw`
suffix rather than under a name that claims bytes per second.

A failed or stale poll exports deco_scrape_success 0 and **no** rate series --
never the last good values (which would hide the outage) and never zeros
(which would look like a quiet network).
"""

import time

from prometheus_client.core import GaugeMetricFamily

CLIENT_LABELS = ["mac", "ip", "name", "connection", "interface"]
STALE_AFTER_INTERVALS = 3


class DecoCollector:
    def __init__(self, poller, clock=time.time):
        self.poller = poller
        self.clock = clock

    def _stale(self, s):
        if s.last_attempt is None:
            return True
        return self.clock() - s.last_attempt > STALE_AFTER_INTERVALS * self.poller.config.poll_interval

    def collect(self):
        s = self.poller.snapshot()
        stale = self._stale(s)
        ok = s.success and not stale

        yield GaugeMetricFamily(
            "deco_scrape_success",
            "1 if the last poll of the Deco API succeeded and is recent, else 0.",
            value=1 if ok else 0)

        if s.last_success is not None:
            yield GaugeMetricFamily(
                "deco_last_success_timestamp_seconds",
                "Unix time of the last successful poll of the Deco API.",
                value=s.last_success)

        if s.duration is not None:
            yield GaugeMetricFamily(
                "deco_scrape_duration_seconds",
                "Wall time of the last poll (login, reads, logout).",
                value=s.duration)

        yield GaugeMetricFamily(
            "deco_mesh_stats_enabled",
            "1 if the opt-in mesh (device_list) read is enabled (homelab#1931 AC8).",
            value=1 if self.poller.config.mesh_enabled else 0)

        if ok:
            yield from self._clients(s)
            yield from self._router(s)
        if not stale and s.mesh_success is not None:
            yield from self._mesh(s)

    def _clients(self, s):
        up = GaugeMetricFamily(
            "deco_client_up_speed_raw",
            "Client upload rate as reported by the Deco (up_speed). UNIT UNCONFIRMED "
            "until calibrated (homelab#1931); online clients only.", labels=CLIENT_LABELS)
        down = GaugeMetricFamily(
            "deco_client_down_speed_raw",
            "Client download rate as reported by the Deco (down_speed). UNIT UNCONFIRMED "
            "until calibrated (homelab#1931); online clients only.", labels=CLIENT_LABELS)
        seen = set()
        online = offline = 0
        for c in s.clients:
            if not c.online:
                offline += 1
                continue
            online += 1
            labels = [c.mac, c.ip, c.name, c.connection, c.interface]
            key = tuple(labels)
            if key in seen:  # a duplicate series would make the whole scrape fail
                continue
            seen.add(key)
            if c.up is not None:
                up.add_metric(labels, c.up)
            if c.down is not None:
                down.add_metric(labels, c.down)
        yield up
        yield down

        n = GaugeMetricFamily("deco_clients", "Clients in the Deco's client list.",
                              labels=["state"])
        n.add_metric(["online"], online)
        n.add_metric(["offline"], offline)
        yield n

    def _router(self, s):
        if s.cpu is not None:
            yield GaugeMetricFamily(
                "deco_cpu_usage",
                "Main Deco CPU usage as reported (cpu_usage; a 0-1 fraction in the "
                "library's fixtures, unconfirmed on the BE85).", value=s.cpu)
        if s.mem is not None:
            yield GaugeMetricFamily(
                "deco_memory_usage",
                "Main Deco memory usage as reported (mem_usage; a 0-1 fraction in the "
                "library's fixtures, unconfirmed on the BE85).", value=s.mem)

    def _mesh(self, s):
        yield GaugeMetricFamily(
            "deco_mesh_scrape_success",
            "1 if the last mesh (device_list) read succeeded.",
            value=1 if s.mesh_success else 0)
        if not s.mesh_success:
            return

        info = GaugeMetricFamily(
            "deco_mesh_node_info", "A Deco unit in the mesh.",
            labels=["mac", "name", "role", "model", "parent_mac", "firmware"])
        conn = GaugeMetricFamily(
            "deco_mesh_node_connected", "1 if the unit's group_status is connected.",
            labels=["mac"])
        sig = GaugeMetricFamily(
            "deco_mesh_node_backhaul_signal_dbm",
            "Signal strength of the unit's uplink to its parent, per band.",
            labels=["mac", "band"])
        rate = GaugeMetricFamily(
            "deco_mesh_node_backhaul_rate_raw",
            "Link rate of the unit's uplink to its parent, per band and direction, "
            "as reported (rx_rate_list/tx_rate_list; unit unconfirmed).",
            labels=["mac", "band", "direction"])
        for node in s.mesh_nodes:
            info.add_metric([node.mac, node.name, node.role, node.model, node.parent_mac,
                             node.firmware], 1)
            conn.add_metric([node.mac], 1 if node.connected else 0)
            for band, v in sorted(node.signal.items()):
                sig.add_metric([node.mac, band], v)
            for band, v in sorted(node.rx_rate.items()):
                rate.add_metric([node.mac, band, "rx"], v)
            for band, v in sorted(node.tx_rate.items()):
                rate.add_metric([node.mac, band, "tx"], v)
        yield info
        yield conn
        yield sig
        yield rate
