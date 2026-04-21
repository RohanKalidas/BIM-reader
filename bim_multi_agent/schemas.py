"""
schemas.py — Data contracts between the 4 agents.

The 4-agent pipeline is:

    user_prompt
         │
         ▼
    ┌────────────┐       Brief     brief: Brief
    │ BriefAgent │ ──────────────────────────────┐
    └────────────┘                               │
         │                                       │
         ▼                                       │
    ┌────────────┐       Layout   layout: Layout │
    │LayoutAgent │ ──────────────────────────────┼─┐
    └────────────┘                               │ │
         │                                       │ │
         ├──────────► ┌──────────────┐           │ │
         │            │ FacadeAgent  │ ──────────┤ │
         │            └──────────────┘  facade   │ │
         │                                       │ │
         └──────────► ┌──────────────┐           │ │
                      │   MEPAgent   │ ──────────┘ │
                      └──────────────┘  mep        │
                                                   │
                                                   ▼
                                         merge → building_spec
                                              → generate.py
                                              → IFC

The schemas here MUST match the shape that generate.py expects at the output.
See `merge_to_spec()` in orchestrator.py for the assembly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ── Agent 1: Brief ──────────────────────────────────────────────────────────
# The orchestrator reads the user prompt and produces a structured building
# brief. Every downstream agent reads this.

class Brief(BaseModel):
    """
    High-level building brief. Output of BriefAgent.

    This is the "design intent" every specialist reads. If a specialist needs
    to know WHY a design decision was made (e.g. "why put the entry here?"),
    the answer lives here in style_notes or constraints.
    """

    name: str = Field(..., description="Human-readable building name.")

    # Style + character
    architectural_style: str = Field(
        ...,
        description="Any style — 'victorian', 'japandi', 'brutalist', etc. Not restricted to a fixed list.",
    )
    style_notes: str = Field(
        ...,
        description="One paragraph of specific massing/aesthetic choices. 'Steep gable, turret SW, wraparound porch.'",
    )
    style_palette: Dict[str, str] = Field(
        default_factory=dict,
        description="Hex colors. Keys: ext_wall, trim, roof, accent (min). Also floor, ceiling, int_wall.",
    )

    # Program
    total_sqft: float = Field(..., description="Approximate total floor area in sqft.")
    floors_count: int = Field(..., ge=1, le=10, description="Number of above-grade floors.")
    program: List[str] = Field(
        ...,
        description="Required rooms in user-facing language. ['living', 'kitchen', '3 bedrooms', '2 bathrooms', 'utility'].",
    )

    # Site / orientation
    front_elevation: Literal["south", "north", "east", "west"] = Field(
        ...,
        description="Which side has the main entry. Public rooms should cluster here.",
    )
    location: Optional[str] = Field(None, description="City, region, or country if given.")
    climate_zone: Optional[str] = Field(
        None,
        description="ASHRAE climate zone if derivable from location. Affects MEP decisions.",
    )

    # Optional budget
    budget_usd: Optional[float] = Field(None, description="Total budget in USD if mentioned.")

    # Free-form constraints the user called out
    constraints: List[str] = Field(
        default_factory=list,
        description="User-specified constraints: 'open-plan kitchen', 'no basement', 'pet-friendly', etc.",
    )


# ── Agent 2: Layout ─────────────────────────────────────────────────────────
# Layout agent reads Brief, emits rooms with explicit coordinates.
# This shape matches what generate.py's plan_walls() expects today.

class Room(BaseModel):
    name: str
    x: float = Field(..., description="SW corner x-coordinate in meters, origin is building SW.")
    y: float = Field(..., description="SW corner y-coordinate in meters.")
    width: float = Field(..., gt=0, description="Dimension along +x.")
    depth: float = Field(..., gt=0, description="Dimension along +y.")
    exterior: bool = Field(
        True,
        description="Does this room touch an exterior wall? Interior rooms (hallways) set False.",
    )
    door_wall: Literal["north", "south", "east", "west"] = Field(
        ...,
        description="Which wall has the primary door. Used by generate.py for window/door placement.",
    )


class Floor(BaseModel):
    name: str = Field(..., description="Human-readable name. 'Ground', 'Upper', 'Basement'.")
    elevation: float = Field(..., description="Floor elevation in meters above grade.")
    height: float = Field(2.7, description="Floor-to-ceiling height in meters.")
    rooms: List[Room]


class Layout(BaseModel):
    """Output of LayoutAgent. Every floor + every room on it."""

    floors: List[Floor]
    footprint_width: float = Field(..., gt=0, description="Overall building width (+x).")
    footprint_depth: float = Field(..., gt=0, description="Overall building depth (+y).")
    rationale: str = Field(
        ...,
        description="One-paragraph explanation of layout choices. 'Public rooms on south, private to the north.'",
    )


# ── Agent 3: Facade ─────────────────────────────────────────────────────────
# Facade agent reads Brief + Layout, emits exterior features from the
# primitive library. Matches exterior_primitives.py vocabulary.

class ExteriorFeature(BaseModel):
    """
    One primitive from exterior_primitives.PRIMITIVES.

    The `type` field must be a registered primitive type. All other fields
    are passed through as params to the primitive's builder function, so the
    shape is intentionally loose here — the builder does its own validation.
    """

    type: str = Field(
        ...,
        description="Primitive type: turret, porch, gable, dormer, chimney, bay_window, canopy, column, portico, balcony, parapet, half_timber_band, shutter, pergola, vertical_fin, awning, wraparound_porch.",
    )

    class Config:
        extra = "allow"  # Let agents emit primitive-specific params


class Facade(BaseModel):
    """Output of FacadeAgent."""

    exterior_features: List[ExteriorFeature]
    style_palette: Dict[str, str] = Field(
        default_factory=dict,
        description="Facade agent may override/refine the brief's palette.",
    )
    rationale: str = Field(
        ...,
        description="Why these features were chosen. 'Turret + wraparound porch = Queen Anne Victorian.'",
    )


# ── Agent 4: MEP ────────────────────────────────────────────────────────────
# MEP agent reads Brief + Layout, emits HVAC/plumbing/electrical strategy.
# Most of this feeds into mep_systems.build_mep() as metadata overrides.

class MEPStrategy(BaseModel):
    """Output of MEPAgent."""

    hvac_type: Literal[
        "central_forced_air",
        "heat_pump_central",
        "heat_pump_ductless",
        "radiant_hydronic",
        "baseboard_electric",
        "none",
    ] = Field(..., description="Primary HVAC approach.")

    heating_fuel: Literal["electric", "gas", "heat_pump", "hybrid", "none"] = Field(...)
    cooling: Literal["central_ac", "heat_pump", "mini_split", "none"] = Field(...)

    hot_water: Literal["tank_electric", "tank_gas", "tankless_gas", "heat_pump", "solar"] = Field(...)

    ventilation: Literal["natural", "mechanical_exhaust", "hrv", "erv"] = Field(...)

    # Where the equipment goes
    equipment_location: Literal["basement", "garage", "utility_room", "mechanical_closet", "roof", "attic"] = Field(
        ...,
        description="Which room houses the main air handler / water heater / panel.",
    )

    # Rough counts — mep_systems.py will scale from these
    hvac_zones: int = Field(1, ge=1, le=8)
    electrical_panel_amps: int = Field(200, description="Main service size in amps.")

    # Fire suppression
    sprinklers: bool = Field(False, description="True for commercial or code-required residential.")
    smoke_detectors: bool = Field(True)

    rationale: str = Field(
        ...,
        description="Why these choices. 'Heat pump central because Chicago climate + modern build.'",
    )


# ── Final merged spec ──────────────────────────────────────────────────────
# What generate.py accepts today. Produced by merging all 4 agent outputs.

class BuildingSpec(BaseModel):
    """
    Final spec handed to generate.py. Shape is deliberately the same as the
    current single-model output so generate.py doesn't change at all.
    """

    name: str
    floors: List[Floor]
    metadata: Dict[str, Any]


# ── Lineage envelope ───────────────────────────────────────────────────────
# Useful for debugging + for the "edit" test case. Wraps every agent call
# with timing and the raw output.

class AgentRun(BaseModel):
    agent: str
    duration_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    raw_output: Any = None


class PipelineResult(BaseModel):
    """What generate_building_multi_agent() returns."""

    spec: BuildingSpec
    brief: Brief
    layout: Layout
    facade: Facade
    mep: MEPStrategy
    runs: List[AgentRun]
    total_duration_s: float
