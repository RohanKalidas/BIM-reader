"""
test_stub.py — Offline sanity check for the orchestrator and schemas.

Runs the pipeline with stubbed agent outputs (no API calls). This verifies:
 - Schemas validate correctly
 - merge_to_spec produces the expected shape
 - The edit path reuses cached outputs properly
 - The final spec has the fields generate.py expects

Run: python test_stub.py
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

# Stub the anthropic import before anything else loads
sys.modules.setdefault("anthropic", type(sys)("anthropic"))  # minimal stub

from .schemas import (  # noqa: E402
    AgentRun,
    Brief,
    Facade,
    ExteriorFeature,
    Floor,
    Layout,
    MEPStrategy,
    Room,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

SAMPLE_BRIEF = Brief(
    name="Victorian Cottage",
    architectural_style="queen anne victorian",
    style_notes=(
        "Queen Anne Victorian cottage with octagonal turret at SW corner, "
        "wraparound porch on south and west, steep gable over entry. "
        "Deep forest green clapboard with cream trim."
    ),
    style_palette={"ext_wall": "#2D4A3E", "trim": "#F0E8D6", "roof": "#3B2F2F", "accent": "#1F3529"},
    total_sqft=800,
    floors_count=1,
    program=["living room", "kitchen", "1 bedroom", "1 bathroom", "utility"],
    front_elevation="south",
    location="New England, USA",
    climate_zone="5A",
    budget_usd=None,
    constraints=[],
)


SAMPLE_LAYOUT = Layout(
    floors=[
        Floor(
            name="Ground",
            elevation=0,
            height=2.7,
            rooms=[
                Room(name="Living Room", x=0, y=0, width=5, depth=5, exterior=True, door_wall="north"),
                Room(name="Kitchen",     x=5, y=0, width=4, depth=5, exterior=True, door_wall="north"),
                Room(name="Hallway",     x=0, y=5, width=9, depth=1.2, exterior=False, door_wall="east"),
                Room(name="Bedroom",     x=0, y=6.2, width=5, depth=4, exterior=True, door_wall="south"),
                Room(name="Bathroom",    x=5, y=6.2, width=2.5, depth=4, exterior=True, door_wall="south"),
                Room(name="Utility",     x=7.5, y=6.2, width=1.5, depth=4, exterior=True, door_wall="south"),
            ],
        )
    ],
    footprint_width=9,
    footprint_depth=10.2,
    rationale=(
        "Single-story L-shape. Public rooms on south, private rooms to the north. "
        "Utility tucked against east wall for exterior venting."
    ),
)


SAMPLE_FACADE = Facade(
    exterior_features=[
        ExteriorFeature(type="turret", corner="sw", radius=1.3, cap="conical", spire=True),
        ExteriorFeature(type="porch", sides=["south", "west"], depth=1.8,
                        column_style="turned", column_count=5, has_roof=True),
        ExteriorFeature(type="gable", side="south", position=0.6, width=2.6, height=1.8),
        ExteriorFeature(type="dormer", side="south", position=0.85, width=1.4, height=1.3),
        ExteriorFeature(type="chimney", position=[6.5, 3.5], height=5.2, width=0.7, depth=0.7),
    ],
    style_palette={},
    rationale=(
        "Classic Queen Anne: corner turret, wraparound porch on front elevation, "
        "gable over entry, dormer for upstairs light."
    ),
)


SAMPLE_MEP = MEPStrategy(
    hvac_type="heat_pump_central",
    heating_fuel="heat_pump",
    cooling="heat_pump",
    hot_water="tank_electric",
    ventilation="mechanical_exhaust",
    equipment_location="utility_room",
    hvac_zones=1,
    electrical_panel_amps=200,
    sprinklers=False,
    smoke_detectors=True,
    rationale="Zone 5A + small footprint → heat pump central is the modern default.",
)


# ── Test 1: Schemas validate ────────────────────────────────────────────────

def test_schemas_validate():
    # Validate each fixture serializes + re-parses
    for name, obj in [("brief", SAMPLE_BRIEF), ("layout", SAMPLE_LAYOUT),
                      ("facade", SAMPLE_FACADE), ("mep", SAMPLE_MEP)]:
        j = obj.model_dump_json()
        assert j, f"{name} serialized to empty JSON"
        parsed = json.loads(j)
        assert isinstance(parsed, dict), f"{name} did not round-trip as dict"
    print("✓ schemas validate and round-trip")


# ── Test 2: merge_to_spec produces generate.py-compatible shape ─────────────

def test_merge_to_spec():
    from .orchestrator import merge_to_spec
    spec = merge_to_spec(SAMPLE_BRIEF, SAMPLE_LAYOUT, SAMPLE_FACADE, SAMPLE_MEP)

    # The spec must have these top-level keys (matching generate.py today)
    spec_dict = spec.model_dump()
    for key in ("name", "floors", "metadata"):
        assert key in spec_dict, f"spec missing top-level {key!r}"

    # Metadata must have these fields generate.py reads
    md = spec_dict["metadata"]
    for key in ("architectural_style", "front_elevation", "style_palette", "exterior_features"):
        assert key in md, f"metadata missing {key!r}"

    # Exterior features must match what architectural_exterior.py expects
    assert len(md["exterior_features"]) == 5
    for feat in md["exterior_features"]:
        assert "type" in feat, f"exterior feature missing 'type': {feat}"

    # Floors must be list of dicts with rooms
    assert isinstance(spec_dict["floors"], list)
    assert len(spec_dict["floors"]) == 1
    assert len(spec_dict["floors"][0]["rooms"]) == 6
    for room in spec_dict["floors"][0]["rooms"]:
        for key in ("name", "x", "y", "width", "depth", "exterior", "door_wall"):
            assert key in room, f"room missing {key!r}: {room}"

    print("✓ merge_to_spec produces generate.py-compatible shape")
    print(f"  - {len(md['exterior_features'])} exterior features")
    print(f"  - {len(spec_dict['floors'])} floor, {len(spec_dict['floors'][0]['rooms'])} rooms")
    print(f"  - palette keys: {sorted(md['style_palette'].keys())}")


# ── Test 3: Facade palette overrides brief palette ──────────────────────────

def test_facade_overrides_brief_palette():
    from .orchestrator import merge_to_spec

    facade_with_override = Facade(
        exterior_features=SAMPLE_FACADE.exterior_features,
        style_palette={"ext_wall": "#111111"},  # overrides brief's #2D4A3E
        rationale="test",
    )
    spec = merge_to_spec(SAMPLE_BRIEF, SAMPLE_LAYOUT, facade_with_override, SAMPLE_MEP)
    palette = spec.metadata["style_palette"]
    assert palette["ext_wall"] == "#111111", "facade override did not take precedence"
    assert palette["trim"] == "#F0E8D6", "non-overridden keys did not fall through"
    print("✓ facade palette overrides brief palette correctly")


# ── Test 4: Edit path reuses cached agent runs ──────────────────────────────

def test_edit_path_reuses_runs():
    from .orchestrator import edit_building, merge_to_spec
    from .schemas import PipelineResult

    base = PipelineResult(
        spec=merge_to_spec(SAMPLE_BRIEF, SAMPLE_LAYOUT, SAMPLE_FACADE, SAMPLE_MEP),
        brief=SAMPLE_BRIEF,
        layout=SAMPLE_LAYOUT,
        facade=SAMPLE_FACADE,
        mep=SAMPLE_MEP,
        runs=[
            AgentRun(agent="brief_agent", duration_s=1.0, input_tokens=100, output_tokens=200),
            AgentRun(agent="layout_agent", duration_s=2.0, input_tokens=300, output_tokens=400),
            AgentRun(agent="facade_agent", duration_s=1.5, input_tokens=200, output_tokens=300),
            AgentRun(agent="mep_agent", duration_s=1.2, input_tokens=150, output_tokens=250),
        ],
        total_duration_s=5.7,
    )

    # Patch the facade agent to return a mutated version + track call count
    new_facade = Facade(
        exterior_features=[
            ExteriorFeature(type="parapet", height=0.9),
            ExteriorFeature(type="canopy", side="south", width=3.5, projection=1.4),
        ],
        style_palette={},
        rationale="edit: converted to modern",
    )
    new_run = AgentRun(agent="facade_agent", duration_s=1.1, input_tokens=500, output_tokens=100)

    call_count = {"facade": 0, "mep": 0, "layout": 0, "brief": 0}
    def fake_facade(*args, **kwargs):
        call_count["facade"] += 1
        return new_facade, new_run
    def fake_mep(*args, **kwargs):
        call_count["mep"] += 1
        return SAMPLE_MEP, AgentRun(agent="mep_agent", duration_s=0, input_tokens=0, output_tokens=0)
    def fake_layout(*args, **kwargs):
        call_count["layout"] += 1
        return SAMPLE_LAYOUT, AgentRun(agent="layout_agent", duration_s=0, input_tokens=0, output_tokens=0)
    def fake_brief(*args, **kwargs):
        call_count["brief"] += 1
        return SAMPLE_BRIEF, AgentRun(agent="brief_agent", duration_s=0, input_tokens=0, output_tokens=0)

    with patch("orchestrator.run_facade_agent", side_effect=fake_facade), \
         patch("orchestrator.run_mep_agent", side_effect=fake_mep), \
         patch("orchestrator.run_layout_agent", side_effect=fake_layout), \
         patch("orchestrator.run_brief_agent", side_effect=fake_brief):

        # Edit just the facade
        result = edit_building(base, "Make it modern with a flat roof", target="facade")

    assert call_count["facade"] == 1, f"facade should be called once, got {call_count['facade']}"
    assert call_count["mep"] == 0, "mep should NOT be re-run on facade edit"
    assert call_count["layout"] == 0, "layout should NOT be re-run on facade edit"
    assert call_count["brief"] == 0, "brief should NOT be re-run on facade edit"
    assert result.facade is new_facade, "facade should be the fresh one"
    assert result.mep is SAMPLE_MEP, "mep should be cached from previous"
    assert result.layout is SAMPLE_LAYOUT, "layout should be cached from previous"
    print("✓ edit path reuses cached runs — facade edit only re-runs facade")

    # Try an MEP-only edit too
    call_count = {"facade": 0, "mep": 0, "layout": 0, "brief": 0}
    with patch("orchestrator.run_facade_agent", side_effect=fake_facade), \
         patch("orchestrator.run_mep_agent", side_effect=fake_mep), \
         patch("orchestrator.run_layout_agent", side_effect=fake_layout), \
         patch("orchestrator.run_brief_agent", side_effect=fake_brief):
        result = edit_building(base, "Switch to gas heating", target="mep")
    assert call_count == {"facade": 0, "mep": 1, "layout": 0, "brief": 0}, \
        f"unexpected calls on MEP edit: {call_count}"
    print("✓ MEP edit only re-runs MEP agent")

    # A layout edit should cascade to facade + MEP
    call_count = {"facade": 0, "mep": 0, "layout": 0, "brief": 0}
    with patch("orchestrator.run_facade_agent", side_effect=fake_facade), \
         patch("orchestrator.run_mep_agent", side_effect=fake_mep), \
         patch("orchestrator.run_layout_agent", side_effect=fake_layout), \
         patch("orchestrator.run_brief_agent", side_effect=fake_brief):
        result = edit_building(base, "Add a basement", target="layout")
    assert call_count == {"facade": 1, "mep": 1, "layout": 1, "brief": 0}, \
        f"layout edit should cascade to facade + MEP: {call_count}"
    print("✓ layout edit cascades to facade + MEP (but not brief)")


# ── Run all tests ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Running stub tests (no API calls)...\n")
    test_schemas_validate()
    test_merge_to_spec()
    test_facade_overrides_brief_palette()
    test_edit_path_reuses_runs()
    print("\nAll stub tests passed ✓")
