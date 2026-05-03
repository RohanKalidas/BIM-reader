"""
canonical_vocab.py — The controlled vocabulary used to classify library
components. The four taxonomies:

  CANONICAL_NAMES — what an object IS, by function (~300 names)
  STYLE_TAGS      — what aesthetic/style family it belongs to (~30 tags)
  CONTEXT_TAGS    — what building types it's appropriate for (~20 tags)
  QUALITY_CLASS   — a coarse signal for premium/standard/basic (3 values)

The classifier emits all four when ingesting a component. The matcher
filters candidates by these tags when fulfilling a fixture request.

Adding new entries to a taxonomy:
  - Add the string here.
  - Re-run the backfill if you want existing components re-tagged.
  - Update the classifier prompt if the new entry needs guidance.

Removing entries: don't, in general — the database has historical values.
"""

# ════════════════════════════════════════════════════════════════════════
# CANONICAL_NAMES — what each component IS
# Organized by IFC category for easy review. Order doesn't matter.
# ════════════════════════════════════════════════════════════════════════

CANONICAL_NAMES = {

    # ── Furniture: seating ──────────────────────────────────────────────
    "office_chair",              # task chair, desk chair, ergonomic
    "executive_chair",           # leather, high-back, weighty
    "conference_chair",          # mid-back, often non-rolling
    "lobby_lounge_chair",        # accent chair, club chair, reception seating
    "guest_chair",               # side chair, visitor chair
    "dining_chair",
    "bar_stool",                 # counter-height seating
    "armchair",                  # residential lounge chair
    "wing_chair",                # traditional residential
    "rocking_chair",
    "bench",                     # flat seating, no back
    "ottoman",                   # backless, often with storage
    "sofa_2_seat",
    "sofa_3_seat",
    "sofa_sectional",
    "loveseat",                  # 2-seat residential
    "chaise_lounge",             # asymmetric / one-armed
    "outdoor_chair",
    "stadium_seat",              # auditorium, theater, sports

    # ── Furniture: tables and desks ─────────────────────────────────────
    "office_desk",               # standard work desk
    "executive_desk",            # large, premium, often L-shape
    "workstation",               # systems furniture cubicle
    "conference_table",          # small (4-6 seat)
    "conference_table_large",    # boardroom (10+ seat)
    "dining_table",              # residential
    "dining_table_large",        # 8+ seat
    "bistro_table",              # small cafe / counter
    "coffee_table",
    "side_table",                # small accent table
    "console_table",             # narrow, against wall
    "nightstand",
    "dresser",                   # bedroom storage with drawers
    "vanity_table",
    "reception_desk",
    "service_counter",           # retail / front-of-house
    "lab_bench",                 # institutional / scientific
    "outdoor_table",
    "picnic_table",

    # ── Furniture: storage ──────────────────────────────────────────────
    "wardrobe",                  # tall residential closet furniture
    "armoire",                   # decorative wardrobe
    "filing_cabinet",
    "bookshelf",                 # tall open shelves
    "credenza",                  # low storage with surface
    "buffet_sideboard",          # dining storage
    "tv_unit",                   # media console
    "shelving_unit",
    "lockers",                   # gym / school / industrial
    "kitchen_cabinet_base",      # lower kitchen cabinet
    "kitchen_cabinet_upper",     # wall-mounted kitchen cabinet
    "pantry_cabinet",            # tall kitchen storage
    "vanity_cabinet",            # bathroom vanity with sink area
    "medicine_cabinet",
    "coat_rack",
    "umbrella_stand",

    # ── Furniture: beds ─────────────────────────────────────────────────
    "bed_twin",
    "bed_full",
    "bed_queen",
    "bed_king",
    "bed_bunk",
    "bed_crib",
    "bed_hospital",              # adjustable medical bed
    "bed_dorm",                  # institutional / residence hall
    "futon",
    "daybed",

    # ── Plumbing fixtures ───────────────────────────────────────────────
    "toilet_residential",
    "toilet_commercial",         # tank-less / flush-valve
    "toilet_ada",                # accessible / comfort-height
    "urinal",
    "bidet",
    "sink_lavatory",             # bathroom hand wash
    "sink_pedestal",             # bathroom standalone
    "sink_undermount",           # built into vanity
    "sink_kitchen",              # residential
    "sink_kitchen_double",
    "sink_utility",              # mop / laundry
    "sink_lab",                  # scientific
    "sink_scrub",                # medical handwash
    "sink_bar",                  # small prep
    "drinking_fountain",
    "bathtub",
    "bathtub_freestanding",      # premium / vintage
    "shower_stall",              # enclosed unit
    "shower_tray",               # base only
    "shower_tub_combo",
    "bathtub_jetted",            # whirlpool / spa
    "mop_basin",
    "floor_drain",

    # ── Appliances: kitchen ─────────────────────────────────────────────
    "range_residential",         # combined cooktop + oven
    "range_commercial",          # restaurant kitchen
    "cooktop",                   # cooktop only, no oven
    "wall_oven",                 # built-in oven
    "microwave",
    "microwave_built_in",
    "refrigerator_residential",
    "refrigerator_french_door",  # premium residential
    "refrigerator_commercial",   # walk-in or reach-in
    "freezer_chest",
    "freezer_upright",
    "dishwasher",
    "dishwasher_commercial",
    "range_hood",
    "garbage_disposal",
    "ice_maker",
    "wine_cooler",
    "coffee_maker_commercial",   # cafe / restaurant
    "vending_machine",
    "warming_drawer",

    # ── Appliances: laundry ─────────────────────────────────────────────
    "washing_machine",
    "dryer",
    "washer_dryer_combo",        # stacked or single unit
    "laundry_tub",

    # ── Appliances: HVAC equipment ──────────────────────────────────────
    "water_heater_tank",
    "water_heater_tankless",
    "boiler_residential",
    "furnace",
    "air_handler",               # AHU
    "rooftop_unit",              # RTU
    "condenser_unit",            # outdoor split-system
    "heat_pump_split_outdoor",
    "heat_pump_split_indoor",
    "mini_split_indoor",         # ductless head
    "fan_coil_unit",
    "ptac_unit",                 # packaged terminal AC, hotel-style
    "vrf_indoor",
    "vrf_outdoor",
    "exhaust_fan",
    "ceiling_fan",
    "dehumidifier",
    "humidifier",

    # ── Appliances: electrical ──────────────────────────────────────────
    "electrical_panel_residential",
    "electrical_panel_commercial",
    "transformer",
    "generator_emergency",
    "ev_charger",
    "battery_storage",

    # ── Doors ───────────────────────────────────────────────────────────
    "door_interior_residential",
    "door_exterior_residential",
    "door_french",               # double residential
    "door_sliding_glass",
    "door_pocket",               # slides into wall
    "door_barn",                 # exposed sliding hardware
    "door_bifold",               # closet
    "door_interior_commercial",  # standard office
    "door_storefront",           # commercial glass entry
    "door_revolving",
    "door_fire_rated",           # rated stair / corridor door
    "door_overhead",             # garage / loading dock roll-up
    "door_dock_loading",         # loading dock
    "door_security",             # detention / vault
    "door_double",               # any double-leaf
    "door_pivot",                # premium architectural
    "door_panic_exit",           # institutional emergency

    # ── Windows ─────────────────────────────────────────────────────────
    "window_casement",
    "window_awning",             # hinged top
    "window_double_hung",
    "window_single_hung",
    "window_sliding",
    "window_fixed",              # picture window, no operation
    "window_bay",
    "window_bow",
    "window_skylight",
    "window_clerestory",
    "window_storefront",         # commercial glazing
    "window_curtain_wall_panel", # full-height glazing unit
    "window_jalousie",           # louvered, tropical
    "window_round",              # circular accent

    # ── Lighting ────────────────────────────────────────────────────────
    "light_recessed_downlight",  # can light
    "light_pendant",
    "light_chandelier",
    "light_sconce_wall",
    "light_track",
    "light_troffer",             # commercial drop-ceiling
    "light_linear",              # commercial linear strip
    "light_emergency",
    "light_exit_sign",
    "light_exterior_wall_pack",
    "light_exterior_pole",       # parking lot
    "light_landscape",           # path / garden
    "light_under_cabinet",
    "light_chandelier_lobby",    # premium / large

    # ── Electrical terminals ────────────────────────────────────────────
    "outlet_standard",           # duplex
    "outlet_gfci",               # bathroom / kitchen
    "outlet_220v",               # range / dryer
    "outlet_floor",
    "outlet_data",               # network jack
    "switch_standard",
    "switch_dimmer",
    "switch_3way",
    "thermostat",
    "smoke_detector",
    "co_detector",

    # ── HVAC terminals ──────────────────────────────────────────────────
    "diffuser_supply_ceiling",
    "diffuser_linear_slot",
    "diffuser_floor",
    "register_return",
    "register_grille",
    "vav_box",                   # variable air volume
    "fan_powered_terminal",
    "register_exhaust",

    # ── Fire suppression ────────────────────────────────────────────────
    "sprinkler_pendant",         # ceiling-mount, hangs down
    "sprinkler_concealed",       # flush-mount
    "sprinkler_sidewall",
    "fire_extinguisher",
    "fire_alarm_pull",
    "fire_alarm_horn_strobe",

    # ── Plumbing pipes/valves (rare in libraries but possible) ───────────
    "pipe_supply",               # generic supply pipe
    "pipe_drain",                # DWV
    "pipe_vent",
    "valve_shutoff",
    "backflow_preventer",

    # ── Structural ──────────────────────────────────────────────────────
    "column_steel_w",            # W-section / I-beam column
    "column_steel_hss",          # square hollow
    "column_concrete",
    "column_masonry",
    "column_wood",
    "beam_steel_w",
    "beam_steel_hss",
    "beam_concrete",
    "beam_wood",                 # glulam / dimensional
    "joist_steel",
    "joist_wood",
    "truss_wood",
    "truss_steel",
    "footing_spread",
    "footing_strip",
    "pile_concrete",
    "deck_metal",                # composite floor deck
    "wall_shear",                # lateral system

    # ── Coverings (floor / wall / ceiling finishes) ─────────────────────
    "flooring_hardwood",
    "flooring_engineered_wood",
    "flooring_laminate",
    "flooring_tile_ceramic",
    "flooring_tile_porcelain",
    "flooring_tile_stone",
    "flooring_carpet_residential",
    "flooring_carpet_commercial",
    "flooring_carpet_tile",      # modular
    "flooring_vinyl_sheet",
    "flooring_vinyl_plank",
    "flooring_concrete_polished",
    "flooring_rubber",           # gym
    "flooring_terrazzo",
    "ceiling_acoustic_tile",     # ACT, drop ceiling
    "ceiling_gypsum",            # painted drywall
    "ceiling_wood_plank",
    "ceiling_metal_panel",
    "ceiling_exposed_structure",
    "wall_finish_paint",
    "wall_finish_wood_paneling",
    "wall_finish_tile",
    "wall_finish_brick",         # exposed interior
    "wall_finish_stone",
    "wall_finish_wallpaper",

    # ── Stairs / circulation ────────────────────────────────────────────
    "stair_straight",
    "stair_l_shape",
    "stair_u_shape",
    "stair_spiral",
    "stair_curved",              # premium architectural
    "stair_floating",            # cantilevered tread
    "railing_residential",
    "railing_commercial",        # code-compliant guardrail
    "railing_glass",
    "railing_cable",
    "elevator_passenger",
    "elevator_freight",
    "escalator",

    # ── Outdoor / site ──────────────────────────────────────────────────
    "deck",
    "patio_paver",
    "fence_residential",
    "gate",
    "planter",
    "trash_receptacle_outdoor",
    "bike_rack",
    "bench_park",
    "fountain",
    "pergola_outdoor",

    # ── Specialty / institutional ───────────────────────────────────────
    "exam_table_medical",
    "hospital_curtain_track",
    "patient_lift",
    "x_ray_unit",
    "lab_fume_hood",
    "lab_centrifuge",
    "altar",
    "pulpit",
    "pew_church",
    "court_bench",
    "jury_box",

    # ── Catch-all ───────────────────────────────────────────────────────
    "other",                     # genuinely doesn't fit anything above
}


