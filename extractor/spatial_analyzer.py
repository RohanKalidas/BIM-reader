"""
extractor/spatial_analyzer.py — Calculates spatial relationships from geometry.

Uses centralized database module. Pre-fetches explicit relationships into an
in-memory set to avoid per-pair Neo4j queries. Batches Neo4j writes.
"""

import math
import logging
import psycopg2.extras
from database.db import get_db_connection, get_neo4j

logger = logging.getLogger(__name__)

NEO4J_BATCH_SIZE = 500

# --- Math helpers ---

def distance(a, b):
    try:
        return math.sqrt(
            (a["pos_x"] - b["pos_x"]) ** 2 +
            (a["pos_y"] - b["pos_y"]) ** 2 +
            (a["pos_z"] - b["pos_z"]) ** 2
        )
    except (TypeError, KeyError):
        return None


def angle_between(a, b):
    try:
        dx = b["pos_x"] - a["pos_x"]
        dy = b["pos_y"] - a["pos_y"]
        return round(math.degrees(math.atan2(dy, dx)), 2)
    except (TypeError, KeyError):
        return None


def angle_difference(rot_a, rot_b):
    try:
        diff = abs(rot_a - rot_b) % 360
        return min(diff, 360 - diff)
    except (TypeError, ValueError):
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
    except (TypeError, KeyError):
        return False


def is_on_top(a, b, tolerance=100):
    try:
        a_top = a["pos_z"] + (a.get("height_mm") or 0)
        b_bottom = b["pos_z"]
        return abs(a_top - b_bottom) <= tolerance
    except (TypeError, KeyError):
        return False


def is_same_level(a, b):
    try:
        return a["level"] == b["level"] and a["level"] is not None
    except (TypeError, KeyError):
        return False


def is_structurally_above(a, b, tolerance=500):
    try:
        return abs(a["pos_z"] - (b["pos_z"] + (b.get("height_mm") or 0))) <= tolerance
    except (TypeError, KeyError):
        return False


# --- Pre-fetch explicit relationships into a set ---

def load_explicit_pairs(session, project_id):
    """Load all explicit relationship pairs into a set for O(1) lookup."""
    result = session.run("""
        MATCH (a:Component {project_id: $pid})-[r {source: 'explicit'}]->(b:Component)
        RETURN a.component_id AS a_id, b.component_id AS b_id
    """, pid=project_id)
    pairs = set()
    for record in result:
        pairs.add((record["a_id"], record["b_id"]))
        pairs.add((record["b_id"], record["a_id"]))  # bidirectional check
    return pairs


def has_explicit(explicit_pairs, id_a, id_b):
    return (id_a, id_b) in explicit_pairs


# --- Batch Neo4j write helpers ---

def batch_write(session, cypher, items):
    """Write items to Neo4j in batches."""
    for i in range(0, len(items), NEO4J_BATCH_SIZE):
        session.run(cypher, batch=items[i:i + NEO4J_BATCH_SIZE])


# --- Analyze wall relationships ---

def analyze_walls(session, walls, explicit_pairs):
    print(f"    Analyzing {len(walls)} walls...")
    connects = []
    adjacents = []

    for i, wall_a in enumerate(walls):
        if wall_a.get("pos_x") is None:
            continue
        for wall_b in walls[i+1:]:
            if wall_a["component_id"] == wall_b["component_id"]:
                continue
            if wall_b.get("pos_x") is None:
                continue
            if not is_same_level(wall_a, wall_b):
                continue

            dist = distance(wall_a, wall_b)
            if dist is None:
                continue

            ang = angle_between(wall_a, wall_b)

            if dist < 300:
                if not has_explicit(explicit_pairs, wall_a["component_id"], wall_b["component_id"]):
                    connects.append({"a": wall_a["component_id"], "b": wall_b["component_id"],
                                     "angle": ang, "dist": dist})
            elif dist < 2000:
                adjacents.append({"a": wall_a["component_id"], "b": wall_b["component_id"],
                                  "dist": dist})

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:CONNECTS_TO]->(b)
        SET r.angle = row.angle, r.distance = row.dist, r.source = 'calculated'
    """, connects)

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:ADJACENT_TO]->(b)
        SET r.distance = row.dist
    """, adjacents)

    print(f"    -> {len(connects)} wall connections, {len(adjacents)} adjacencies")


# --- Analyze slab and roof relationships ---

def analyze_slabs_and_roofs(session, slabs, roofs, walls, columns):
    print(f"    Analyzing {len(slabs)} slabs and {len(roofs)} roofs...")
    sits_on_items = []
    supported_items = []

    for slab in slabs + roofs:
        if slab.get("pos_x") is None:
            continue

        for wall in walls:
            if wall.get("pos_x") is None:
                continue
            if is_on_top(wall, slab):
                sits_on_items.append({"a": slab["component_id"], "b": wall["component_id"]})

        for col in columns:
            if col.get("pos_x") is None:
                continue
            if is_structurally_above(slab, col):
                supported_items.append({"a": slab["component_id"], "b": col["component_id"]})

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:SITS_ON]->(b)
        SET r.source = 'calculated'
    """, sits_on_items)

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:SUPPORTED_BY]->(b)
    """, supported_items)

    print(f"    -> {len(sits_on_items)} sits_on, {len(supported_items)} supported_by")


