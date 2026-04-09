import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# --- Database connection ---
def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

# --- Get all projects ---
def get_all_projects():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("SELECT * FROM projects ORDER BY uploaded_at DESC")
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get all components for a project ---
def get_components_by_project(project_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM components
        WHERE project_id = %s
        ORDER BY category
    """, (project_id,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get components by category ---
def get_components_by_category(category):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM components
        WHERE category = %s
        ORDER BY quality_score DESC
    """, (category,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get high quality components ---
def get_high_quality_components(min_score=0.8):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM components
        WHERE quality_score >= %s
        ORDER BY quality_score DESC
    """, (min_score,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get components by dimensions ---
def get_walls_by_min_length(min_length_mm):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM components
        WHERE category = 'IfcWall'
        AND length_mm >= %s
        ORDER BY length_mm DESC
    """, (min_length_mm,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get exterior walls ---
def get_exterior_walls():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM components
        WHERE category = 'IfcWall'
        AND parameters->'Pset_WallCommon'->>'IsExternal' = 'true'
        ORDER BY length_mm DESC
    """)
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get spatial data for a component ---
def get_spatial_data(component_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM spatial_data
        WHERE component_id = %s
    """, (component_id,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return result

# --- Get all components with spatial data ---
def get_components_with_spatial():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT c.*, s.pos_x, s.pos_y, s.pos_z, 
               s.rot_x, s.rot_y, s.rot_z,
               s.bounding_box, s.level, s.elevation
        FROM components c
        JOIN spatial_data s ON s.component_id = c.id
        ORDER BY c.category
    """)
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get all materials for a project ---
def get_materials_by_project(project_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM materials
        WHERE project_id = %s
    """, (project_id,))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get wall types with layers ---
def get_wall_types():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT c.family_name, c.type_name, c.quality_score,
               w.total_thickness, w.function, w.layers
        FROM wall_types w
        JOIN components c ON c.id = w.component_id
        ORDER BY w.total_thickness DESC
    """)
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get MEP systems ---
def get_mep_systems():
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT c.family_name, c.category, c.quality_score,
               m.system_type, m.flow_rate, m.pressure_drop, m.connectors
        FROM mep_systems m
        JOIN components c ON c.id = m.component_id
        ORDER BY m.system_type
    """)
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Search components by name ---
def search_components(search_term):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM components
        WHERE family_name ILIKE %s
        OR type_name ILIKE %s
        ORDER BY quality_score DESC
    """, (f'%{search_term}%', f'%{search_term}%'))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    return results

# --- Get component by revit id ---
def get_component_by_revit_id(revit_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("""
        SELECT * FROM components
        WHERE revit_id = %s
    """, (revit_id,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return result
