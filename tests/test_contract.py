import json
from pathlib import Path

import pytest

from custom_components.matrix_eeg.contract import (
    UNAVAILABLE,
    Cmd,
    Reject,
    build_ack,
    build_state,
    parse_cmd,
)

FIX = Path(__file__).parent.parent / "contract" / "v1"


def load(name):
    return json.loads((FIX / name).read_text())


def raw(name):
    return (FIX / name).read_bytes()


@pytest.mark.parametrize("case", load("cmd_invalid.json"), ids=lambda c: c["name"])
def test_invalid_cases(case):
    res = parse_cmd(json.dumps(case["payload"]).encode())
    if case["error"] is None:
        assert res is None
    else:
        assert res == Reject(case["payload"]["id"], case["error"])


def test_boost_and_end_parse():
    assert parse_cmd(raw("cmd_boost.json")) == Cmd(7, 53, 50, 300)
    assert parse_cmd(raw("cmd_end.json")) == Cmd(8, 50, 50, 0)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"\xff\xfe",
        b"[1]",
        b'{"v":1,"id":true,"target_c":53,"fallback_c":50,"lease_s":300}',
        b'{"v":1,"id":"7","target_c":53,"fallback_c":50,"lease_s":300}',
    ],
)
def test_no_id_gives_none(payload):
    assert parse_cmd(payload) is None


@pytest.mark.parametrize(
    "fields,error",
    [
        ('"v":true,"id":1,"target_c":53,"fallback_c":50,"lease_s":300', "Unbekannte Vertragsversion true"),
        ('"v":1,"id":1,"target_c":53,"fallback_c":50,"lease_s":true', "Ungültige Lease"),
        ('"v":1,"id":1,"target_c":53,"fallback_c":50,"lease_s":3.5', "Ungültige Lease"),
        ('"v":1,"id":1,"target_c":true,"fallback_c":50,"lease_s":300', "Ungültiger Befehl"),
        ('"v":1,"id":1,"target_c":53,"lease_s":300', "Ungültiger Befehl"),
        ('"v":1,"id":1,"target_c":53,"fallback_c":"50","lease_s":300', "Ungültiger Befehl"),
        ('"v":1,"id":1,"target_c":NaN,"fallback_c":50,"lease_s":300', "Ungültiger Befehl"),
    ],
)
def test_malformed_fields(fields, error):
    assert parse_cmd(("{" + fields + "}").encode()) == Reject(1, error)


def test_only_hard_cap_rejects():
    # the local limit clamps in the bridge, parse_cmd knows only 60
    assert parse_cmd(b'{"v":1,"id":5,"target_c":60,"fallback_c":60,"lease_s":0}') == Cmd(5, 60, 60, 0)
    assert parse_cmd(b'{"v":1,"id":5,"target_c":60.5,"fallback_c":50,"lease_s":300}') == Reject(
        5, "Sollwert über Obergrenze 60 °C"
    )


def test_state_matches_fixtures():
    ts = "2026-10-27T10:00:00Z"
    full = build_state(48.5, 50, False, None, None, None, ts)
    assert json.loads(full) == load("state_heat_pump.json")
    unavailable = build_state(UNAVAILABLE, UNAVAILABLE, False, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, ts)
    assert json.loads(unavailable) == load("state_heat_pump_minimal.json")


def test_ack_matches_fixtures():
    assert json.loads(build_ack(7, None)) == load("ack_ok.json")
    assert json.loads(build_ack(7, "Sollwert über Obergrenze 60 °C")) == load("ack_error.json")


def test_overflow_and_lease_bound():
    huge = "1" + "0" * 400
    assert parse_cmd(f'{{"v":1,"id":1,"target_c":{huge},"fallback_c":50,"lease_s":300}}'.encode()) == Reject(
        1, "Ungültiger Befehl"
    )
    assert parse_cmd(f'{{"v":1,"id":1,"target_c":53,"fallback_c":50,"lease_s":{huge}}}'.encode()) == Reject(
        1, "Ungültige Lease"
    )
    ok = b'{"v":1,"id":1,"target_c":53,"fallback_c":50,"lease_s":86400}'
    assert parse_cmd(ok) == Cmd(1, 53, 50, 86400)
    assert parse_cmd(ok.replace(b"86400", b"86401")) == Reject(1, "Ungültige Lease")
