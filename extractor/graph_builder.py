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

# --- Create a component node ---
def create_component_node(session, row):
    enrichment = row["parameters"].get("ai_enrichment", {}) if row["parameters"] else {}
    normalized_category = enrichment.get("normalized_category", row["category"])
    is_mep = enrichment.get("is_mep", False)
    is_structural = enrichment.get("is_structural", False)
    mep_system_type = enrichment.get("mep_system_type", None)

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
            c.rot_x = $rot_x,
            c.rot_y = $rot_y,
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
        component_id=row["id"],
        project_id=row["project_id"],
        revit_id=row["revit_id"],
        category=row["category"],
        normalized_category=normalized_category,
        family_name=row["family_name"] or "",
        type_name=row["type_name"] or "",
        is_mep=is_mep,
        is_structural=is_structural,
        mep_system_type=mep_system_type,
        pos_x=row["pos_x"],
        pos_y=row["pos_y"],
        pos_z=row["pos_z"],
        rot_x=row["rot_x"],
        rot_y=row["rot_y"],
        rot_z=row["rot_z"],
        level=row["level"],
        elevation=row["elevation"],
        width_mm=row["width_mm"],
        height_mm=row["height_mm"],
        length_mm=row["length_mm"],
        area_m2=row["area_m2"],
        volume_m3=row["volume_m3"],
        quality_score=row["quality_score"]
    )

# --- Create a space node ---
def create_space_node(session, row):
    session.run("""
        MERGE (s:Space {revit_id: $revit_id, project_id: $project_id})
        SET s.name = $name,
            s.long_name = $long_name,
            s.level = $level,
            s.elevation = $elevation,
            s.area_m2 = $area_m2,
            s.volume_m3 = $volume_m3
    """,
        revit_id=row["revit_id"],
        project_id=row["project_id"],
        name=row["name"] or "",
        long_name=row["long_name"] or "",
        level=row["level"],
        elevation=row["elevation"],
        area_m2=row["area_m2"],
        volume_m3=row["volume_m3"]
    )

# --- Create a floor node ---
def create_floor_node(session, project_id, level_name, elevation):
    if not level_name:
        return
    session.run("""
        MERGE (f:Floor {project_id: $project_id, name: $name})
        SET f.elevation = $elevation
    """, project_id=project_id, name=level_name, elevation=elevation)

# --- Create a building node ---
def create_building_node(session, project_id, project_name):
    session.run("""
        MERGE (b:Building {project_id: $project_id})
        SET b.name = $name
    """, project_id=project_id, name=project_name)

# --- Link component to floor ---
def link_component_to_floor(session, component_id, project_id, level_name):
    if not level_name:
        return
    session.run("""
        MATCH (c:Component {component_id: $component_id})
        MATCH (f:Floor {project_id: $project_id, name: $level_name})
        MERGE (c)-[:ON_FLOOR]->(f)
    """, component_id=component_id, project_id=project_id, level_name=level_name)

# --- Link floor to building ---
def link_floors_to_building(session, project_id):
    session.run("""
        MATCH (b:Building {project_id: $project_id})
        MATCH (f:Floor {project_id: $project_id})
        MERGE (f)-[:PART_OF]->(b)
    """, project_id=project_id)

# --- Link space to floor ---
def link_space_to_floor(session, project_id, space_revit_id, level_name):
    if not level_name:
        return
    session.run("""
        MATCH (s:Space {revit_id: $revit_id, project_id: $project_id})
        MATCH (f:Floor {project_id: $project_id, name: $level_name})
        MERGE (s)-[:ON_FLOOR]->(f)
    """, revit_id=space_revit_id, project_id=project_id, level_name=level_name)

