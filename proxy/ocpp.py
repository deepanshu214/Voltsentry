"""OCPP 1.6-J frame handling — pure functions, no I/O.

Wire format (CONTEXT.md §5.A) is a JSON **array**, not an object:

    CALL       [2, "<uniqueId>", "<Action>", {payload}]
    CALLRESULT [3, "<uniqueId>", {payload}]
    CALLERROR  [4, "<uniqueId>", "<code>", "<desc>", {details}]

A CALLRESULT does not carry the action name; correlation back to the
originating action is the caller's job via a pending-call map (see state.py).

The MeterValues payload shape and the string-typed sampled values are pinned
in CONTEXT.md §6.2.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Any, Optional

CALL = 2
CALLRESULT = 3
CALLERROR = 4


@dataclass
class OCPPFrame:
    """A parsed OCPP frame. Fields not carried by a given message type are None."""

    message_type_id: int
    unique_id: str
    action: Optional[str] = None            # CALL only
    payload: Optional[dict] = None          # CALL / CALLRESULT
    error_code: Optional[str] = None        # CALLERROR
    error_description: Optional[str] = None  # CALLERROR
    error_details: Optional[dict] = None    # CALLERROR


def parse_frame(raw: str) -> OCPPFrame:
    """Parse a raw OCPP wire string into an OCPPFrame.

    Raises ValueError on anything that is not a well-formed OCPP array frame.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"frame is not valid JSON: {exc}") from exc

    if not isinstance(data, list) or len(data) < 2:
        raise ValueError("OCPP frame must be a JSON array of at least 2 elements")

    message_type_id = data[0]
    unique_id = data[1]
    if not isinstance(message_type_id, int):
        raise ValueError("messageTypeId must be an integer")
    if not isinstance(unique_id, str):
        raise ValueError("uniqueId must be a string")

    if message_type_id == CALL:
        if len(data) != 4 or not isinstance(data[2], str) or not isinstance(data[3], dict):
            raise ValueError("malformed CALL frame")
        return OCPPFrame(CALL, unique_id, action=data[2], payload=data[3])

    if message_type_id == CALLRESULT:
        if len(data) != 3 or not isinstance(data[2], dict):
            raise ValueError("malformed CALLRESULT frame")
        return OCPPFrame(CALLRESULT, unique_id, payload=data[2])

    if message_type_id == CALLERROR:
        if len(data) != 5 or not isinstance(data[2], str) or not isinstance(data[3], str):
            raise ValueError("malformed CALLERROR frame")
        details = data[4] if isinstance(data[4], dict) else {}
        return OCPPFrame(
            CALLERROR,
            unique_id,
            error_code=data[2],
            error_description=data[3],
            error_details=details,
        )

    raise ValueError(f"unknown messageTypeId: {message_type_id}")


def serialise_call(action: str, payload: dict, unique_id: Optional[str] = None) -> str:
    """Serialise an outgoing CALL frame [2, uniqueId, action, payload].

    Generates a uniqueId when one is not supplied.
    """
    if unique_id is None:
        unique_id = uuid.uuid4().hex
    return json.dumps([CALL, unique_id, action, payload])


def serialise_result(unique_id: str, payload: dict) -> str:
    """Serialise a CALLRESULT [3, uniqueId, payload]."""
    return json.dumps([CALLRESULT, unique_id, payload])


def _sampled_values(payload: dict) -> list[dict[str, Any]]:
    """Flatten the nested MeterValues sampledValue list (CONTEXT.md §6.2)."""
    samples: list[dict[str, Any]] = []
    for meter_value in payload.get("meterValue", []) or []:
        if isinstance(meter_value, dict):
            samples.extend(s for s in meter_value.get("sampledValue", []) or [] if isinstance(s, dict))
    return samples


# OCPP 1.6 SampledValue defaults (spec §7.43): measurand, location and unit.
_DEFAULT_MEASURAND = "Energy.Active.Import.Register"
_DEFAULT_LOCATION = "Outlet"

# Scale factor into the proxy's canonical unit per measurand family. Readings in
# a unit not listed here are dropped rather than guessed at.
_POWER_SCALE = {"kW": 1.0, "W": 0.001}
_ENERGY_SCALE = {"kWh": 1.0, "Wh": 0.001}

# Phase tags that mean phase-to-neutral; anything else on a voltage is line-to-line.
_PHASE_TO_NEUTRAL = {"L1-N", "L2-N", "L3-N", "L1", "L2", "L3", "N"}
_SQRT3 = math.sqrt(3.0)


