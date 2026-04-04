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

# --- Get all components that need enrichment ---
def get_components(cursor):
    cursor.execute("""
        SELECT id, category, family_name, type_name, parameters
        FROM components
    """)
    return cursor.fetchall()

# --- Ask Claude to enrich a component ---
def enrich_component(component):
    id, category, family_name, type_name, parameters = component

    prompt = f"""You are a BIM data expert. Given the following building component data, 
return a JSON object with these fields:
- normalized_category: a clean simple category name (e.g. "wall", "door", "duct", "pipe", "slab", "roof", "furniture", "column", "beam", "stair", "window")
- description: a one sentence description of what this component is
- is_structural: true or false
- is_mep: true or false (mechanical, electrical, or plumbing)
- confidence: how confident you are in your answer from 0 to 1

Component data:
- IFC Category: {category}
- Family Name: {family_name}
- Type Name: {type_name}
- Parameters: {json.dumps(parameters, indent=2)}

Return only valid JSON, no explanation, no markdown."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        return json.loads(result)
    except Exception as e:
        print(f"  → Error: {e}")
        return None

# --- Save enriched data back to database ---
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

    components = get_components(cursor)
    print(f"Found {len(components)} components to enrich\n")

    for component in components:
        id = component[0]
        family_name = component[2] or "unnamed"
        
        print(f"Enriching: {family_name} (id={id})...")
        
        enriched = enrich_component(component)
        
        if enriched:
            save_enrichment(cursor, id, enriched)
            print(f"  → {enriched.get('normalized_category')} | {enriched.get('description')}")
        else:
            print(f"  → Failed to enrich")

    conn.commit()
    cursor.close()
    conn.close()
    print("\nDone!")

if __name__ == "__main__":
    run()