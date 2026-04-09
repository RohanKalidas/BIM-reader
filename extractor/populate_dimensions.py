import os
import json
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
 
load_dotenv()
 
def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )
 
def extract_from_parameters(parameters):
    """
    Pull dimensions directly from raw IFC parameter sets.
    Checks the most common Pset and Qto locations across IFC2x3 and IFC4.
    Returns a dict with whatever it finds — missing keys will be None.
    """
    dims = {}
 
    if not parameters:
        return dims
 
    # --- Height ---
    # strip.py writes wall height here directly from IfcExtrudedAreaSolid
    dims["height_mm"] = parameters.get("_height_mm")
 
    # Standard quantity sets
    for qto in ["Qto_WallBaseQuantities", "Qto_SlabBaseQuantities",
                 "Qto_RoofBaseQuantities", "Qto_ColumnBaseQuantities",
                 "Qto_BeamBaseQuantities", "Qto_DoorBaseQuantities",
                 "Qto_WindowBaseQuantities", "Qto_CoveringBaseQuantities"]:
        pset = parameters.get(qto, {})
        if not pset:
            continue
        dims["height_mm"]  = dims.get("height_mm")  or pset.get("Height") or pset.get("Depth")
        dims["length_mm"]  = dims.get("length_mm")  or pset.get("Length") or pset.get("Width")
        dims["width_mm"]   = dims.get("width_mm")   or pset.get("Width")  or pset.get("Thickness")
        dims["area_m2"]    = dims.get("area_m2")    or pset.get("NetSideArea") or pset.get("NetArea") or pset.get("NetFloorArea") or pset.get("GrossArea")
        dims["volume_m3"]  = dims.get("volume_m3")  or pset.get("NetVolume") or pset.get("GrossVolume")
 
    # BaseQuantities (IFC4 style — sometimes just called BaseQuantities)
    pset = parameters.get("BaseQuantities", {})
    if pset:
        dims["height_mm"] = dims.get("height_mm") or pset.get("Height") or pset.get("Depth")
        dims["length_mm"] = dims.get("length_mm") or pset.get("Length")
        dims["width_mm"]  = dims.get("width_mm")  or pset.get("Width") or pset.get("Thickness")
        dims["area_m2"]   = dims.get("area_m2")   or pset.get("NetArea") or pset.get("GrossArea")
        dims["volume_m3"] = dims.get("volume_m3") or pset.get("NetVolume") or pset.get("GrossVolume")
 
    # Pset_WallCommon thickness → width
    pset = parameters.get("Pset_WallCommon", {})
    if pset:
        dims["width_mm"] = dims.get("width_mm") or pset.get("Thickness")
 
    # Pset_DoorCommon / Pset_WindowCommon
    for key in ["Pset_DoorCommon", "Pset_WindowCommon"]:
        pset = parameters.get(key, {})
        if pset:
            dims["height_mm"] = dims.get("height_mm") or pset.get("Height")
            dims["width_mm"]  = dims.get("width_mm")  or pset.get("Width")
 
    # Revit-style parameters (often stored flat under "Identity Data" or similar)
    for key in ["Constraints", "Dimensions", "Identity Data"]:
        pset = parameters.get(key, {})
        if not pset:
            continue
        dims["height_mm"] = dims.get("height_mm") or pset.get("Height") or pset.get("Unconnected Height")
        dims["length_mm"] = dims.get("length_mm") or pset.get("Length")
        dims["width_mm"]  = dims.get("width_mm")  or pset.get("Width") or pset.get("Thickness")
        dims["area_m2"]   = dims.get("area_m2")   or pset.get("Area")
        dims["volume_m3"] = dims.get("volume_m3") or pset.get("Volume")
 
    # Convert anything that came back as a string
    for k, v in dims.items():
        if isinstance(v, str):
            try:
                dims[k] = float(v)
            except (ValueError, TypeError):
                dims[k] = None
 
    return dims
 
 
def populate_dimensions(project_id=None):
    conn = get_db()
    cursor = conn.cursor()
 
    if project_id:
        cursor.execute("""
            SELECT id, category, parameters
            FROM components
            WHERE project_id = %s
        """, (project_id,))
    else:
        cursor.execute("""
            SELECT id, category, parameters
            FROM components
        """)
 
    components = cursor.fetchall()
    print(f"Found {len(components)} components\n")
 
    updated = 0
 
    for component in components:
        comp_id, category, parameters = component
 
        # --- Layer 1: AI enrichment (most trusted if it ran) ---
        enrichment = (parameters or {}).get("ai_enrichment", {})
        ai_dims = enrichment.get("dimensions", {})
        ai_calc = enrichment.get("calculated_dimensions", {})
 
        width   = ai_dims.get("width_mm")   or ai_calc.get("width_mm")
        height  = ai_dims.get("height_mm")  or ai_calc.get("height_mm")
        length  = ai_dims.get("length_mm")  or ai_calc.get("length_mm")
        area    = ai_dims.get("area_m2")    or ai_calc.get("area_m2")
        volume  = ai_dims.get("volume_m3")  or ai_calc.get("volume_m3")
        depth   = ai_dims.get("depth_mm")   or ai_calc.get("depth_mm")
        quality = enrichment.get("quality_score")
 
        # Use depth as height for slabs if height still missing
        if not height and depth:
            height = depth
 
        # --- Layer 2: Raw IFC parameters (fills any gaps left by AI) ---
        raw = extract_from_parameters(parameters)
 
        width   = width   or raw.get("width_mm")
        height  = height  or raw.get("height_mm")
        length  = length  or raw.get("length_mm")
        area    = area    or raw.get("area_m2")
        volume  = volume  or raw.get("volume_m3")
 
        # --- Layer 3: Derive quality score from data completeness if AI didn't set one ---
        if quality is None:
            filled = sum(1 for v in [width, height, length, area, volume] if v is not None)
            quality = round(filled / 5, 2)
 
        cursor.execute("""
            UPDATE components
            SET
                width_mm      = %s,
                height_mm     = %s,
                length_mm     = %s,
                area_m2       = %s,
                volume_m3     = %s,
                quality_score = %s
            WHERE id = %s
        """, (width, height, length, area, volume, quality, comp_id))
 
        print(f"id={comp_id} [{category}] → "
              f"w={width} h={height} l={length} "
              f"area={area} vol={volume} q={quality}")
        updated += 1
 
    conn.commit()
    cursor.close()
    conn.close()
    print(f"\nUpdated {updated} components. Done!")
 
 
if __name__ == "__main__":
    import sys
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if pid:
        print(f"Running for project_id={pid}")
    else:
        print("Running for ALL projects")
    populate_dimensions(project_id=pid)
 
