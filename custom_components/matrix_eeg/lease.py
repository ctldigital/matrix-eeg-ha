"""Boost lease state machine. A manual setpoint change is never undone: fallback is only
written while the current setpoint is still the one this lease applied (or, while the boost
write is still in flight, the one it replaced)."""

TOL_C = 0.05  # HA reports floats
PENDING_S = 120  # a vendor entity may show a written setpoint only on its next poll


def _same(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) < TOL_C


class LeaseCore:
    """now/until are wall-clock epoch seconds (the lease is persisted across restarts).

    Writes returned by on_tick/on_start are the stored fallback_c, unclamped: the glue must
    clamp them to the max entity and the local limit before writing.
    """

    def __init__(self, stored: dict | None):
        # {applied, fallback_c, until, prev, observed, pending_until}; stores from before prev count as observed
        self._lease = {"observed": True, **stored} if stored else None

    def to_store(self) -> dict:
        return dict(self._lease or {})

    def _in_flight(self, current, now=None) -> bool:
        """The boost write has not shown yet and current is still the setpoint it replaced
        (now=None: without the PENDING_S window, for ownership)."""
        lease = self._lease
        return (
            not lease["observed"]
            and (now is None or now < lease.get("pending_until", 0))
            and _same(current, lease.get("prev"))
        )

    def _owned(self, current) -> bool:
        return _same(current, self._lease["applied"]) or self._in_flight(current)

    def boost_active(self, current_target, now) -> bool:
        """An unknown setpoint (entity lagging or briefly unavailable) is not a manual change: it
        counts as boosting until the lease ends."""
        if not self._lease or now >= self._lease["until"]:
            return False
        if current_target is None or _same(current_target, self._lease["applied"]):
            return True
        return self._in_flight(current_target, now)

    def on_cmd(self, cmd, current_target, now, max_c=None):
        """Returns (value to write or None, ack error or None)."""
        if cmd.target_c == cmd.fallback_c:  # end
            owned = self._lease is not None and self._owned(current_target)
            lease, self._lease = self._lease, None
            if lease and current_target is None:
                # ownership undecidable: keep it expired so the next on_tick decides
                self._lease = {**lease, "until": now}
            elif owned:
                return (cmd.fallback_c if max_c is None else min(cmd.fallback_c, max_c)), None
            return None, None
        applied = cmd.target_c if max_c is None else min(cmd.target_c, max_c)
        if applied <= cmd.fallback_c:  # an ok ack without a write would look like a manual change
            return None, f"Boost nicht möglich: Obergrenze {max_c:g} °C".replace(".", ",")
        until = now + cmd.lease_s
        if self._lease and _same(applied, self._lease["applied"]):
            self._lease.update(fallback_c=cmd.fallback_c, until=until)
            return None, None
        self._lease = {
            "applied": applied,
            "fallback_c": cmd.fallback_c,
            "until": until,
            "prev": current_target,
            "observed": False,
            "pending_until": now + PENDING_S,
        }
        return applied, None

    def on_tick(self, current_target, now):
        lease = self._lease
        if lease and not lease["observed"] and _same(current_target, lease["applied"]):
            lease["observed"] = True
        if not lease or now < lease["until"] or current_target is None:
            return None  # unknown setpoint: keep the lease until ownership can be decided
        owned = self._owned(current_target)
        self._lease = None
        return lease["fallback_c"] if owned else None

    on_start = on_tick
