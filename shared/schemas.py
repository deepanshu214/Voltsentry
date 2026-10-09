"""VoltSentry shared data contract.

Frozen at hour 1 of the hackathon. Since then only *optional* fields have been
added, so every older producer and consumer (and the recorded replay fixture)
still validates. Never rename or remove a field.

Units are in the field name: _kw (real power), _kva (apparent power), _kwh,
_v (volts), _a (amperes), _hz. A transformer is rated in kVA; kVA = kW / PF.
"""

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class StationStatus(str, Enum):
    AVAILABLE = "Available"
    PREPARING = "Preparing"
    CHARGING = "Charging"
    FINISHING = "Finishing"
    QUARANTINED = "Quarantined"
    OFFLINE = "Offline"


class AttackType(str, Enum):
    METER_SPOOF = "meter_spoof"
    FLEET_OSCILLATE = "fleet_oscillate"
    SESSION_SHADOW = "session_shadow"
    SUBTLE_DRIFT = "subtle_drift"
    TAG_CLONE = "tag_clone"               # params: {"id_tag": "<card to clone>"}
    RESET = "reset"


# --------------------------------------------------------------------------
# Dashboard feed  --  ws://localhost:8100/feed
# Every frame on this socket is exactly one of the three models below,
# discriminated by the literal `event` field. The dashboard switches on it.
# --------------------------------------------------------------------------


class TelemetryEvent(BaseModel):
    event: Literal["telemetry"] = "telemetry"
    station_id: str                       # "CP-04" == OCPP chargePointId
    transaction_id: Optional[int] = None
    ts: float                             # proxy receive clock, unix epoch seconds
    power_kw: float
    soc: float = Field(ge=0.0, le=100.0)
    dp_dt: float                          # kW/s; 0.0 on the first sample of a session
    duration_sec: float
    energy_register_kwh: float            # meter's cumulative register, as reported
    energy_residual_kwh: float            # register - integral(reported power dt)
    status: StationStatus
    ml_score: float = Field(default=0.0, ge=0.0, le=1.0)
    # Electrical context, as reported by the charger's meter. None when the
    # charger does not report that measurand.
    dc_voltage_v: Optional[float] = None  # output voltage to the vehicle
    dc_current_a: Optional[float] = None  # output current to the vehicle
    ac_voltage_v: Optional[float] = None  # supply voltage at the inlet, line-to-line
    frequency_hz: Optional[float] = None  # supply frequency at the inlet


class ThreatEvent(BaseModel):
    event: Literal["threat"] = "threat"
    station_id: str
    ts: float
    tier: Literal[1, 2]
    rule_id: Optional[str] = None         # "R2_PHYSICS" etc, tier 1 only
    ml_score: Optional[float] = None      # tier 2 only
    severity: Literal["low", "medium", "high"]
    reason: str                           # human-readable, rendered on the badge
    action_taken: Literal["logged", "frame_dropped", "quarantined"]
    raw_frame: Optional[str] = None       # forensic copy of the offending frame
    node_id: Optional[str] = None         # which VoltSentry node raised it


class GridEvent(BaseModel):
    """The site transformer, once a second.

    total_load_kw is the real power the transformer supplies (charger AC
    input). transformer_capacity_kva is its rating, and headroom_pct is
    measured against that rating in kVA, not kW.
    """

    event: Literal["grid"] = "grid"
    ts: float
    total_load_kw: float
    transformer_capacity_kva: float = 1250.0
    headroom_pct: float
    active_stations: int
    node_id: Optional[str] = None
    site_name: Optional[str] = None
    output_kw: Optional[float] = None         # DC power delivered to vehicles
    total_load_kva: Optional[float] = None    # apparent power = total_load_kw / PF
    loading_pct: Optional[float] = None       # total_load_kva / rating
    power_factor: Optional[float] = None
    charger_efficiency: Optional[float] = None
    hv_supply_kv: Optional[float] = None      # transformer primary
    lt_nominal_v: Optional[float] = None      # transformer secondary, line-to-line
    lt_voltage_v: Optional[float] = None      # measured bus voltage, line-to-line
    lt_current_a: Optional[float] = None      # per-phase line current
    frequency_hz: Optional[float] = None      # measured supply frequency


# --------------------------------------------------------------------------
# Twin control channel  --  ws://localhost:9100/control
# The red-team CLI drives the twin through this. It does NOT touch the
# twin<->proxy socket; see CONTEXT.md [FIX-4].
# --------------------------------------------------------------------------


class AttackTrigger(BaseModel):
    attack_type: AttackType
    station_id: Optional[str] = None      # None => fleet-wide
    params: dict = Field(default_factory=dict)


class ControlAck(BaseModel):
    ok: bool
    detail: str = ""
