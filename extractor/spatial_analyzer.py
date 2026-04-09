import os
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

# --- Math helpers ---
def distance(a, b):
    try:
        return math.sqrt(
            (a["pos_x"] - b["pos_x"]) ** 2 +
            (a["pos_y"] - b["pos_y"]) ** 2 +
            (a["pos_z"] - b["pos_z"]) ** 2
        )
    except:
        return None

def angle_between(a, b):
    try:
        dx = b["pos_x"] - a["pos_x"]
        dy = b["pos_y"] - a["pos_y"]
        return round(math.degrees(math.atan2(dy, dx)), 2)
    except:
        return None

def angle_difference(rot_a, rot_b):
    try:
        diff = abs(rot_a - rot_b) % 360
        return min(diff, 360 - diff)
    except:
        return None

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

def is_on_top(a, b, tolerance=100):
    try:
        a_top = a["pos_z"] + (a.get("height_mm") or 0)
        b_bottom = b["pos_z"]
        return abs(a_top - b_bottom) <= tolerance
    except:
        return False

def is_same_level(a, b):
    try:
        return a["level"] == b["level"] and a["level"] is not None
    except:
        return False

def is_structurally_above(a, b, tolerance=500):
    try:
        return abs(a["pos_z"] - (b["pos_z"] + (b.get("height_mm") or 0))) <= tolerance
    except:
        return False

# --- Neo4j relationship creators ---
def create_connects_to(session, id_a, id_b, angle, dist, source="calculated"):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:CONNECTS_TO]->(b)
        SET r.angle = $angle,
            r.distance = $dist,
            r.source = $source
    """, id_a=id_a, id_b=id_b, angle=angle, dist=dist, source=source)

def create_sits_on(session, id_a, id_b, source="calculated"):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:SITS_ON]->(b)
        SET r.source = $source
    """, id_a=id_a, id_b=id_b, source=source)

def create_supported_by(session, id_a, id_b):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:SUPPORTED_BY]->(b)
    """, id_a=id_a, id_b=id_b)

def create_adjacent_to(session, id_a, id_b, dist):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:ADJACENT_TO]->(b)
        SET r.distance = $dist
    """, id_a=id_a, id_b=id_b, dist=dist)

def create_penetrates(session, id_a, id_b):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:PENETRATES]->(b)
        SET r.source = 'calculated'
    """, id_a=id_a, id_b=id_b)

def create_flows_into(session, id_a, id_b, dist):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:FLOWS_INTO]->(b)
        SET r.distance = $dist,
            r.source = 'calculated'
    """, id_a=id_a, id_b=id_b, dist=dist)

def create_runs_along(session, id_a, id_b):
    session.run("""
        MATCH (a:Component {component_id: $id_a})
        MATCH (b:Component {component_id: $id_b})
        MERGE (a)-[r:RUNS_ALONG]->(b)
    """, id_a=id_a, id_b=id_b)

# --- Get components from Neo4j ---
def get_components(session, project_id):
    result = session.run("""
        MATCH (c:Component {project_id: $project_id})
        RETURN c
    """, project_id=project_id)
    return [dict(record["c"]) for record in result]

# --- Check if explicit relationship already exists ---
def explicit_relationship_exists(session, id_a, id_b):
    result = session.run("""
        MATCH (a:Component {component_id: $id_a})-[r {source: 'explicit'}]->(b:Component {component_id: $id_b})
        RETURN count(r) as count
    """, id_a=id_a, id_b=id_b)
    return result.single()["count"] > 0

# --- Analyze wall relationships ---
def analyze_walls(session, walls):
    print(f"    Analyzing {len(walls)} walls...")
    connections = 0
    adjacencies = 0

    for i, wall_a in enumerate(walls):
        for wall_b in walls[i+1:]:
            if wall_a["component_id"] == wall_b["component_id"]:
                continue
            if wall_a.get("pos_x") is None or wall_b.get("pos_x") is None:
                continue
            if not is_same_level(wall_a, wall_b):
                continue

            dist = distance(wall_a, wall_b)
            if dist is None:
                continue

            ang = angle_between(wall_a, wall_b)

            # Direct connection — very close
            if dist < 300:
                if not explicit_relationship_exists(session, wall_a["component_id"], wall_b["component_id"]):
                    create_connects_to(session, wall_a["component_id"], wall_b["component_id"], ang, dist)
                    connections += 1

            # Adjacent — nearby but not touching
            elif dist < 2000:
                create_adjacent_to(session, wall_a["component_id"], wall_b["component_id"], dist)
                adjacencies += 1

    print(f"    → {connections} wall connections, {adjacencies} adjacencies")

