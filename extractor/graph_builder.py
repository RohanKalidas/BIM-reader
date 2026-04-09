import os
import json
import psycopg2
import psycopg2.extras
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

# --- Database connections ---
def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

def get_neo4j():
    return GraphDatabase.driver(
        os.getenv("NEO4J_URI"),
        auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
    )

# --- Clear existing graph for a project ---
def clear_project_graph(session, project_id):
    session.run("""
        MATCH (n {project_id: $project_id})
        DETACH DELETE n
    """, project_id=project_id)

# --- Create a component node in Neo4j ---
def create_component_node(session, component, spatial):
    id, project_id, category, family_name, type_name, revit_id, parameters, \
    width_mm, height_mm, length_mm, area_m2, volume_m3, quality_score, created_at = component

    enrichment = parameters.get("ai_enrichment", {}) if parameters else {}
    normalized_category = enrichment.get("normalized_category", category)
    is_mep = enrichment.get("is_mep", False)
    is_structural = enrichment.get("is_structural", False)
    mep_system_type = enrichment.get("mep_system_type", None)

    pos_x = spatial.get("pos_x") if spatial else None
    pos_y = spatial.get("pos_y") if spatial else None
    pos_z = spatial.get("pos_z") if spatial else None
    rot_z = spatial.get("rot_z") if spatial else None
    level = spatial.get("level") if spatial else None
    elevation = spatial.get("elevation") if spatial else None

    session.run("""
        MERGE (c:Component {component_id: $component_id})
        SET c.project_id = $project_id,
            c.revit_id = $revit_id,
            c.category = $category,
            c.normalized_category = $normalized_category,
            c.family_name = $family_name,
            c.type_name = $type_name,
            c.is_mep = $is_mep,
            c.is_structural = $is_structural,
            c.mep_system_type = $mep_system_type,
            c.pos_x = $pos_x,
            c.pos_y = $pos_y,
            c.pos_z = $pos_z,
            c.rot_z = $rot_z,
            c.level = $level,
            c.elevation = $elevation,
            c.width_mm = $width_mm,
            c.height_mm = $height_mm,
            c.length_mm = $length_mm,
            c.area_m2 = $area_m2,
            c.volume_m3 = $volume_m3,
            c.quality_score = $quality_score
    """, 
        component_id=id,
        project_id=project_id,
        revit_id=revit_id,
        category=category,
        normalized_category=normalized_category,
        family_name=family_name or "",
        type_name=type_name or "",
        is_mep=is_mep,
        is_structural=is_structural,
        mep_system_type=mep_system_type,
        pos_x=pos_x,
        pos_y=pos_y,
        pos_z=pos_z,
        rot_z=rot_z,
        level=level,
        elevation=elevation,
        width_mm=width_mm,
        height_mm=height_mm,
        length_mm=length_mm,
        area_m2=area_m2,
        volume_m3=volume_m3,
        quality_score=quality_score
    )

# --- Create floor node ---
def create_floor_node(session, project_id, level_name, elevation):
    if not level_name:
        return
    session.run("""
        MERGE (f:Floor {project_id: $project_id, name: $name})
        SET f.elevation = $elevation
    """, project_id=project_id, name=level_name, elevation=elevation)

# --- Link component to its floor ---
def link_component_to_floor(session, component_id, project_id, level_name):
    if not level_name:
        return
    session.run("""
        MATCH (c:Component {component_id: $component_id})
        MATCH (f:Floor {project_id: $project_id, name: $level_name})
        MERGE (c)-[:ON_FLOOR]->(f)
    """, component_id=component_id, project_id=project_id, level_name=level_name)

# --- Create building node and link floors ---
def create_building_node(session, project_id, project_name):
    session.run("""
        MERGE (b:Building {project_id: $project_id})
        SET b.name = $name
    """, project_id=project_id, name=project_name)

    session.run("""
        MATCH (b:Building {project_id: $project_id})
        MATCH (f:Floor {project_id: $project_id})
        MERGE (f)-[:PART_OF]->(b)
    """, project_id=project_id)

# --- Get all components with spatial data from PostgreSQL ---
def get_components_with_spatial(cursor, project_id):
    cursor.execute("""
        SELECT c.id, c.project_id, c.category, c.family_name, c.type_name,
               c.revit_id, c.parameters, c.width_mm, c.height_mm, c.length_mm,
               c.area_m2, c.volume_m3, c.quality_score, c.created_at,
               s.pos_x, s.pos_y, s.pos_z, s.rot_x, s.rot_y, s.rot_z,
               s.bounding_box, s.level, s.elevation
        FROM components c
        LEFT JOIN spatial_data s ON s.component_id = c.id
        WHERE c.project_id = %s
    """, (project_id,))
    return cursor.fetchall()

# --- Get all projects ---
def get_projects(cursor):
    cursor.execute("SELECT id, name FROM projects WHERE status = 'done'")
    return cursor.fetchall()

# --- Main ---
def build_graph(project_id=None):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    driver = get_neo4j()

    projects = get_projects(cursor)

    if project_id:
        projects = [p for p in projects if p["id"] == project_id]

    print(f"Building graph for {len(projects)} project(s)\n")

    with driver.session() as session:
        for project in projects:
            pid = project["id"]
            pname = project["name"]

            print(f"Processing project: {pname} (id={pid})")

            # Clear existing graph data for this project
            clear_project_graph(session, pid)

            # Get all components with spatial data
            rows = get_components_with_spatial(cursor, pid)
            print(f"  Found {len(rows)} components")

            # Create component nodes
            for row in rows:
                component = (
                    row["id"], row["project_id"], row["category"],
                    row["family_name"], row["type_name"], row["revit_id"],
                    row["parameters"], row["width_mm"], row["height_mm"],
                    row["length_mm"], row["area_m2"], row["volume_m3"],
                    row["quality_score"], row["created_at"]
                )
                spatial = {
                    "pos_x": row["pos_x"],
                    "pos_y": row["pos_y"],
                    "pos_z": row["pos_z"],
                    "rot_x": row["rot_x"],
                    "rot_y": row["rot_y"],
                    "rot_z": row["rot_z"],
                    "bounding_box": row["bounding_box"],
                    "level": row["level"],
                    "elevation": row["elevation"]
                }

                create_component_node(session, component, spatial)

                # Create floor node and link
                if row["level"]:
                    create_floor_node(session, pid, row["level"], row["elevation"])
                    link_component_to_floor(session, row["id"], pid, row["level"])

            # Create building node
            create_building_node(session, pid, pname)

            print(f"  ✓ Graph nodes created")

    cursor.close()
    conn.close()
    driver.close()
    print("\nGraph build complete. Run spatial_analyzer.py to add relationships.")

if __name__ == "__main__":
    build_graph()