# ════════════════════════════════════════════════════════════════════════
# STYLE_TAGS — aesthetic family the component belongs to
# A component can have multiple style tags. ~30 distinct tags.
# ════════════════════════════════════════════════════════════════════════

STYLE_TAGS = {
    # Periods (residential leaning)
    "victorian",
    "italianate",
    "georgian",
    "colonial",
    "federal",
    "art_deco",
    "art_nouveau",
    "craftsman",
    "tudor_revival",
    "spanish_revival",
    "mediterranean",

    # Modern era (residential + commercial)
    "modern",                    # general modernism
    "mid_century_modern",
    "international",             # Mies / Bauhaus
    "minimalist",
    "contemporary",              # current / today
    "industrial",                # exposed steel/brick aesthetic
    "scandinavian",
    "japandi",                   # japanese + scandinavian
    "japanese",
    "shou_sugi_ban",             # charred wood japanese

    # Commercial / institutional
    "class_a_glass",             # corporate glass tower
    "postmodern",
    "brutalist",
    "high_tech_expressionist",   # Foster / Rogers

    # Casual / vernacular residential
    "farmhouse",
    "rustic",
    "cottage",
    "coastal",
    "ranch",
    "cape_cod",

    # Luxury / traditional
    "transitional",              # mix of modern + traditional
    "luxury",                    # high-end generic
    "traditional",               # generic non-modern

    # Functional / no-style
    "utilitarian",               # warehouse, industrial, no aesthetic
    "institutional",             # generic school/hospital/civic
    "any",                       # genuinely style-neutral (e.g., generic toilet)
}


