from custom_components.matrix_eeg.contract import Cmd
from custom_components.matrix_eeg.lease import LeaseCore

INF = float("inf")
BOOST = Cmd(1, 53, 50, 300)
END = Cmd(2, 50, 50, 0)


def boosted(now=1000.0, max_c=INF):
    core = LeaseCore(None)
    assert core.on_cmd(BOOST, 50, now, max_c) == (min(53, max_c), None)
    return core


def test_boost_writes_and_renew_does_not():
    core = boosted()
    assert core.to_store() == {
        "applied": 53, "fallback_c": 50, "until": 1300.0, "prev": 50, "observed": False, "pending_until": 1120.0
    }
    assert core.on_cmd(Cmd(3, 53, 50, 300), 53, 1200.0, INF) == (None, None)
    assert core.to_store()["until"] == 1500.0


def test_end_restores_fallback_when_owned():
    core = boosted()
    assert core.on_cmd(END, 53, 1100.0, INF) == (50, None)
    assert core.to_store() == {}
    assert not core.boost_active(50, 1100.0)


def test_end_after_manual_change_writes_nothing():
    core = boosted()
    assert core.on_cmd(END, 45, 1100.0, INF) == (None, None)
    assert core.to_store() == {}


def test_end_without_lease_writes_nothing():
    assert LeaseCore(None).on_cmd(END, 53, 1100.0, INF) == (None, None)


def test_expiry_restores_fallback():
    core = boosted()
    assert core.on_tick(53, 1299.0) is None
    assert core.on_tick(53, 1300.0) == 50
    assert core.to_store() == {}


def test_expiry_after_manual_change_writes_nothing():
    core = boosted()
    assert core.on_tick(45, 1300.0) is None
    assert core.to_store() == {}


def test_restart_after_expiry_restores_fallback():
    stored = boosted().to_store()
    assert LeaseCore(stored).on_start(53, 5000.0) == 50


def test_restart_before_expiry_keeps_boost():
    stored = boosted().to_store()
    core = LeaseCore(stored)
    assert core.on_start(53, 1100.0) is None
    assert core.boost_active(53, 1100.0)


def test_clamped_to_max_entity():
    core = LeaseCore(None)
    assert core.on_cmd(BOOST, 50, 1000.0, 52) == (52, None)
    assert core.to_store()["applied"] == 52
    assert core.boost_active(52, 1000.0)


def test_clamp_at_or_below_fallback_acks_error():
    core = LeaseCore(None)
    assert core.on_cmd(BOOST, 50, 1000.0, 50) == (None, "Boost nicht möglich: Obergrenze 50 °C")
    assert core.on_cmd(Cmd(1, 53, 49.5, 300), 50, 1000.0, 49.5) == (None, "Boost nicht möglich: Obergrenze 49,5 °C")
    assert core.to_store() == {}
    assert not core.boost_active(50, 1100.0)


def test_boost_active_tolerance():
    core = boosted()
    assert core.boost_active(53.04, 1100.0)
    assert not core.boost_active(52.9, 1100.0)


def test_unknown_setpoint_counts_as_boost_until_lease_end():
    # a lagging or briefly unavailable entity must not look like a manual change
    core = boosted()
    assert core.boost_active(None, 1299.0)
    assert not core.boost_active(None, 1300.0)
    assert not LeaseCore(None).boost_active(None, 1000.0)
    core.on_cmd(END, None, 1100.0, INF)  # end with unknown setpoint: until = now
    assert not core.boost_active(None, 1100.0)


def test_value_in_flight_window():
    core = boosted()  # prev 50, pending until 1120
    assert core.boost_active(50, 1119.0)  # the vendor entity has not shown 53 yet
    assert not core.boost_active(50, 1120.0)  # never applied within the window
    assert not core.boost_active(45, 1010.0)  # manual change during the window


def test_landed_then_back_to_prev_is_manual():
    core = boosted()
    assert core.on_tick(53, 1010.0) is None  # observed
    assert not core.boost_active(50, 1020.0)
    assert core.on_cmd(END, 50, 1030.0, INF) == (None, None)  # not ours any more


def test_end_in_flight_writes_fallback():
    assert boosted().on_cmd(END, 50, 1010.0, INF) == (50, None)  # cancels a write that may still land


def test_expiry_in_flight_writes_fallback():
    assert boosted().on_tick(50, 1300.0) == 50
    assert boosted().on_tick(45, 1300.0) is None


def test_old_store_counts_as_observed():
    core = LeaseCore({"applied": 53, "fallback_c": 50, "until": 1300.0})
    assert core.boost_active(53, 1000.0)
    assert not core.boost_active(50, 1000.0)
    assert core.on_cmd(END, 50, 1000.0, INF) == (None, None)


def test_end_clamps_fallback_to_max_entity():
    core = boosted(max_c=52)
    assert core.on_cmd(Cmd(2, 58, 58, 0), 52, 1100.0, 52) == (52, None)


def test_renew_tolerates_float_jitter():
    core = boosted(max_c=52.0)
    assert core.on_cmd(BOOST, 45, 1100.0, 52.01) == (None, None)  # manual 45 is not overwritten
    assert core.to_store()["until"] == 1400.0


def test_end_with_unknown_target_keeps_lease_for_tick():
    core = boosted()
    assert core.on_cmd(END, None, 1100.0, INF) == (None, None)
    assert core.to_store()["until"] == 1100.0
    assert core.on_tick(None, 1101.0) is None
    assert core.to_store() != {}
    assert core.on_tick(53, 1102.0) == 50
    assert core.to_store() == {}


def test_end_with_unknown_target_and_manual_change_writes_nothing_later():
    core = boosted()
    core.on_cmd(END, None, 1100.0, INF)
    assert core.on_tick(45, 1101.0) is None
    assert core.to_store() == {}


def test_tick_and_start_with_unknown_target_keep_lease():
    core = boosted()
    assert core.on_tick(None, 5000.0) is None
    assert LeaseCore(core.to_store()).on_start(None, 5000.0) is None
    assert core.to_store() != {}


def test_restart_after_expiry_with_manual_change_writes_nothing():
    assert LeaseCore(boosted().to_store()).on_start(45, 5000.0) is None
