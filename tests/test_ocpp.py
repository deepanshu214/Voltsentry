"""Tests for proxy.ocpp — OCPP 1.6-J frame parsing and MeterValues extraction.

Wire format and the MeterValues shape are pinned in CONTEXT.md §6.
Frames are JSON arrays, not objects. Sampled values are strings on the wire.
"""

import json

import pytest

from proxy.ocpp import extract_electrical, extract_meter_values, parse_frame, serialise_call


# A valid MeterValues CALL, shaped exactly like CONTEXT.md §6.2.
VALID_METERVALUES = json.dumps(
    [
        2,
        "a3f1",
        "MeterValues",
        {
            "connectorId": 1,
            "transactionId": 1041,
            "meterValue": [
                {
                    "timestamp": "2026-09-06T15:04:05Z",
                    "sampledValue": [
                        {"value": "120.4", "measurand": "Power.Active.Import", "unit": "kW"},
                        {"value": "18.72", "measurand": "Energy.Active.Import.Register", "unit": "kWh"},
                        {"value": "64.0", "measurand": "SoC", "unit": "Percent"},
                    ],
                }
            ],
        },
    ]
)


def test_parse_valid_metervalues_frame():
    frame = parse_frame(VALID_METERVALUES)

    assert frame.message_type_id == 2
    assert frame.unique_id == "a3f1"
    assert frame.action == "MeterValues"
    assert frame.payload["transactionId"] == 1041

    values = extract_meter_values(frame.payload)
    # Values are strings on the wire; the proxy casts them to float.
    assert values == {"power_kw": 120.4, "energy_register_kwh": 18.72, "soc": 64.0}


def test_parse_malformed_frame_raises():
    # Not a JSON array — an object. CONTEXT.md §5.A: the wire format is an array.
    with pytest.raises(ValueError):
        parse_frame('{"messageTypeId": 2, "action": "MeterValues"}')


def test_serialise_call_produces_ocpp_call_array():
    raw = serialise_call("ChangeAvailability", {"connectorId": 1, "type": "Inoperative"})
    decoded = json.loads(raw)

    assert decoded[0] == 2  # CALL
    assert isinstance(decoded[1], str) and decoded[1]  # non-empty uniqueId
    assert decoded[2] == "ChangeAvailability"
    assert decoded[3] == {"connectorId": 1, "type": "Inoperative"}


def _mv(*sampled):
    return {"connectorId": 1, "transactionId": 7, "meterValue": [{"timestamp": "t", "sampledValue": list(sampled)}]}


def test_power_in_watts_is_normalised_to_kw():
    # Real chargers commonly report W/Wh. Reading 120000 W as kW would trip R2.
    payload = _mv(
        {"value": "120000", "measurand": "Power.Active.Import", "unit": "W"},
        {"value": "18720", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
        {"value": "64", "measurand": "SoC", "unit": "Percent"},
    )
    assert extract_meter_values(payload) == {"power_kw": 120.0, "energy_register_kwh": 18.72, "soc": 64.0}


def test_ocpp_defaults_measurand_energy_and_unit_wh():
    # OCPP 1.6: measurand defaults to Energy.Active.Import.Register, unit to Wh.
    payload = _mv({"value": "5000"})
    assert extract_meter_values(payload)["energy_register_kwh"] == 5.0


def test_signed_and_garbage_values_are_skipped_not_fatal():
    payload = _mv(
        {"value": "AbC+/==", "format": "SignedData", "measurand": "Power.Active.Import", "unit": "kW"},
        {"value": "not-a-number", "measurand": "SoC", "unit": "Percent"},
        {"value": "50.5", "measurand": "Power.Active.Import", "unit": "kW"},
    )
    assert extract_meter_values(payload) == {"power_kw": 50.5, "energy_register_kwh": 0.0, "soc": 0.0}


def test_inlet_power_does_not_overwrite_outlet_power():
    # location defaults to Outlet; the AC Inlet reading is a different quantity.
    payload = _mv(
        {"value": "120.0", "measurand": "Power.Active.Import", "unit": "kW", "location": "Outlet"},
        {"value": "126.3", "measurand": "Power.Active.Import", "unit": "kW", "location": "Inlet"},
    )
    assert extract_meter_values(payload)["power_kw"] == 120.0
    assert extract_electrical(payload).ac_power_kw == 126.3


def test_extract_electrical_dc_and_ac_sides():
    payload = _mv(
        {"value": "712.4", "measurand": "Voltage", "unit": "V", "location": "Outlet"},
        {"value": "168.4", "measurand": "Current.Import", "unit": "A", "location": "Outlet"},
        {"value": "418.0", "measurand": "Voltage", "unit": "V", "location": "Inlet", "phase": "L1-L2"},
        {"value": "420.0", "measurand": "Voltage", "unit": "V", "location": "Inlet", "phase": "L2-L3"},
        {"value": "416.0", "measurand": "Voltage", "unit": "V", "location": "Inlet", "phase": "L3-L1"},
        {"value": "49.98", "measurand": "Frequency", "location": "Inlet"},
    )
    e = extract_electrical(payload)
    assert e.dc_voltage_v == 712.4
    assert e.dc_current_a == 168.4
    assert e.ac_voltage_ll_v == pytest.approx(418.0)  # mean of the three line-to-line readings
    assert e.frequency_hz == 49.98


def test_phase_to_neutral_voltage_is_converted_to_line_to_line():
    # 240 V L-N on a balanced three-phase supply is 240 * sqrt(3) = 415.7 V L-L.
    payload = _mv({"value": "240.0", "measurand": "Voltage", "unit": "V", "location": "Inlet", "phase": "L1-N"})
    assert extract_electrical(payload).ac_voltage_ll_v == pytest.approx(415.69, abs=0.01)


def test_extract_electrical_absent_measurands_are_none():
    e = extract_electrical(_mv({"value": "10", "measurand": "Power.Active.Import", "unit": "kW"}))
    assert e.dc_voltage_v is None and e.dc_current_a is None
    assert e.ac_voltage_ll_v is None and e.frequency_hz is None and e.ac_power_kw is None
