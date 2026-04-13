"""
extractor/graph_builder.py — Builds Neo4j graph from PostgreSQL data.

Uses centralized database module. Batches Neo4j operations using UNWIND
for dramatically faster graph construction on large models.
"""

import logging
import psycopg2.extras
from database.db import get_db_connection, get_neo4j

logger = logging.getLogger(__name__)

# Batch size for Neo4j UNWIND operations
NEO4J_BATCH_SIZE = 500


# --- Batched node creation ---

def batch_create_components(session, rows):
    """Create component nodes in batches using UNWIND."""
    for i in range(0, len(rows), NEO4J_BATCH_SIZE):
        batch = rows[i:i + NEO4J_BATCH_SIZE]
        params = []
        for row in batch:
            enrichment = row["parameters"].get("ai_enrichment", {}) if row["parameters"] else {}
            params.append({
                "component_id": row["id"],
                "project_id": row["project_id"],
                "revit_id": row["revit_id"],
                "category": row["category"],
                "normalized_category": enrichment.get("normalized_category", row["category"]),
                "family_name": row["family_name"] or "",
                "type_name": row["type_name"] or "",
                "is_mep": enrichment.get("is_mep", False),
                "is_structural": enrichment.get("is_structural", False),
                "mep_system_type": enrichment.get("mep_system_type", None),
                "pos_x": row["pos_x"],
                "pos_y": row["pos_y"],
                "pos_z": row["pos_z"],
                "rot_x": row["rot_x"],
                "rot_y": row["rot_y"],
                "rot_z": row["rot_z"],
                "level": row["level"],
                "elevation": row["elevation"],
                "width_mm": row["width_mm"],
                "height_mm": row["height_mm"],
                "length_mm": row["length_mm"],
                "area_m2": row["area_m2"],
                "volume_m3": row["volume_m3"],
                "quality_score": row["quality_score"],
            })

        session.run("""
            UNWIND $batch AS row
            MERGE (c:Component {component_id: row.component_id})
            SET c.project_id = row.project_id,
                c.revit_id = row.revit_id,
                c.category = row.category,
                c.normalized_category = row.normalized_category,
                c.family_name = row.family_name,
                c.type_name = row.type_name,
                c.is_mep = row.is_mep,
                c.is_structural = row.is_structural,
                c.mep_system_type = row.mep_system_type,
                c.pos_x = row.pos_x,
                c.pos_y = row.pos_y,
                c.pos_z = row.pos_z,
                c.rot_x = row.rot_x,
                c.rot_y = row.rot_y,
                c.rot_z = row.rot_z,
                c.level = row.level,
                c.elevation = row.elevation,
                c.width_mm = row.width_mm,
                c.height_mm = row.height_mm,
                c.length_mm = row.length_mm,
                c.area_m2 = row.area_m2,
                c.volume_m3 = row.volume_m3,
                c.quality_score = row.quality_score
        """, batch=params)


def batch_create_floor_nodes(session, project_id, rows):
    """Create floor nodes and link components to floors in batch."""
    # Collect unique levels
    levels = {}
    for row in rows:
        if row["level"] and row["level"] not in levels:
            levels[row["level"]] = row["elevation"]

    if not levels:
        return

    # Create floor nodes
    floor_params = [{"name": name, "elevation": elev} for name, elev in levels.items()]
    session.run("""
        UNWIND $batch AS row
        MERGE (f:Floor {project_id: $pid, name: row.name})
        SET f.elevation = row.elevation
    """, batch=floor_params, pid=project_id)

    # Link components to floors in batch
    links = [{"component_id": r["id"], "level": r["level"]}
             for r in rows if r["level"]]

    for i in range(0, len(links), NEO4J_BATCH_SIZE):
        batch = links[i:i + NEO4J_BATCH_SIZE]
        session.run("""
            UNWIND $batch AS row
            MATCH (c:Component {component_id: row.component_id})
            MATCH (f:Floor {project_id: $pid, name: row.level})
            MERGE (c)-[:ON_FLOOR]->(f)
        """, batch=batch, pid=project_id)


