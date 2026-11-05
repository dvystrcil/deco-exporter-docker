"""Unit tests for the Deco exporter, against recorded fixture responses.

No network: the router client is a fake (conftest.FakeDecoClient) with the same
three methods the exporter calls on tplinkrouterc6u.TPLinkDecoClient.
"""

import logging
import urllib.request

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from conftest import CLIENT_LIST, DEVICE_LIST, PERFORMANCE, FakeFactory, default_responses
from deco_exporter.collector import DecoCollector
from deco_exporter.config import Config, ConfigError, load_config
from deco_exporter.poller import Poller

PASSWORD = "hunter2-not-real"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def cfg(**kw):
    base = dict(host="http://192.0.2.1", password=PASSWORD, username="admin",
                poll_interval=60, timeout=10, mesh_enabled=False, mesh_interval=600,
                port=0)
    base.update(kw)
    return Config(**base)


def render(poller, clock):
    reg = CollectorRegistry()
    reg.register(DecoCollector(poller, clock=clock))
    return generate_latest(reg).decode()


def samples(text):
    out = {}
    for fam in text_string_to_metric_families(text):
        for s in fam.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def value(text, _metric, **labels):
    return samples(text).get((_metric, tuple(sorted(labels.items()))))


def names(text):
    return {k[0] for k in samples(text)}


# ---------------------------------------------------------------- login

def test_logs_in_with_configured_credentials_before_any_read(factory):
    p = Poller(cfg(), factory, clock=Clock())
    p.poll_once()

    assert factory.calls[0] == dict(host="http://192.0.2.1", password=PASSWORD,
                                    username="admin", timeout=10)
    assert factory.log[0] == ("authorize",)
    assert factory.log[-1] == ("logout",), "the Deco allows one admin session; free it"


def test_client_list_request_body_is_the_one_ac1_verified(factory):
    Poller(cfg(), factory, clock=Clock()).poll_once()
    bodies = [e[2] for e in factory.log if e[0] == "request" and e[1] == CLIENT_LIST]
    assert bodies == [{"operation": "read", "params": {"device_mac": "default"}}]


def test_login_failure_is_a_failed_scrape_not_a_crash():
    f = FakeFactory(fail_login=Exception("Cannot authorize"))
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    p.poll_once()
    text = render(p, clock)
    assert value(text, "deco_scrape_success") == 0
    assert "deco_client_up_speed_raw" not in names(text)


# ---------------------------------------------------------------- parsing