# ════════════════════════════════════════════════════════════════════════
# CONTEXT_TAGS — building types the component is appropriate for
# ════════════════════════════════════════════════════════════════════════

CONTEXT_TAGS = {
    "residential",               # single-family, multi-family
    "commercial",                # general commercial
    "office",                    # specifically office buildings
    "retail",                    # stores, big-box
    "hospitality",               # hotel, restaurant
    "restaurant",                # specifically restaurant kitchen / dining
    "hotel",                     # specifically hotel guest / lobby
    "healthcare",                # clinic, hospital
    "hospital",                  # specifically inpatient
    "education",                 # school, university
    "industrial",                # warehouse, manufacturing
    "civic",                     # government, library, museum
    "religious",                 # church, temple, mosque
    "sports",                    # gym, arena, stadium
    "outdoor",                   # site work, exterior
    "lab",                       # scientific / research
    "courthouse",                # specialized civic
    "any",                       # genuinely context-neutral
}


# ════════════════════════════════════════════════════════════════════════
# QUALITY_CLASS — coarse premium/standard/basic signal
# Used to break ties when multiple candidates match style+context.
# ════════════════════════════════════════════════════════════════════════

QUALITY_CLASS = {
    "premium",      # high-end, named brand, expensive material, special detail
    "standard",     # mid-market, typical
    "basic",        # economy, builder-grade, utility
}


