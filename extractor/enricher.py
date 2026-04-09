import os
import json
import psycopg2
import psycopg2.extras
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# --- Database connection ---
def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

# --- Get all components ---
def get_components(cursor):
    cursor.execute("""
        SELECT id, category, family_name, type_name, parameters
        FROM components
    """)
    return cursor.fetchall()

# --- Get all existing normalized categories for duplicate checking ---
def get_existing_components(cursor):
    cursor.execute("""
        SELECT id, family_name, type_name,
               parameters->'ai_enrichment'->>'normalized_category' as normalized_category,
               parameters
        FROM components
        WHERE parameters->'ai_enrichment' IS NOT NULL
    """)
    return cursor.fetchall()

# --- Ask Claude to enrich a component ---
def enrich_component(component, existing_components):
    id, category, family_name, type_name, parameters = component

    existing_summary = []
    for ec in existing_components:
        if ec[0] != id:
            existing_summary.append({
                "id": ec[0],
                "family_name": ec[1],
                "type_name": ec[2],
                "normalized_category": ec[3]
            })

    # Determine component system type for better enrichment
    is_mep = category in [
        "IfcFlowSegment", "IfcFlowFitting", "IfcFlowTerminal",
        "IfcFlowController", "IfcFlowMovingDevice", "IfcFlowStorageDevice",
        "IfcDistributionFlowElement", "IfcEnergyConversionDevice",
        "IfcDuctSegment", "IfcDuctFitting", "IfcPipeSegment", "IfcPipeFitting",
        "IfcAirTerminal", "IfcValve", "IfcPump", "IfcFan",
        "IfcElectricAppliance", "IfcLightFixture", "IfcOutlet",
        "IfcElectricDistributionBoard"
    ]

    is_structural = category in [
        "IfcBeam", "IfcColumn", "IfcMember", "IfcPlate",
        "IfcFooting", "IfcPile", "IfcReinforcingBar"
    ]

    system_context = ""
    if is_mep:
        system_context = f"\nThis is an MEP component. Pay special attention to: system_name={parameters.get('_system_name')}, system_type={parameters.get('_system_type')}, connectors and flow direction."
    elif is_structural:
        system_context = "\nThis is a structural component. Pay special attention to: load bearing capacity, material strength, connection points."

    prompt = f"""You are a BIM data expert managing a building component library.
Analyze this component and return a JSON object with these fields:

- normalized_category: clean simple category (e.g. "wall", "door", "duct", "pipe", "slab", "roof", "furniture", "column", "beam", "stair", "window", "valve", "diffuser", "pump", "light_fixture", "outlet")
- description: one sentence description of what this component is
- is_structural: true or false
- is_mep: true or false
- mep_system_type: if is_mep is true, one of "hvac" | "plumbing" | "electrical" | "fire_protection" | null
- confidence: 0 to 1
- dimensions: object with any dimensions you can extract from parameters (width_mm, height_mm, length_mm, area_m2, volume_m3, depth_mm) — only include what you can find
- quality_score: 0 to 1 rating of how complete and useful this component's data is
- missing_data: array of important properties that are missing
- duplicate_of: id of duplicate component from the existing list below, or null if unique
- notes: any other useful observations about this component
{system_context}

Existing components in database for duplicate detection:
{json.dumps(existing_summary, indent=2)}

Component to analyze:
- ID: {id}
- IFC Category: {category}
- Family Name: {family_name}
- Type Name: {type_name}
- Parameters: {json.dumps(parameters, indent=2)}

Return only valid JSON, no explanation, no markdown."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        return json.loads(result)
    except Exception as e:
        print(f"  → Error: {e}")
        return None

# --- Calculate and estimate missing dimensions ---
def calculate_dimensions(component, all_components):
    id, category, family_name, type_name, parameters = component

    enrichment = parameters.get("ai_enrichment", {})
    dims = enrichment.get("dimensions", {})

    calculated = {}

    width = dims.get("width_mm")
    length = dims.get("length_mm")
    area = dims.get("area_m2")
    volume = dims.get("volume_m3")
    height = dims.get("height_mm")
    depth = dims.get("depth_mm")

    # --- Math based calculations (100% confidence) ---

    # Calculate height from area and length
    if not height and area and length and length > 0:
        calculated["height_mm"] = round((area * 1000000) / length, 2)
        calculated["height_source"] = "calculated from area/length"
        calculated["height_confidence"] = 1.0

    # Calculate depth from volume and area
    if not depth and volume and area and area > 0:
        calculated["depth_mm"] = round((volume / area) * 1000, 2)
        calculated["depth_source"] = "calculated from volume/area"
        calculated["depth_confidence"] = 1.0

    # Calculate area from width and length
    if not area and width and length:
        calculated["area_m2"] = round((width * length) / 1000000, 3)
        calculated["area_source"] = "calculated from width x length"
        calculated["area_confidence"] = 1.0

    # --- Context based estimates (lower confidence) ---

    # Estimate wall height from standard residential heights
    if not height and not calculated.get("height_mm"):
        if category == "IfcWall":
            storey = parameters.get("_storey", "")
            if "ground" in str(storey).lower() or "00" in str(storey):
                calculated["height_mm"] = 2700
                calculated["height_source"] = "estimated standard ground floor height"
                calculated["height_confidence"] = 0.7
            else:
                calculated["height_mm"] = 2700
                calculated["height_source"] = "estimated standard residential height"
                calculated["height_confidence"] = 0.6

    return calculated if calculated else None

# --- Save enriched data back to database ---
def save_enrichment(cursor, component_id, enriched):
    cursor.execute("""
        UPDATE components
        SET parameters = parameters || %s::jsonb
        WHERE id = %s
    """, (json.dumps({"ai_enrichment": enriched}), component_id))

# --- Print a nice summary of what the AI found ---
def print_summary(enriched):
    cat = enriched.get('normalized_category', '?')
    desc = enriched.get('description', '?')
    quality = enriched.get('quality_score', '?')
    missing = enriched.get('missing_data', [])
    duplicate = enriched.get('duplicate_of')
    dims = enriched.get('dimensions', {})
    mep_type = enriched.get('mep_system_type')

    print(f"  → [{cat}] {desc}")
    print(f"  → Quality score: {quality}")
    if mep_type:
        print(f"  → MEP system: {mep_type}")
    if dims:
        print(f"  → Dimensions: {dims}")
    if missing:
        print(f"  → Missing: {', '.join(missing)}")
    if duplicate:
        print(f"  → DUPLICATE of component id={duplicate}")

# --- Main ---
def run():
    conn = get_db()
    cursor = conn.cursor()

    components = get_components(cursor)
    print(f"Found {len(components)} components to enrich\n")

    for component in components:
        id = component[0]
        family_name = component[2] or "unnamed"

        print(f"Enriching: {family_name} (id={id})...")

        existing = get_existing_components(cursor)

        enriched = enrich_component(component, existing)

        if enriched:
            calculated = calculate_dimensions(component, components)
            if calculated:
                enriched["calculated_dimensions"] = calculated
                print(f"  → Calculated: {calculated}")

            save_enrichment(cursor, id, enriched)
            print_summary(enriched)
        else:
            print(f"  → Failed to enrich")

        print()

    conn.commit()
    cursor.close()
    conn.close()
    print("Done!")

if __name__ == "__main__":
    run()