def batch_create_spaces(session, spaces, project_id):
    """Create space nodes in batch."""
    if not spaces:
        return

    params = []
    for row in spaces:
        params.append({
            "revit_id": row["revit_id"],
            "name": row["name"] or "",
            "long_name": row["long_name"] or "",
            "level": row["level"],
            "elevation": row["elevation"],
            "area_m2": row["area_m2"],
            "volume_m3": row["volume_m3"],
        })

    for i in range(0, len(params), NEO4J_BATCH_SIZE):
        batch = params[i:i + NEO4J_BATCH_SIZE]
        session.run("""
            UNWIND $batch AS row
            MERGE (s:Space {revit_id: row.revit_id, project_id: $pid})
            SET s.name = row.name,
                s.long_name = row.long_name,
                s.level = row.level,
                s.elevation = row.elevation,
                s.area_m2 = row.area_m2,
                s.volume_m3 = row.volume_m3
        """, batch=batch, pid=project_id)

    # Link spaces to floors
    space_links = [{"revit_id": s["revit_id"], "level": s["level"]}
                   for s in spaces if s["level"]]
    if space_links:
        session.run("""
            UNWIND $batch AS row
            MATCH (s:Space {revit_id: row.revit_id, project_id: $pid})
            MATCH (f:Floor {project_id: $pid, name: row.level})
            MERGE (s)-[:ON_FLOOR]->(f)
        """, batch=space_links, pid=project_id)


# --- Relationship edge creation (batched by type) ---

def batch_create_relationships(session, relationships, project_id):
    """Create relationship edges in Neo4j, batched by type."""
    # Group by type
    by_type = {}
    for rel in relationships:
        rt = rel["relationship_type"]
        if rt not in by_type:
            by_type[rt] = []
        by_type[rt].append(rel)

    for rel_type, rels in by_type.items():
        try:
            _create_edges_for_type(session, rel_type, rels, project_id)
        except Exception as e:
            logger.warning("Failed to create %s edges: %s", rel_type, e)


def _create_edges_for_type(session, rel_type, rels, project_id):
    """Batch-create edges for a single relationship type."""

    if rel_type == "CONNECTS_TO":
        params = [{"a": r["component_a_id"], "b": r["component_b_id"]} for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (a:Component {component_id: row.a})
                MATCH (b:Component {component_id: row.b})
                MERGE (a)-[r:CONNECTS_TO]->(b)
                SET r.source = 'explicit'
            """, batch=params[i:i + NEO4J_BATCH_SIZE])

    elif rel_type == "FILLS":
        params = [{"a": r["component_a_id"], "b": r["component_b_id"],
                    "opening_id": (r["properties"] or {}).get("opening_id", "")}
                   for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (a:Component {component_id: row.a})
                MATCH (b:Component {component_id: row.b})
                MERGE (a)-[r:FILLS]->(b)
                SET r.opening_id = row.opening_id
            """, batch=params[i:i + NEO4J_BATCH_SIZE])

    elif rel_type == "VOIDS":
        params = [{"a": r["component_a_id"],
                    "opening_id": (r["properties"] or {}).get("opening_id", ""),
                    "opening_name": (r["properties"] or {}).get("opening_name", "")}
                   for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (a:Component {component_id: row.a})
                MERGE (a)-[r:HAS_OPENING]->(a)
                SET r.opening_id = row.opening_id,
                    r.opening_name = row.opening_name
            """, batch=params[i:i + NEO4J_BATCH_SIZE])

    elif rel_type == "BOUNDS":
        params = [{"a": r["component_a_id"],
                    "space_id": (r["properties"] or {}).get("space_id", ""),
                    "boundary_type": (r["properties"] or {}).get("boundary_type", "")}
                   for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (c:Component {component_id: row.a})
                MATCH (s:Space {revit_id: row.space_id, project_id: $pid})
                MERGE (c)-[r:BOUNDS]->(s)
                SET r.boundary_type = row.boundary_type
            """, batch=params[i:i + NEO4J_BATCH_SIZE], pid=project_id)

    elif rel_type == "CONTAINS":
        params = [{"a": r["component_a_id"],
                    "space_id": (r["properties"] or {}).get("space_id", "")}
                   for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (c:Component {component_id: row.a})
                MATCH (s:Space {revit_id: row.space_id, project_id: $pid})
                MERGE (s)-[r:CONTAINS]->(c)
            """, batch=params[i:i + NEO4J_BATCH_SIZE], pid=project_id)

    elif rel_type == "FLOWS_INTO":
        params = [{"a": r["component_a_id"], "b": r["component_b_id"],
                    "flow_direction": (r["properties"] or {}).get("flow_direction", ""),
                    "port_a": (r["properties"] or {}).get("port_a", ""),
                    "port_b": (r["properties"] or {}).get("port_b", "")}
                   for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (a:Component {component_id: row.a})
                MATCH (b:Component {component_id: row.b})
                MERGE (a)-[r:FLOWS_INTO]->(b)
                SET r.flow_direction = row.flow_direction,
                    r.port_a = row.port_a,
                    r.port_b = row.port_b
            """, batch=params[i:i + NEO4J_BATCH_SIZE])

    elif rel_type == "PART_OF":
        params = [{"a": r["component_a_id"], "b": r["component_b_id"]} for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (a:Component {component_id: row.a})
                MATCH (b:Component {component_id: row.b})
                MERGE (a)-[r:PART_OF]->(b)
            """, batch=params[i:i + NEO4J_BATCH_SIZE])

    elif rel_type == "ASSIGNED_TO":
        params = [{"a": r["component_a_id"],
                    "system_name": (r["properties"] or {}).get("system_name", ""),
                    "system_type": (r["properties"] or {}).get("system_type", "")}
                   for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (c:Component {component_id: row.a})
                MERGE (sys:System {name: row.system_name, project_id: $pid})
                SET sys.system_type = row.system_type
                MERGE (c)-[r:ASSIGNED_TO]->(sys)
            """, batch=params[i:i + NEO4J_BATCH_SIZE], pid=project_id)

    elif rel_type == "COVERED_BY":
        params = [{"a": r["component_a_id"], "b": r["component_b_id"]} for r in rels]
        for i in range(0, len(params), NEO4J_BATCH_SIZE):
            session.run("""
                UNWIND $batch AS row
                MATCH (a:Component {component_id: row.a})
                MATCH (b:Component {component_id: row.b})
                MERGE (a)-[r:COVERED_BY]->(b)
            """, batch=params[i:i + NEO4J_BATCH_SIZE])