def test_parses_online_clients_with_decoded_names(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    text = render(p, clock)

    assert value(text, "deco_scrape_success") == 1
    lab = dict(mac="cf:51:c9:04:e1:02", ip="192.168.68.101", name="wireless1",
               connection="band5", interface="main")
    assert value(text, "deco_client_up_speed_raw", **lab) == 17
    assert value(text, "deco_client_down_speed_raw", **lab) == 3

    wired = dict(mac="5f:f8:08:28:af:54", ip="192.168.68.100", name="wired1",
                 connection="wired", interface="main")
    assert value(text, "deco_client_up_speed_raw", **wired) == 3


def test_offline_clients_get_no_rate_series_but_are_counted(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    text = render(p, clock)

    macs = {dict(k[1])["mac"] for k in samples(text) if k[0] == "deco_client_up_speed_raw"}
    assert "92:28:a8:b7:d5:e6" not in macs  # online: false in the fixture
    assert value(text, "deco_clients", state="online") == 5
    assert value(text, "deco_clients", state="offline") == 1


def test_missing_mac_and_unknown_ip_do_not_leak_placeholders(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    text = render(p, clock)
    lab = dict(mac="", ip="", name="wireless5", connection="band2_4", interface="guest")
    assert value(text, "deco_client_up_speed_raw", **lab) == 3


def test_online_falls_back_to_active_when_firmware_omits_online():
    r = default_responses()
    r[CLIENT_LIST] = {"client_list": [
        {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.86.10", "name": "cGhvbmU=",
         "up_speed": 5, "down_speed": 9, "active": True},
        {"mac": "aa:bb:cc:dd:ee:02", "ip": "192.168.86.11", "name": "dHY=",
         "up_speed": 0, "down_speed": 0, "active": False},
    ]}
    f = FakeFactory(responses=r)
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    p.poll_once()
    text = render(p, clock)
    assert value(text, "deco_clients", state="online") == 1
    lab = dict(mac="aa:bb:cc:dd:ee:01", ip="192.168.86.10", name="phone",
               connection="", interface="")
    assert value(text, "deco_client_down_speed_raw", **lab) == 9


def test_non_base64_name_is_kept_as_is():
    r = default_responses()
    r[CLIENT_LIST] = {"client_list": [
        {"mac": "aa:bb:cc:dd:ee:01", "ip": "192.168.86.10", "name": "living-room tv!",
         "up_speed": 1, "down_speed": 2, "online": True}]}
    f = FakeFactory(responses=r)
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    p.poll_once()
    assert "living-room tv!" in render(p, clock)


def test_router_cpu_and_memory(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    text = render(p, clock)
    assert value(text, "deco_cpu_usage") == pytest.approx(0.1)
    assert value(text, "deco_memory_usage") == pytest.approx(0.43)


# ---------------------------------------------------------------- failed scrape

def test_failed_scrape_drops_rates_and_keeps_last_success_time():
    r = default_responses()
    ok = r[CLIENT_LIST]
    r[CLIENT_LIST] = [ok, Exception("empty"), Exception("empty")]
    f = FakeFactory(responses=r)
    clock = Clock(1000.0)
    p = Poller(cfg(), f, clock=clock)

    p.poll_once()
    assert value(render(p, clock), "deco_last_success_timestamp_seconds") == 1000.0

    clock.t = 1060.0
    p.poll_once()
    text = render(p, clock)
    assert value(text, "deco_scrape_success") == 0
    assert value(text, "deco_last_success_timestamp_seconds") == 1000.0
    # No stale rates: a failed scrape must not look like the last good one,
    # and must not look like zero traffic either -- the series are absent.
    assert not {"deco_client_up_speed_raw", "deco_client_down_speed_raw",
                "deco_cpu_usage", "deco_memory_usage", "deco_clients"} & names(text)


def test_no_success_yet_means_no_last_success_timestamp():
    f = FakeFactory(fail_login=Exception("down"))
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    p.poll_once()
    assert "deco_last_success_timestamp_seconds" not in names(render(p, clock))


def test_read_error_reauthenticates_once_and_recovers():
    r = default_responses()
    r[CLIENT_LIST] = [Exception("empty response"), r[CLIENT_LIST]]
    f = FakeFactory(responses=r)
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    p.poll_once()

    assert [e for e in f.log if e[0] == "authorize"] == [("authorize",)] * 2
    assert len(f.calls) == 2, "a fresh client (new session) for the retry"
    assert value(render(p, clock), "deco_scrape_success") == 1


def test_second_consecutive_error_gives_up_for_this_poll():
    r = default_responses()
    r[CLIENT_LIST] = [Exception("a"), Exception("b"), r[CLIENT_LIST]]
    f = FakeFactory(responses=r)
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    p.poll_once()
    assert len(f.calls) == 2  # one retry, not a loop
    assert value(render(p, clock), "deco_scrape_success") == 0


def test_performance_error_fails_the_scrape_too():
    r = default_responses()
    r[PERFORMANCE] = [Exception("x"), Exception("y")]
    f = FakeFactory(responses=r)
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    p.poll_once()
    assert value(render(p, clock), "deco_scrape_success") == 0


def test_logout_failure_does_not_fail_a_good_scrape(factory, monkeypatch):
    import conftest

    def bad_logout(self):
        raise Exception("logout blew up")
    monkeypatch.setattr(conftest.FakeDecoClient, "logout", bad_logout)
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    assert value(render(p, clock), "deco_scrape_success") == 1


def test_a_stalled_poller_reports_failure_not_frozen_data(factory):
    clock = Clock(1000.0)
    p = Poller(cfg(poll_interval=60), factory, clock=clock)
    p.poll_once()
    clock.t = 1000.0 + 3 * 60 + 1  # three intervals with no poll attempt
    text = render(p, clock)
    assert value(text, "deco_scrape_success") == 0
    assert "deco_client_up_speed_raw" not in names(text)
    assert value(text, "deco_last_success_timestamp_seconds") == 1000.0


def test_password_never_reaches_logs_or_metrics(caplog):
    f = FakeFactory(fail_login=Exception(f"Cannot authorize! password={PASSWORD}"))
    clock = Clock()
    p = Poller(cfg(), f, clock=clock)
    with caplog.at_level(logging.DEBUG):
        p.poll_once()
    assert caplog.records, "a failed scrape should be logged"
    assert PASSWORD not in caplog.text
    assert PASSWORD not in render(p, clock)


def test_scrape_duration_is_exported(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    assert value(render(p, clock), "deco_scrape_duration_seconds") is not None


# ---------------------------------------------------------------- backhaul safety (AC8 off by default)

ALLOWED_DEFAULT = {CLIENT_LIST, PERFORMANCE}


def test_default_config_never_reads_device_list(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    for i in range(20):
        clock.t += 60
        p.poll_once()
    assert set(factory.paths()) <= ALLOWED_DEFAULT


def test_default_config_never_reads_device_list_even_on_the_retry_path():
    r = default_responses()
    r[CLIENT_LIST] = [Exception("x"), r[CLIENT_LIST]]
    f = FakeFactory(responses=r)
    Poller(cfg(), f, clock=Clock()).poll_once()
    assert set(f.paths()) <= ALLOWED_DEFAULT


def test_mesh_stats_when_enabled_are_read_on_their_own_slow_interval():
    f = FakeFactory()
    clock = Clock(0.0)
    p = Poller(cfg(mesh_enabled=True, mesh_interval=600), f, clock=clock)
    for _ in range(21):  # 0, 60, ... 1200 s
        p.poll_once()
        clock.t += 60
    assert f.paths().count(DEVICE_LIST) == 3  # t=0, 600, 1200


def test_mesh_stats_metrics():
    f = FakeFactory()
    clock = Clock()
    p = Poller(cfg(mesh_enabled=True), f, clock=clock)
    p.poll_once()
    text = render(p, clock)

    assert value(text, "deco_mesh_scrape_success") == 1
    kids = "f0:09:0d:fa:29:84"
    assert value(text, "deco_mesh_node_info", mac=kids, name="Kids", role="slave",
                 model="X50", parent_mac="f0:09:0d:fa:29:7c",
                 firmware="1.8.0 Build 25102213 Rel. 43970") == 1
    assert value(text, "deco_mesh_node_connected", mac=kids) == 1
    assert value(text, "deco_mesh_node_backhaul_signal_dbm", mac=kids, band="band5") == -50
    assert value(text, "deco_mesh_node_backhaul_rate_raw", mac=kids, band="band5",
                 direction="rx") == 960
    assert value(text, "deco_mesh_node_backhaul_rate_raw", mac=kids, band="band2_4",
                 direction="tx") == 258
    # The master has no uplink: no backhaul series, not zeros.
    master = "f0:09:0d:fa:29:7c"
    assert value(text, "deco_mesh_node_backhaul_signal_dbm", mac=master, band="band5") is None


def test_mesh_failure_does_not_fail_the_client_scrape():
    r = default_responses()
    r[DEVICE_LIST] = Exception("device_list failed")
    f = FakeFactory(responses=r)
    clock = Clock()
    p = Poller(cfg(mesh_enabled=True), f, clock=clock)
    p.poll_once()
    text = render(p, clock)
    assert value(text, "deco_scrape_success") == 1
    assert value(text, "deco_mesh_scrape_success") == 0
    assert "deco_mesh_node_info" not in names(text)
    assert f.paths().count(DEVICE_LIST) == 1, "no re-login retry storm on the risky call"


def test_mesh_disabled_exports_no_mesh_series(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    assert not any(n.startswith("deco_mesh_node") or n == "deco_mesh_scrape_success"
               for n in names(render(p, clock)))


def test_mesh_enabled_flag_is_itself_exported(factory):
    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    assert value(render(p, clock), "deco_mesh_stats_enabled") == 0


# ---------------------------------------------------------------- config

def test_config_defaults():
    c = load_config({"DECO_PASSWORD": "x"})
    assert c.host == "http://192.168.86.1"
    assert c.username == "admin"
    assert c.poll_interval == 60
    assert c.timeout == 10
    assert c.mesh_enabled is False
    assert c.mesh_interval == 600
    assert c.port == 9850


def test_config_requires_password():
    with pytest.raises(ConfigError, match="DECO_PASSWORD"):
        load_config({})


def test_config_rejects_mesh_interval_below_five_minutes():
    with pytest.raises(ConfigError, match="DECO_MESH_INTERVAL_SECONDS"):
        load_config({"DECO_PASSWORD": "x", "DECO_MESH_INTERVAL_SECONDS": "60"})


def test_config_rejects_poll_interval_below_fifteen_seconds():
    with pytest.raises(ConfigError, match="DECO_POLL_INTERVAL_SECONDS"):
        load_config({"DECO_PASSWORD": "x", "DECO_POLL_INTERVAL_SECONDS": "5"})


@pytest.mark.parametrize("raw,want", [("true", True), ("1", True), ("false", False),
                                      ("", False), ("no", False)])
def test_config_mesh_flag(raw, want):
    assert load_config({"DECO_PASSWORD": "x", "DECO_MESH_STATS_ENABLED": raw}).mesh_enabled is want


def test_config_repr_hides_password():
    c = load_config({"DECO_PASSWORD": PASSWORD})
    assert PASSWORD not in repr(c)


# ---------------------------------------------------------------- the real client + HTTP

def test_real_factory_builds_the_pinned_library_client():
    from tplinkrouterc6u import TPLinkDecoClient
    from deco_exporter.poller import make_deco_client

    c = make_deco_client("http://192.0.2.1", PASSWORD, "admin", 7)
    assert isinstance(c, TPLinkDecoClient)
    assert c.host == "http://192.0.2.1"
    assert c.timeout == 7


def test_http_server_serves_metrics_and_healthz(factory):
    from deco_exporter.main import serve

    clock = Clock()
    p = Poller(cfg(), factory, clock=clock)
    p.poll_once()
    server, port = serve(p, port=0, addr="127.0.0.1")
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
        assert "deco_scrape_success" in body
        hz = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert hz.status == 200
    finally:
        server.shutdown()
