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

def populate_dimensions():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, parameters
        FROM components
        WHERE parameters->'ai_enrichment'->'dimensions' IS NOT NULL
    """)

    components = cursor.fetchall()
    print(f"Found {len(components)} components with dimensions\n")

    updated = 0

    for component in components:
        id, parameters = component
        
        dims = parameters.get("ai_enrichment", {}).get("dimensions", {})
        quality = parameters.get("ai_enrichment", {}).get("quality_score")

        width = dims.get("width_mm")
        height = dims.get("height_mm")
        length = dims.get("length_mm")
        area = dims.get("area_m2")
        volume = dims.get("volume_m3")

        cursor.execute("""
            UPDATE components
            SET 
                width_mm = %s,
                height_mm = %s,
                length_mm = %s,
                area_m2 = %s,
                volume_m3 = %s,
                quality_score = %s
            WHERE id = %s
        """, (width, height, length, area, volume, quality, id))

        print(f"id={id} → width={width} height={height} length={length} area={area} volume={volume} quality={quality}")
        updated += 1

    conn.commit()
    cursor.close()
    conn.close()
    print(f"\nUpdated {updated} components. Done!")

if __name__ == "__main__":
    populate_dimensions()