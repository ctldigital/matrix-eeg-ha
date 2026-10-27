"""MQTT contract v1 (see contract/v1/README.md): parse commands, build state and ack messages."""

import json
import math
from dataclasses import dataclass

from .const import MAX_TARGET_C

MAX_LEASE_S = 86400

UNAVAILABLE = object()  # configured but currently without a value: the field is omitted from the state


@dataclass(frozen=True)
class Cmd:
    id: int
    target_c: float
    fallback_c: float
    lease_s: int


@dataclass(frozen=True)
class Reject:
    id: int
    error: str


def _is_int(x) -> bool:
    return type(x) is int  # bool is an int subclass, reject it


def _is_num(x) -> bool:
    try:
        return type(x) in (int, float) and math.isfinite(x)
    except OverflowError:  # huge ints do not fit a float
        return False


def parse_cmd(raw: bytes) -> Cmd | Reject | None:
    """None = no usable id, so no ack. Validation order is part of the contract (README).
    Only the hard cap MAX_TARGET_C rejects; the local limit and the max entity clamp the write."""
    try:
        p = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(p, dict) or not _is_int(p.get("id")):
        return None
    id_ = p["id"]
    if not (_is_int(p.get("v")) and p["v"] == 1):
        return Reject(id_, f"Unbekannte Vertragsversion {json.dumps(p.get('v'))}")
    lease = p.get("lease_s")
    if not _is_int(lease) or lease < 0 or lease > MAX_LEASE_S:
        return Reject(id_, "Ungültige Lease")
    target, fallback = p.get("target_c"), p.get("fallback_c")
    if not _is_num(target) or not _is_num(fallback):
        return Reject(id_, "Ungültiger Befehl")
    if target > MAX_TARGET_C or fallback > MAX_TARGET_C:
        return Reject(id_, f"Sollwert über Obergrenze {MAX_TARGET_C} °C")
    if target < fallback:
        return Reject(id_, "Ungültiger Befehl")
    if target > fallback and lease == 0:
        return Reject(id_, "Ungültige Lease")
    return Cmd(id_, target, fallback, lease)


def _dump(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def build_state(dhw_temp_c, dhw_target_c, boost_active, power_w, heater_rod_active, dhw_max_c, ts) -> bytes:
    """Optional values: None = not configured (null), UNAVAILABLE = configured but no value (omitted)."""
    optional = {
        "dhw_temp_c": dhw_temp_c,
        "dhw_target_c": dhw_target_c,
        "power_w": power_w,
        "heater_rod_active": heater_rod_active,
        "dhw_max_c": dhw_max_c,
    }
    state = {"v": 1, "category": "heat_pump", "ts": ts, "boost_active": boost_active}
    state.update({k: v for k, v in optional.items() if v is not UNAVAILABLE})
    return _dump(state)


def build_ack(id: int, error: str | None) -> bytes:
    return _dump({"id": id, "ok": True} if error is None else {"id": id, "ok": False, "error": error})
