"""Tests for shared.electrical — the site supply-chain arithmetic."""

import math

import pytest

from shared import electrical as el


def test_415_line_to_line_is_240_phase_to_neutral():
    assert el.phase_to_neutral_v(415.0) == pytest.approx(239.6, abs=0.1)


def test_charger_draws_more_than_it_delivers():
    # 120 kW to the car at 95 % efficiency needs 126.3 kW from the bus (+ standby).
    assert el.ac_input_kw(120.0) == pytest.approx(120.0 / 0.95 + 0.15)
    assert el.ac_input_kw(0.0) == pytest.approx(0.15)
    assert el.ac_input_kw(-5.0) == pytest.approx(0.15)  # a spoofed negative never feeds back


def test_kva_is_kw_over_power_factor():
    assert el.apparent_kva(99.0, 0.99) == pytest.approx(100.0)


def test_line_current_matches_three_phase_formula():
    # 1000 kVA on a 415 V bus: 1000e3 / (sqrt(3) * 415) = 1391 A per phase.
    assert el.line_current_a(1000.0, 415.0) == pytest.approx(1391.2, abs=0.1)


def test_bus_voltage_sags_with_loading_but_stays_near_415():
    assert el.lt_bus_voltage(0.0) == pytest.approx(433.0)
    full = el.lt_bus_voltage(1.0)
    assert 415.0 < full < 433.0
    assert el.lt_bus_voltage(0.5) > full


def test_eight_120kw_bays_need_a_1250_kva_transformer():
    # 8 x 127.75 kVA = 1022 kVA: 1000 kVA would overload, 1250 is the next size.
    assert el.bay_peak_kva() == pytest.approx((120.0 / 0.95 + 0.15) / 0.99)
    sel = el.transformer_for_fleet(8)
    assert (sel.units, sel.unit_kva) == (1, 1250)
    assert el.SITE_TRANSFORMER_KVA == 1250.0


def test_a_500_kva_transformer_cannot_feed_eight_fast_chargers():
    assert 8 * el.bay_peak_kva() > 500.0


def test_large_fleets_parallel_the_biggest_unit():
    sel = el.transformer_for_fleet(48)
    assert sel.unit_kva == 2500
    assert sel.total_kva >= 48 * el.bay_peak_kva()
    assert sel.units == math.ceil(48 * el.bay_peak_kva() / 2500)


def test_grid_band_brackets_nominal():
    lo, hi = el.GRID_BAND_HZ
    assert lo < el.GRID_NOMINAL_HZ < hi
