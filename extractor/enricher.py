"""
extractor/enricher.py — Optional AI enrichment for ambiguous components.

Uses centralized database module. Trims API payload to reduce token waste.
"""

import os
import json
import logging
import anthropic
from database.db import get_db_connection
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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

# Keys worth sending to the AI for context (skip internal/large data)
RELEVANT_PSET_PREFIXES = ("Pset_", "Qto_", "BaseQuantities")


def trim_parameters(parameters):
    """
    Trim a component's parameters dict to only include relevant property sets.
    This dramatically reduces token usage in API calls.
    """
    if not parameters:
        return {}

    trimmed = {}
    for pset_name, pset_data in parameters.items():
        # Skip internal keys
        if pset_name.startswith("_"):
            continue
        # Only include known-useful psets
        if any(pset_name.startswith(prefix) for prefix in RELEVANT_PSET_PREFIXES):
            if isinstance(pset_data, dict):
                # Only keep scalar values, skip huge nested objects
                trimmed[pset_name] = {k: v for k, v in pset_data.items()
                                       if isinstance(v, (str, int, float, bool))}
            continue
        # Include other psets but limit to a few key properties
        if isinstance(pset_data, dict) and len(pset_data) <= 10:
            trimmed[pset_name] = {k: v for k, v in pset_data.items()
                                   if isinstance(v, (str, int, float, bool))}

    return trimmed


def get_unenriched_components(cursor, project_id=None):
    if project_id:
        cursor.execute("""
            SELECT id, category, family_name, type_name, parameters
            FROM components
            WHERE project_id = %s
            AND parameters->'ai_enrichment' IS NULL
            AND category NOT IN %s
            ORDER BY id
        """, (project_id, tuple(SKIP_CATEGORIES)))
    else:
        cursor.execute("""
            SELECT id, category, family_name, type_name, parameters
            FROM components
            WHERE parameters->'ai_enrichment' IS NULL
            AND category NOT IN %s
            ORDER BY id
        """, (tuple(SKIP_CATEGORIES),))
    return cursor.fetchall()


def enrich_batch(batch):
    components_json = []
    for comp_id, category, family_name, type_name, parameters in batch:
        components_json.append({
            "id": comp_id,
            "category": category,
            "family_name": family_name,
            "type_name": type_name,
            "parameters": trim_parameters(parameters)
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
        logger.error("Batch enrichment failed: %s", e)
        return []


def save_enrichment(cursor, component_id, enriched):
    cursor.execute("""
        UPDATE components
        SET parameters = parameters || %s::jsonb
        WHERE id = %s
    """, (json.dumps({"ai_enrichment": enriched}), component_id))


def run(project_id=None):
    with get_db_connection() as (conn, cursor):
        if project_id:
            print(f"Enriching components for project_id={project_id}\n")
        else:
            print("Enriching components for ALL projects\n")

        components = get_unenriched_components(cursor, project_id)

        if not components:
            print("Nothing to enrich — all components are either already enriched or have known categories.")
            return

        print(f"Found {len(components)} components needing enrichment\n")

        batches = [components[i:i+BATCH_SIZE] for i in range(0, len(components), BATCH_SIZE)]
        print(f"Processing {len(batches)} batch(es) of up to {BATCH_SIZE} components each\n")

        total_enriched = 0

        for i, batch in enumerate(batches):
            print(f"Batch {i+1}/{len(batches)} ({len(batch)} components)...")
            results = enrich_batch(batch)

            if not results:
                print(f"  -> Batch failed, skipping")
                continue

            for result in results:
                component_id = result.get("id")
                if component_id:
                    save_enrichment(cursor, component_id, result)
                    print(f"  -> id={component_id} [{result.get('normalized_category')}] "
                          f"{result.get('description', '')[:80]}")
                    total_enriched += 1

            conn.commit()
            print()

        # Final commit handled by context manager

    print(f"Done! Enriched {total_enriched} components.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(project_id=pid)