# ════════════════════════════════════════════════════════════════════════
# Validation helpers
# ════════════════════════════════════════════════════════════════════════

def is_valid_canonical(name: str) -> bool:
    return name in CANONICAL_NAMES

def is_valid_style(tag: str) -> bool:
    return tag in STYLE_TAGS

def is_valid_context(tag: str) -> bool:
    return tag in CONTEXT_TAGS

def is_valid_quality(cls: str) -> bool:
    return cls in QUALITY_CLASS

def validate_classification(canonical_name: str,
                             style_tags: list,
                             context_tags: list,
                             quality_class: str) -> tuple[bool, str]:
    """Returns (ok, error_message). Error empty if ok."""
    if not is_valid_canonical(canonical_name):
        return False, f"Unknown canonical_name: {canonical_name!r}"
    bad_styles = [t for t in style_tags if not is_valid_style(t)]
    if bad_styles:
        return False, f"Unknown style_tags: {bad_styles}"
    bad_contexts = [t for t in context_tags if not is_valid_context(t)]
    if bad_contexts:
        return False, f"Unknown context_tags: {bad_contexts}"
    if not is_valid_quality(quality_class):
        return False, f"Unknown quality_class: {quality_class!r}"
    return True, ""


if __name__ == "__main__":
    print(f"CANONICAL_NAMES: {len(CANONICAL_NAMES)} entries")
    print(f"STYLE_TAGS:      {len(STYLE_TAGS)} entries")
    print(f"CONTEXT_TAGS:    {len(CONTEXT_TAGS)} entries")
    print(f"QUALITY_CLASS:   {len(QUALITY_CLASS)} entries")