# --- Analyze structural relationships ---

def analyze_structural(session, beams, columns):
    print(f"    Analyzing {len(beams)} beams and {len(columns)} columns...")
    supported_items = []

    for beam in beams:
        if beam.get("pos_x") is None:
            continue
        for col in columns:
            if col.get("pos_x") is None:
                continue
            dist = distance(beam, col)
            if dist is not None and dist < 500:
                supported_items.append({"a": beam["component_id"], "b": col["component_id"]})

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:SUPPORTED_BY]->(b)
    """, supported_items)

    print(f"    -> {len(supported_items)} structural relationships")


# --- Analyze MEP relationships ---

def analyze_mep(session, mep_components, walls, slabs, explicit_pairs):
    print(f"    Analyzing {len(mep_components)} MEP components...")
    flow_items = []
    penetration_items = []
    runs_along_items = []

    # Group by system type
    by_system = {}
    for comp in mep_components:
        sys_type = comp.get("mep_system_type") or "unknown"
        by_system.setdefault(sys_type, []).append(comp)

    # Flow connections within same system
    for sys_type, comps in by_system.items():
        for i, mep_a in enumerate(comps):
            if mep_a.get("pos_x") is None:
                continue
            for mep_b in comps[i+1:]:
                if mep_a["component_id"] == mep_b["component_id"]:
                    continue
                if mep_b.get("pos_x") is None:
                    continue
                if has_explicit(explicit_pairs, mep_a["component_id"], mep_b["component_id"]):
                    continue
                dist = distance(mep_a, mep_b)
                if dist is not None and dist < 1500:
                    flow_items.append({"a": mep_a["component_id"], "b": mep_b["component_id"],
                                       "dist": dist})

    # Penetrations and runs_along
    for mep in mep_components:
        if mep.get("pos_x") is None:
            continue
        bb_mep = mep.get("bounding_box") or {}

        for wall in walls + slabs:
            if wall.get("pos_x") is None:
                continue
            bb_wall = wall.get("bounding_box") or {}
            if bb_mep and bb_wall and boxes_intersect(bb_mep, bb_wall, tolerance=50):
                penetration_items.append({"a": mep["component_id"], "b": wall["component_id"]})

        for wall in walls:
            if wall.get("pos_x") is None:
                continue
            if not is_same_level(mep, wall):
                continue
            dist = distance(mep, wall)
            if dist is not None and 100 < dist < 500:
                runs_along_items.append({"a": mep["component_id"], "b": wall["component_id"]})

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:FLOWS_INTO]->(b)
        SET r.distance = row.dist, r.source = 'calculated'
    """, flow_items)

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:PENETRATES]->(b)
        SET r.source = 'calculated'
    """, penetration_items)

    batch_write(session, """
        UNWIND $batch AS row
        MATCH (a:Component {component_id: row.a})
        MATCH (b:Component {component_id: row.b})
        MERGE (a)-[r:RUNS_ALONG]->(b)
    """, runs_along_items)

    print(f"    -> {len(flow_items)} flow connections, {len(penetration_items)} penetrations, {len(runs_along_items)} runs_along")


# --- Get components from Neo4j ---

def get_components(session, project_id):
    result = session.run("""
        MATCH (c:Component {project_id: $pid})
        RETURN c
    """, pid=project_id)
    return [dict(record["c"]) for record in result]


# --- Main ---

def analyze(project_id=None):
    driver = get_neo4j()

    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT id, name FROM projects WHERE status = 'done'")
        projects = cursor.fetchall()
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

            # Pre-fetch all explicit relationships for O(1) lookup
            explicit_pairs = load_explicit_pairs(session, pid)
            print(f"  Pre-loaded {len(explicit_pairs)} explicit relationship pairs\n")

            # Categorize components
            walls = [c for c in components if c.get("category") in [
                "IfcWall", "IfcWallStandardCase", "IfcWallElementedCase"
            ]]
            slabs = [c for c in components if c.get("category") in [
                "IfcSlab", "IfcPlate"
            ]]
            roofs = [c for c in components if c.get("category") == "IfcRoof"]
            columns = [c for c in components if c.get("category") in [
                "IfcColumn", "IfcColumnStandardCase"
            ]]
            beams = [c for c in components if c.get("category") in [
                "IfcBeam", "IfcBeamStandardCase"
            ]]
            mep = [c for c in components if c.get("category") in [
                "IfcDuctSegment", "IfcDuctFitting", "IfcPipeSegment", "IfcPipeFitting",
                "IfcAirTerminal", "IfcValve", "IfcPump", "IfcFan",
                "IfcFlowSegment", "IfcFlowFitting", "IfcFlowTerminal",
                "IfcFlowController", "IfcFlowMovingDevice", "IfcFlowStorageDevice",
                "IfcElectricAppliance", "IfcLightFixture", "IfcOutlet",
                "IfcElectricDistributionBoard", "IfcDistributionFlowElement"
            ]]

            print("  Architectural:")
            analyze_walls(session, walls, explicit_pairs)
            analyze_slabs_and_roofs(session, slabs, roofs, walls, columns)

            print("  Structural:")
            analyze_structural(session, beams, columns)

            print("  MEP:")
            analyze_mep(session, mep, walls, slabs, explicit_pairs)

            print(f"\n  Project done\n")

    print("Spatial analysis complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    analyze()