# --- Create relationship edge in Neo4j from PostgreSQL relationships table ---
def create_relationship_edge(session, rel):
    rel_type = rel["relationship_type"]
    id_a = rel["component_a_id"]
    id_b = rel["component_b_id"]
    props = rel["properties"] or {}

    if rel_type == "CONNECTS_TO":
        session.run("""
            MATCH (a:Component {component_id: $id_a})
            MATCH (b:Component {component_id: $id_b})
            MERGE (a)-[r:CONNECTS_TO]->(b)
            SET r.source = 'explicit'
        """, id_a=id_a, id_b=id_b)

    elif rel_type == "FILLS":
        session.run("""
            MATCH (a:Component {component_id: $id_a})
            MATCH (b:Component {component_id: $id_b})
            MERGE (a)-[r:FILLS]->(b)
            SET r.opening_id = $opening_id
        """, id_a=id_a, id_b=id_b, opening_id=props.get("opening_id", ""))

    elif rel_type == "VOIDS":
        session.run("""
            MATCH (a:Component {component_id: $id_a})
            MERGE (a)-[r:HAS_OPENING]->(a)
            SET r.opening_id = $opening_id,
                r.opening_name = $opening_name
        """, id_a=id_a, opening_id=props.get("opening_id", ""), opening_name=props.get("opening_name", ""))

    elif rel_type == "BOUNDS":
        session.run("""
            MATCH (c:Component {component_id: $id_a})
            MATCH (s:Space {revit_id: $space_id, project_id: $project_id})
            MERGE (c)-[r:BOUNDS]->(s)
            SET r.boundary_type = $boundary_type
        """, id_a=id_a, space_id=props.get("space_id", ""),
             project_id=rel["project_id"], boundary_type=props.get("boundary_type", ""))

    elif rel_type == "CONTAINS":
        session.run("""
            MATCH (c:Component {component_id: $id_a})
            MATCH (s:Space {revit_id: $space_id, project_id: $project_id})
            MERGE (s)-[r:CONTAINS]->(c)
        """, id_a=id_a, space_id=props.get("space_id", ""), project_id=rel["project_id"])

    elif rel_type == "FLOWS_INTO":
        session.run("""
            MATCH (a:Component {component_id: $id_a})
            MATCH (b:Component {component_id: $id_b})
            MERGE (a)-[r:FLOWS_INTO]->(b)
            SET r.flow_direction = $flow_direction,
                r.port_a = $port_a,
                r.port_b = $port_b
        """, id_a=id_a, id_b=id_b,
             flow_direction=props.get("flow_direction", ""),
             port_a=props.get("port_a", ""),
             port_b=props.get("port_b", ""))

    elif rel_type == "PART_OF":
        session.run("""
            MATCH (a:Component {component_id: $id_a})
            MATCH (b:Component {component_id: $id_b})
            MERGE (a)-[r:PART_OF]->(b)
        """, id_a=id_a, id_b=id_b)

    elif rel_type == "ASSIGNED_TO":
        session.run("""
            MATCH (c:Component {component_id: $id_a})
            MERGE (sys:System {name: $system_name, project_id: $project_id})
            SET sys.system_type = $system_type
            MERGE (c)-[r:ASSIGNED_TO]->(sys)
        """, id_a=id_a, system_name=props.get("system_name", ""),
             project_id=rel["project_id"], system_type=props.get("system_type", ""))

    elif rel_type == "COVERED_BY":
        session.run("""
            MATCH (a:Component {component_id: $id_a})
            MATCH (b:Component {component_id: $id_b})
            MERGE (a)-[r:COVERED_BY]->(b)
        """, id_a=id_a, id_b=id_b)

# --- Get all projects ---
def get_projects(cursor):
    cursor.execute("SELECT id, name FROM projects WHERE status = 'done'")
    return cursor.fetchall()

# --- Get components with spatial data ---
def get_components(cursor, project_id):
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

# --- Get all spaces ---
def get_spaces(cursor, project_id):
    cursor.execute("""
        SELECT * FROM spaces WHERE project_id = %s
    """, (project_id,))
    return cursor.fetchall()

# --- Get all relationships ---
def get_relationships(cursor, project_id):
    cursor.execute("""
        SELECT * FROM relationships WHERE project_id = %s
    """, (project_id,))
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

            print(f"Processing: {pname} (id={pid})")

            # Clear old graph data
            clear_project_graph(session, pid)

            # Create component nodes
            components = get_components(cursor, pid)
            print(f"  Creating {len(components)} component nodes...")
            for row in components:
                create_component_node(session, row)
                if row["level"]:
                    create_floor_node(session, pid, row["level"], row["elevation"])
                    link_component_to_floor(session, row["id"], pid, row["level"])

            # Create space nodes
            spaces = get_spaces(cursor, pid)
            print(f"  Creating {len(spaces)} space nodes...")
            for space in spaces:
                create_space_node(session, space)
                if space["level"]:
                    link_space_to_floor(session, pid, space["revit_id"], space["level"])

            # Create building node and link floors
            create_building_node(session, pid, pname)
            link_floors_to_building(session, pid)

            # Create relationship edges from PostgreSQL relationships table
            relationships = get_relationships(cursor, pid)
            print(f"  Creating {len(relationships)} relationship edges...")
            for rel in relationships:
                try:
                    create_relationship_edge(session, rel)
                except Exception as e:
                    pass

            print(f"  ✓ Done\n")

    cursor.close()
    conn.close()
    driver.close()
    print("Graph build complete. Run spatial_analyzer.py to add calculated relationships.")

if __name__ == "__main__":
    build_graph()
