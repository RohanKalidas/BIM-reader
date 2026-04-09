import os
import json
import math
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

# --- Calculate distance between two components ---
def distance(a, b):
    try:
        return math.sqrt(
            (a["pos_x"] - b["pos_x"]) ** 2 +
            (a["pos_y"] - b["pos_y"]) ** 2 +
            (a["pos_z"] - b["pos_z"]) ** 2
        )
    except:
        return None

# --- Calculate angle between two components in degrees ---
def angle_between(a, b):
    try:
        dx = b["pos_x"] - a["pos_x"]
        dy = b["pos_y"] - a["pos_y"]
        angle = math.degrees(math.atan2(dy, dx))
        return round(angle, 2)
    except:
        return None

# --- Check if two bounding boxes overlap or touch ---
def boxes_intersect(bb_a, bb_b, tolerance=100):
    try:
        return (
            bb_a["min_x"] - tolerance <= bb_b["max_x"] and
            bb_a["max_x"] + tolerance >= bb_b["min_x"] and
            bb_a["min_y"] - tolerance <= bb_b["max_y"] and
            bb_a["max_y"] + tolerance >= bb_b["min_y"] and
            bb_a["min_z"] - tolerance <= bb_b["max_z"] and
            bb_a["max_z"] + tolerance >= bb_b["min_z"]
        )
    except:
        return False

# --- Check if component B is on top of component A ---
def is_on_top(a, b, tolerance=100):
    try:
        a_top = a["pos_z"] + (a.get("height_mm") or 0)
        b_bottom = b["pos_z"]
        return abs(a_top - b_bottom) <= tolerance
    except:
        return False

# --- Check if component B is embedded in component A ---
def is_embedded(a, b):
    try:
        bb_a = a.get("bounding_box", {})
        bb_b = b.get("bounding_box", {})
        if not bb_a or not bb_b:
            return False
        return (
            bb_b["min_x"] >= bb_a["min_x"] and
            bb_b["max_x"] <= bb_a["max_x"] and
            bb_b["min_y"] >= bb_a["min_y"] and
            bb_b["max_y"] <= bb_a["max_y"]
        )
    except:
        return False

# --- Create CONNECTS_TO relationship ---
def create_connects_to(session, id_a, id_b, angle, dist):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:CONNECTS_TO]->(b)
        SET r.angle = $angle,
            r.distance = $dist
    """, id_a=id_a, id_b=id_b, angle=angle, dist=dist)

# --- Create SITS_ON relationship ---
def create_sits_on(session, id_a, id_b):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[:SITS_ON]->(b)
    """, id_a=id_a, id_b=id_b)

# --- Create EMBEDDED_IN relationship ---
def create_embedded_in(session, id_a, id_b):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[:EMBEDDED_IN]->(b)
    """, id_a=id_a, id_b=id_b)

# --- Create FLOWS_INTO relationship for MEP ---
def create_flows_into(session, id_a, id_b):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[:FLOWS_INTO]->(b)
    """, id_a=id_a, id_b=id_b)

# --- Create PENETRATES relationship ---
def create_penetrates(session, id_a, id_b):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[:PENETRATES]->(b)
    """, id_a=id_a, id_b=id_b)

# --- Get all components with spatial data from Neo4j ---
def get_graph_components(session, project_id):
    result = session.run("""
        MATCH (c:Component {project_id: $project_id})
        RETURN c
    """, project_id=project_id)
    return [record["c"] for record in result]

# --- Analyze architectural relationships ---
def analyze_architectural(session, components):
    walls = [c for c in components if c["normalized_category"] == "wall"]
    slabs = [c for c in components if c["normalized_category"] in ["slab", "floor"]]
    roofs = [c for c in components if c["normalized_category"] == "roof"]
    doors = [c for c in components if c["normalized_category"] == "door"]
    windows = [c for c in components if c["normalized_category"] == "window"]

    print(f"  Analyzing {len(walls)} walls...")

    # Wall to wall connections
    for i, wall_a in enumerate(walls):
        for wall_b in walls[i+1:]:
            if wall_a["component_id"] == wall_b["component_id"]:
                continue

            if wall_a["pos_x"] is None or wall_b["pos_x"] is None:
                continue

            dist = distance(wall_a, wall_b)
            if dist is not None and dist < 500:
                ang = angle_between(wall_a, wall_b)
                create_connects_to(session, wall_a["component_id"], wall_b["component_id"], ang, dist)

    # Slabs and roofs sit on walls
    for slab in slabs + roofs:
        for wall in walls:
            if slab["pos_x"] is None or wall["pos_x"] is None:
                continue
            if is_on_top(wall, slab):
                create_sits_on(session, slab["component_id"], wall["component_id"])

    # Doors and windows embedded in walls
    for door in doors + windows:
        for wall in walls:
            if door["pos_x"] is None or wall["pos_x"] is None:
                continue
            if is_embedded(wall, door):
                create_embedded_in(session, door["component_id"], wall["component_id"])

    print(f"  ✓ Architectural relationships done")

# --- Analyze MEP relationships ---
def analyze_mep(session, components):
    mep = [c for c in components if c["is_mep"]]
    walls = [c for c in components if c["normalized_category"] == "wall"]
    slabs = [c for c in components if c["normalized_category"] in ["slab", "floor"]]

    print(f"  Analyzing {len(mep)} MEP components...")

    # MEP to MEP flow connections
    for i, mep_a in enumerate(mep):
        for mep_b in mep[i+1:]:
            if mep_a["component_id"] == mep_b["component_id"]:
                continue
            if mep_a["pos_x"] is None or mep_b["pos_x"] is None:
                continue
            if mep_a["mep_system_type"] != mep_b["mep_system_type"]:
                continue

            dist = distance(mep_a, mep_b)
            if dist is not None and dist < 1000:
                create_flows_into(session, mep_a["component_id"], mep_b["component_id"])

    # MEP penetrating walls and slabs
    for mep_comp in mep:
        for wall in walls + slabs:
            if mep_comp["pos_x"] is None or wall["pos_x"] is None:
                continue
            bb_mep = mep_comp.get("bounding_box") or {}
            bb_wall = wall.get("bounding_box") or {}
            if bb_mep and bb_wall and boxes_intersect(bb_mep, bb_wall, tolerance=50):
                create_penetrates(session, mep_comp["component_id"], wall["component_id"])

    print(f"  ✓ MEP relationships done")

# --- Get all projects ---
def get_projects(cursor):
    cursor.execute("SELECT id, name FROM projects WHERE status = 'done'")
    return cursor.fetchall()

# --- Main ---
def analyze(project_id=None):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    driver = get_neo4j()

    projects = get_projects(cursor)
    if project_id:
        projects = [p for p in projects if p["id"] == project_id]

    print(f"Analyzing spatial relationships for {len(projects)} project(s)\n")

    with driver.session() as session:
        for project in projects:
            pid = project["id"]
            pname = project["name"]

            print(f"Project: {pname} (id={pid})")

            components = get_graph_components(session, pid)
            print(f"  Found {len(components)} nodes in graph")

            analyze_architectural(session, components)
            analyze_mep(session, components)

            print(f"  ✓ Done\n")

    cursor.close()
    conn.close()
    driver.close()
    print("Spatial analysis complete.")

if __name__ == "__main__":
    analyze()