# --- Data loading ---

def get_projects(cursor):
    cursor.execute("SELECT id, name FROM projects WHERE status = 'done'")
    return cursor.fetchall()


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


def get_spaces(cursor, project_id):
    cursor.execute("SELECT * FROM spaces WHERE project_id = %s", (project_id,))
    return cursor.fetchall()


def get_relationships(cursor, project_id):
    cursor.execute("SELECT * FROM relationships WHERE project_id = %s", (project_id,))
    return cursor.fetchall()


# --- Main ---

def build_graph(project_id=None):
    driver = get_neo4j()

    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
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
                session.run("""
                    MATCH (n {project_id: $project_id})
                    DETACH DELETE n
                """, project_id=pid)

                # Create component nodes (batched)
                components = get_components(cursor, pid)
                print(f"  Creating {len(components)} component nodes...")
                batch_create_components(session, components)

                # Create floor nodes and links (batched)
                batch_create_floor_nodes(session, pid, components)

                # Create space nodes (batched)
                spaces = get_spaces(cursor, pid)
                print(f"  Creating {len(spaces)} space nodes...")
                batch_create_spaces(session, spaces, pid)

                # Create building node and link floors
                session.run("""
                    MERGE (b:Building {project_id: $pid})
                    SET b.name = $name
                """, pid=pid, name=pname)

                session.run("""
                    MATCH (b:Building {project_id: $pid})
                    MATCH (f:Floor {project_id: $pid})
                    MERGE (f)-[:PART_OF]->(b)
                """, pid=pid)

                # Create relationship edges (batched by type)
                relationships = get_relationships(cursor, pid)
                print(f"  Creating {len(relationships)} relationship edges...")
                batch_create_relationships(session, relationships, pid)

                print(f"  Done\n")

    print("Graph build complete. Run spatial_analyzer.py to add calculated relationships.")


# Need this import here since get_db_connection returns tuples
import psycopg2.extras

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_graph()
