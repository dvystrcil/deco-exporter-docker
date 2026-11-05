"""bin/calibrate-units.py: pinning the unit of the Deco's up/down_speed.

The fixtures are three real runs from 2026-11-05 (homelab#1931). What they
taught, and what these pin:
  - the Deco's figure ramps over ~2 polls: ~400 at 64 s into a steady
    5 MiB/s download, about half at 108 s, full by 128 s. Early polls must
    be dropped or they drag the ratio to nonsense;
  - back-to-back runs are one continuous transfer, so the settle time counts
    from the first run's start, not each run's;
  - an unthrottled download's average is a poor match for an instantaneous
    sample: those ratios disagree with each other, and the verdict must say
    so instead of picking a unit.
"""
import importlib.util
import json
import sys

import pytest

from conftest import ROOT, FIXTURES

SPEC = importlib.util.spec_from_file_location("calibrate_units", ROOT / "bin" / "calibrate-units.py")
cu = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cu
SPEC.loader.exec_module(cu)


def fixture(name):
    return json.loads((FIXTURES / "calibration" / name).read_text())


def test_limited_run_pins_kbit_per_s():
    d = fixture("2026-11-05-limited.json")
    r = cu.analyze(d["runs"], d["polls"])
    assert [p["raw"] for p in r["steady"]] == [46070, 44244]
    assert r["verdict"] == "kbit/s"
    assert 0.85 < r["off_by"] < 1.0  # the Deco reads ~5-10% high (headers)


def test_early_polls_are_dropped_while_the_deco_lags():
    d = fixture("2026-11-05-limited.json")
    r = cu.analyze(d["runs"], d["polls"])
    assert 405 not in [p["raw"] for p in r["steady"]]


def test_back_to_back_runs_settle_from_the_first_start():
    d = fixture("2026-11-05-unlimited.json")
    r = cu.analyze(d["runs"], d["polls"])
    # 17:42:24 is 10 s into run 3 but 180 s into continuous traffic: kept.
    # 17:41:20 is 116 s in: still ramping, dropped.
    assert [p["raw"] for p in r["steady"]] == [217526]


def test_unthrottled_runs_never_yield_a_unit():
    # Their average doesn't match an instantaneous sample (the ratios came
    # out 2x apart before the settle time was raised).
    d = fixture("2026-11-05-unlimited.json")
    assert cu.analyze(d["runs"], d["polls"])["verdict"] not in cu.UNITS
    assert cu.verdict([45.6, 96.3])[0] == "INCONSISTENT"


def test_the_ramp_poll_is_dropped_and_the_shipped_run_pins_kbit_per_s():
    d = fixture("2026-11-05-ramp.json")
    r = cu.analyze(d["runs"], d["polls"])
    assert 23898 not in [p["raw"] for p in r["steady"]]  # +108 s, half the rate
    assert r["verdict"] == "kbit/s"


def test_no_steady_poll_is_an_error_not_a_verdict():
    d = fixture("2026-11-05-limited.json")
    short = [{"start": d["runs"][0]["start"], "end": d["runs"][0]["start"] + 90,
              "bytes_per_s": 5233183}]
    with pytest.raises(ValueError, match="no steady poll"):
        cu.analyze(short, d["polls"])


@pytest.mark.parametrize("ratio,unit", [(1.02, "bytes/s"), (1000, "kB/s"), (1024, "KiB/s"),
                                        (118, "kbit/s"), (125000, "Mbit/s")])
def test_each_candidate_unit_is_recognised(ratio, unit):
    assert cu.verdict([ratio, ratio])[0] == unit


def test_a_ratio_between_units_is_unknown():
    # 1000 and 1024 are 2.4% apart, so kB/s vs KiB/s needs a tight match;
    # 400 is near nothing.
    assert cu.verdict([400, 400])[0] == "UNKNOWN"


def test_polls_from_series_takes_the_value_scraped_after_each_poll():
    starts = {"data": {"result": [{"values": [[100, "1000.0"], [105, "1000.0"], [170, "1064.0"]]}]}}
    down = {"data": {"result": [{"values": [[995, "7"], [1006, "42"], [1070, "43"]]}]}}
    assert cu.polls_from(starts, down) == [{"start": 1000.0, "raw": 42.0}, {"start": 1064.0, "raw": 43.0}]