@dataclass
class Sample:
    """One sampled value, already in the proxy's canonical unit."""

    measurand: str
    location: str
    phase: Optional[str]
    value: float


def _canonical(measurand: str, unit: Optional[str], raw: float) -> Optional[float]:
    if measurand.startswith("Power."):
        # The spec gives no default unit for power. Assuming W errs toward an
        # under-read, which R7 then flags, rather than a 1000x over-read that
        # would quarantine an honest station on R2.
        scale = _POWER_SCALE.get(unit or "W")
    elif measurand.startswith("Energy."):
        scale = _ENERGY_SCALE.get(unit or "Wh")
    elif measurand == "Voltage":
        scale = 1.0 if unit in (None, "V") else None
    elif measurand.startswith("Current."):
        scale = 1.0 if unit in (None, "A") else None
    elif measurand == "SoC":
        scale = 1.0 if unit in (None, "Percent") else None
    elif measurand == "Frequency":
        scale = 1.0  # OCPP 1.6 has no Hz unit; the value is Hz by definition
    else:
        return None
    return None if scale is None else raw * scale


def normalised_samples(payload: dict) -> list[Sample]:
    """Every readable sampled value, unit-normalised (kW, kWh, V, A, %, Hz).

    Skipped, never fatal: SignedData blobs, non-numeric strings and unknown
    units. One malformed reading must not tear down the charger's socket.
    """
    out: list[Sample] = []
    for s in _sampled_values(payload):
        if s.get("format") == "SignedData":
            continue
        try:
            raw = float(s.get("value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(raw):
            continue
        measurand = s.get("measurand") or _DEFAULT_MEASURAND
        value = _canonical(measurand, s.get("unit"), raw)
        if value is None:
            continue
        out.append(Sample(measurand, s.get("location") or _DEFAULT_LOCATION, s.get("phase"), value))
    return out


def _last(samples: list[Sample], measurand: str, locations: tuple[str, ...]) -> Optional[float]:
    found = [s.value for s in samples if s.measurand == measurand and s.location in locations]
    return found[-1] if found else None


def extract_meter_values(payload: dict) -> dict:
    """Extract {power_kw, energy_register_kwh, soc} from a MeterValues payload.

    Power and energy are the charger's output (Outlet, the OCPP default), in kW
    and kWh whatever unit the charger used on the wire. Missing measurands come
    back as 0.0.
    """
    samples = normalised_samples(payload)
    power = _last(samples, "Power.Active.Import", ("Outlet",))
    energy = _last(samples, "Energy.Active.Import.Register", ("Outlet",))
    soc = _last(samples, "SoC", ("EV", "Outlet", "Body", "Cable", "Inlet"))
    return {
        "power_kw": power if power is not None else 0.0,
        "energy_register_kwh": energy if energy is not None else 0.0,
        "soc": soc if soc is not None else 0.0,
    }


@dataclass
class Electrical:
    """Electrical context of one MeterValues; any field the charger omits is None."""

    dc_voltage_v: Optional[float] = None    # output voltage to the vehicle
    dc_current_a: Optional[float] = None    # output current to the vehicle
    ac_voltage_ll_v: Optional[float] = None  # supply voltage, line-to-line
    ac_power_kw: Optional[float] = None     # active power drawn from the supply
    frequency_hz: Optional[float] = None    # supply frequency


def extract_electrical(payload: dict) -> Electrical:
    """Voltage / current / frequency / supply-side power from a MeterValues.

    Supply-side voltage is reported per phase; line-to-line readings are
    averaged, and phase-to-neutral readings are scaled by sqrt(3) first, so the
    result is always a line-to-line figure (415 V class, not 230 V).
    """
    samples = normalised_samples(payload)

    ll: list[float] = []
    for s in samples:
        if s.measurand == "Voltage" and s.location == "Inlet":
            ll.append(s.value * _SQRT3 if (s.phase or "") in _PHASE_TO_NEUTRAL else s.value)

    return Electrical(
        dc_voltage_v=_last(samples, "Voltage", ("Outlet",)),
        dc_current_a=_last(samples, "Current.Import", ("Outlet",)),
        ac_voltage_ll_v=sum(ll) / len(ll) if ll else None,
        ac_power_kw=_last(samples, "Power.Active.Import", ("Inlet",)),
        frequency_hz=_last(samples, "Frequency", ("Inlet", "Outlet", "Body")),
    )
