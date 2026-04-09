import os
import json
import psycopg2
import psycopg2.extras
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Categories we already know — no AI needed
SKIP_CATEGORIES = {
    "IfcWall", "IfcWallStandardCase", "IfcWallElementedCase",
    "IfcSlab", "IfcRoof", "IfcDoor", "IfcWindow",
    "IfcColumn", "IfcColumnStandardCase", "IfcBeam", "IfcBeamStandardCase",
    "IfcStair", "IfcStairFlight", "IfcRailing", "IfcPlate",
    "IfcMember", "IfcCurtainWall", "IfcCovering", "IfcFurnishingElement",
    "IfcFurniture", "IfcOpeningElement",
    "IfcDuctSegment", "IfcDuctFitting", "IfcPipeSegment", "IfcPipeFitting",
    "IfcAirTerminal", "IfcValve", "IfcPump", "IfcFan",
    "IfcFlowSegment", "IfcFlowFitting", "IfcFlowTerminal",
    "IfcElectricAppliance", "IfcLightFixture", "IfcOutlet",
    "IfcElectricDistributionBoard"
}

BATCH_SIZE = 15

def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

# --- Get only components that need enrichment ---
def get_unenriched_components(cursor):
    cursor.execute("""
        SELECT id, category, family_name, type_name, parameters
        FROM components
        WHERE parameters->'ai_enrichment' IS NULL
        AND category NOT IN %s
        ORDER BY id
    """, (tuple(SKIP_CATEGORIES),))
    return cursor.fetchall()

# --- Enrich a batch of components in one API call ---
def enrich_batch(batch):
    components_json = []
    for id, category, family_name, type_name, parameters in batch:
        components_json.append({
            "id": id,
            "category": category,
            "family_name": family_name,
            "type_name": type_name,
            "parameters": parameters
        })

    prompt = f"""You are a BIM data expert. Analyze these building components and return a JSON array.
Each item in the array must have:
- id: the component id (same as input)
- normalized_category: clean simple name (e.g. "wall", "pipe", "duct", "proxy", "equipment")
- description: one sentence describing what this component is
- is_structural: true or false
- is_mep: true or false
- mep_system_type: "hvac" | "plumbing" | "electrical" | "fire_protection" | null
- quality_score: 0 to 1 based on how complete the data is
- missing_data: array of missing important properties

Components:
{json.dumps(components_json, indent=2)}

Return only a valid JSON array, no explanation, no markdown."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        return json.loads(result)
    except Exception as e:
        print(f"  → Batch error: {e}")
        return []

# --- Save enrichment back to database ---
def save_enrichment(cursor, component_id, enriched):
    cursor.execute("""
        UPDATE components
        SET parameters = parameters || %s::jsonb
        WHERE id = %s
    """, (json.dumps({"ai_enrichment": enriched}), component_id))

# --- Main ---
def run():
    conn = get_db()
    cursor = conn.cursor()

    components = get_unenriched_components(cursor)

    if not components:
        print("Nothing to enrich — all components are either already enriched or have known categories.")
        return

    print(f"Found {len(components)} components needing enrichment\n")

    # Split into batches
    batches = [components[i:i+BATCH_SIZE] for i in range(0, len(components), BATCH_SIZE)]
    print(f"Processing {len(batches)} batch(es) of up to {BATCH_SIZE} components each\n")

    total_enriched = 0

    for i, batch in enumerate(batches):
        print(f"Batch {i+1}/{len(batches)} ({len(batch)} components)...")
        results = enrich_batch(batch)

        if not results:
            print(f"  → Batch failed, skipping")
            continue

        for result in results:
            component_id = result.get("id")
            if component_id:
                save_enrichment(cursor, component_id, result)
                print(f"  → id={component_id} [{result.get('normalized_category')}] {result.get('description', '')[:80]}")
                total_enriched += 1

        conn.commit()
        print()

    cursor.close()
    conn.close()
    print(f"Done! Enriched {total_enriched} components.")

if __name__ == "__main__":
    run()
