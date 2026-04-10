import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ── Key name maps ──────────────────────────────────────────────────────────────
# For each dimension, a ranked list of key names to look for across ANY pset.
# First match wins. All comparisons are case-insensitive.

HEIGHT_KEYS  = ['height', 'unconnected height', 'depth', 'thickness', 'sill height',
                'head height', 'default head height', 'overall height']
WIDTH_KEYS   = ['width', 'thickness', 'total thickness', 'default thickness',
                'rough width', 'overall width']
LENGTH_KEYS  = ['length', 'span', 'rough length', 'overall length']
AREA_KEYS    = ['netsidearea', 'grosssidearea', 'netfloorarea', 'grossfloorarea',
                'netarea', 'grossarea', 'grossceilingarea', 'totalarea',
                'projectedarea', 'area']
VOLUME_KEYS  = ['netvolume', 'grossvolume', 'volume']

# Psets that are metadata only — never contain real dimensions
SKIP_PSETS = {
    'phasing', 'graphics', 'pset_manufacturertypeinformation',
    'pset_membercommon', 'pset_roofcommon', 'other',
    'materials and finishes', 'analytical properties',
    'ai_enrichment', '_material', '_material_layers', '_material_constituents'
}

def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )


def safe_float(v):
    """Convert a value to float, return None if not possible."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def search_psets(parameters, key_names):
    """
    Search all psets in parameters for the first matching key (case-insensitive).
    Returns the float value or None.
    Skips internal keys (prefixed with _) and known metadata psets.
    """
    if not parameters:
        return None

    key_names_lower = [k.lower() for k in key_names]

    for pset_name, pset_data in parameters.items():
        if pset_name.startswith('_'):
            continue
        if pset_name.lower() in SKIP_PSETS:
            continue
        if not isinstance(pset_data, dict):
            continue

        for prop_key, prop_val in pset_data.items():
            if prop_key.lower() in key_names_lower:
                v = safe_float(prop_val)
                if v is not None:
                    return v

    return None


def extract_dims(category, parameters):
    """
    Extract dimensions from a component's parameters using fuzzy pset search.
    Returns dict with width_mm, height_mm, length_mm, area_m2, volume_m3, quality_score.
    """
    dims = {}

    # ── Layer 1: AI enrichment (most trusted if enricher ran) ──────────────────
    enrichment = parameters.get('ai_enrichment', {}) if parameters else {}
    ai_dims    = enrichment.get('dimensions', {})
    ai_calc    = enrichment.get('calculated_dimensions', {})

    dims['width_mm']  = safe_float(ai_dims.get('width_mm'))  or safe_float(ai_calc.get('width_mm'))
    dims['height_mm'] = safe_float(ai_dims.get('height_mm')) or safe_float(ai_calc.get('height_mm'))
    dims['length_mm'] = safe_float(ai_dims.get('length_mm')) or safe_float(ai_calc.get('length_mm'))
    dims['area_m2']   = safe_float(ai_dims.get('area_m2'))   or safe_float(ai_calc.get('area_m2'))
    dims['volume_m3'] = safe_float(ai_dims.get('volume_m3')) or safe_float(ai_calc.get('volume_m3'))
    depth             = safe_float(ai_dims.get('depth_mm'))  or safe_float(ai_calc.get('depth_mm'))
    if not dims['height_mm'] and depth:
        dims['height_mm'] = depth

    quality = enrichment.get('quality_score')

    # ── Layer 2: Strip.py internal keys ────────────────────────────────────────
    if not dims['height_mm'] and parameters:
        dims['height_mm'] = safe_float(parameters.get('_height_mm'))

    # ── Layer 3: Fuzzy search across all psets ─────────────────────────────────
    if not dims['height_mm']:
        dims['height_mm'] = search_psets(parameters, HEIGHT_KEYS)

    if not dims['width_mm']:
        dims['width_mm'] = search_psets(parameters, WIDTH_KEYS)

    if not dims['length_mm']:
        dims['length_mm'] = search_psets(parameters, LENGTH_KEYS)

    if not dims['area_m2']:
        dims['area_m2'] = search_psets(parameters, AREA_KEYS)

    if not dims['volume_m3']:
        raw_vol = search_psets(parameters, VOLUME_KEYS)
        if raw_vol is not None:
            # If volume > 10000 it's almost certainly stored in mm³ not m³
            dims['volume_m3'] = raw_vol / 1e9 if raw_vol > 10000 else raw_vol

    # ── Layer 4: Derive quality score from data completeness ───────────────────
    if quality is None:
        filled = sum(1 for k in ['width_mm', 'height_mm', 'length_mm', 'area_m2', 'volume_m3']
                     if dims.get(k) is not None)
        quality = round(filled / 5, 2)

    dims['quality_score'] = quality
    return dims


def populate_dimensions(project_id=None):
    conn   = get_db()
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

    for comp_id, category, parameters in components:
        dims = extract_dims(category, parameters or {})

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
        """, (
            dims.get('width_mm'),
            dims.get('height_mm'),
            dims.get('length_mm'),
            dims.get('area_m2'),
            dims.get('volume_m3'),
            dims.get('quality_score'),
            comp_id
        ))

        print(f"id={comp_id} [{category}] → "
              f"w={dims.get('width_mm')} "
              f"h={dims.get('height_mm')} "
              f"l={dims.get('length_mm')} "
              f"area={dims.get('area_m2')} "
              f"vol={dims.get('volume_m3')} "
              f"q={dims.get('quality_score')}")
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