# --- Analyze slab and roof relationships ---
def analyze_slabs_and_roofs(session, slabs, roofs, walls, columns):
    print(f"    Analyzing {len(slabs)} slabs and {len(roofs)} roofs...")
    sits_on = 0
    supported = 0

    for slab in slabs + roofs:
        if slab.get("pos_x") is None:
            continue

        # Sits on walls
        for wall in walls:
            if wall.get("pos_x") is None:
                continue
            if is_on_top(wall, slab):
                create_sits_on(session, slab["component_id"], wall["component_id"])
                sits_on += 1

        # Supported by columns
        for col in columns:
            if col.get("pos_x") is None:
                continue
            if is_structurally_above(slab, col):
                create_supported_by(session, slab["component_id"], col["component_id"])
                supported += 1

    print(f"    → {sits_on} sits_on, {supported} supported_by")

# --- Analyze structural relationships ---
def analyze_structural(session, beams, columns):
    print(f"    Analyzing {len(beams)} beams and {len(columns)} columns...")
    supported = 0

    for beam in beams:
        if beam.get("pos_x") is None:
            continue
        for col in columns:
            if col.get("pos_x") is None:
                continue
            dist = distance(beam, col)
            if dist is not None and dist < 500:
                create_supported_by(session, beam["component_id"], col["component_id"])
                supported += 1

    print(f"    → {supported} structural relationships")

# --- Analyze MEP relationships ---
def analyze_mep(session, mep_components, walls, slabs):
    print(f"    Analyzing {len(mep_components)} MEP components...")
    flows = 0
    penetrations = 0
    runs_along = 0

    # Group MEP by system type
    by_system = {}
    for comp in mep_components:
        sys_type = comp.get("mep_system_type") or "unknown"
        if sys_type not in by_system:
            by_system[sys_type] = []
        by_system[sys_type].append(comp)

    # Flow connections within same system type
    for sys_type, comps in by_system.items():
        for i, mep_a in enumerate(comps):
            for mep_b in comps[i+1:]:
                if mep_a["component_id"] == mep_b["component_id"]:
                    continue
                if mep_a.get("pos_x") is None or mep_b.get("pos_x") is None:
                    continue
                if explicit_relationship_exists(session, mep_a["component_id"], mep_b["component_id"]):
                    continue

                dist = distance(mep_a, mep_b)
                if dist is not None and dist < 1500:
                    create_flows_into(session, mep_a["component_id"], mep_b["component_id"], dist)
                    flows += 1

    # MEP penetrating walls and slabs
    for mep in mep_components:
        if mep.get("pos_x") is None:
            continue
        bb_mep = mep.get("bounding_box") or {}

        for wall in walls + slabs:
            if wall.get("pos_x") is None:
                continue
            bb_wall = wall.get("bounding_box") or {}
            if bb_mep and bb_wall and boxes_intersect(bb_mep, bb_wall, tolerance=50):
                create_penetrates(session, mep["component_id"], wall["component_id"])
                penetrations += 1

        # MEP running along walls
        for wall in walls:
            if wall.get("pos_x") is None:
                continue
            dist = distance(mep, wall)
            if dist is not None and 100 < dist < 500:
                create_runs_along(session, mep["component_id"], wall["component_id"])
                runs_along += 1

    print(f"    → {flows} flow connections, {penetrations} penetrations, {runs_along} runs_along")

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

            components = get_components(session, pid)
            print(f"  Found {len(components)} nodes in graph\n")

            # Sort components by type
            walls = [c for c in components if c.get("normalized_category") == "wall"]
            slabs = [c for c in components if c.get("normalized_category") in ["slab", "floor"]]
            roofs = [c for c in components if c.get("normalized_category") == "roof"]
            columns = [c for c in components if c.get("normalized_category") == "column"]
            beams = [c for c in components if c.get("normalized_category") == "beam"]
            mep = [c for c in components if c.get("is_mep")]

            print("  Architectural:")
            analyze_walls(session, walls)
            analyze_slabs_and_roofs(session, slabs, roofs, walls, columns)

            print("  Structural:")
            analyze_structural(session, beams, columns)

            print("  MEP:")
            analyze_mep(session, mep, walls, slabs)

            print(f"\n  ✓ Project done\n")

    cursor.close()
    conn.close()
    driver.close()
    print("Spatial analysis complete.")

if __name__ == "__main__":
    analyze()
