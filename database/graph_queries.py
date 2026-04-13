"""
database/graph_queries.py — Neo4j query functions for BIM Component Stripper.

Uses the centralized database module for the Neo4j driver.
"""

import logging
from database.db import get_neo4j

logger = logging.getLogger(__name__)


def _query(cypher, params=None, transform=None):
    """Run a single read query and return transformed results."""
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run(cypher, **(params or {}))
        if transform:
            return [transform(record) for record in result]
        return [dict(record[0]) for record in result]


# --- Components ---

def get_all_components(project_id):
    return _query(
        "MATCH (c:Component {project_id: $pid}) RETURN c ORDER BY c.category",
        {"pid": project_id},
        lambda r: dict(r["c"]),
    )


def get_walls(project_id):
    return _query(
        """MATCH (c:Component {project_id: $pid})
           WHERE c.category IN ['IfcWall', 'IfcWallStandardCase', 'IfcWallElementedCase']
           RETURN c ORDER BY c.length_mm DESC""",
        {"pid": project_id},
        lambda r: dict(r["c"]),
    )


def get_slabs(project_id):
    return _query(
        """MATCH (c:Component {project_id: $pid})
           WHERE c.category IN ['IfcSlab', 'IfcPlate']
           RETURN c""",
        {"pid": project_id},
        lambda r: dict(r["c"]),
    )


def get_mep_components(project_id):
    return _query(
        """MATCH (c:Component {project_id: $pid})
           WHERE c.is_mep = true
           RETURN c ORDER BY c.mep_system_type""",
        {"pid": project_id},
        lambda r: dict(r["c"]),
    )


def get_structural_components(project_id):
    return _query(
        """MATCH (c:Component {project_id: $pid})
           WHERE c.is_structural = true
           RETURN c ORDER BY c.category""",
        {"pid": project_id},
        lambda r: dict(r["c"]),
    )


# --- Floors & Spaces ---

def get_components_on_floor(project_id, floor_name):
    return _query(
        """MATCH (c:Component {project_id: $pid})-[:ON_FLOOR]->(f:Floor {name: $floor})
           RETURN c""",
        {"pid": project_id, "floor": floor_name},
        lambda r: dict(r["c"]),
    )


def get_floors(project_id):
    return _query(
        """MATCH (f:Floor {project_id: $pid})
           RETURN f ORDER BY f.elevation""",
        {"pid": project_id},
        lambda r: dict(r["f"]),
    )


def get_spaces(project_id):
    return _query(
        """MATCH (s:Space {project_id: $pid})
           RETURN s ORDER BY s.level, s.name""",
        {"pid": project_id},
        lambda r: dict(r["s"]),
    )


# --- Relationships ---

def get_connections(component_id):
    return _query(
        """MATCH (a:Component {component_id: $cid})-[r:CONNECTS_TO]->(b:Component)
           RETURN b, r.angle as angle, r.distance as distance, r.source as source""",
        {"cid": component_id},
        lambda r: {
            "component": dict(r["b"]),
            "angle": r["angle"],
            "distance": r["distance"],
            "source": r["source"],
        },
    )


def get_wall_openings(component_id):
    return _query(
        """MATCH (a:Component)-[:FILLS]->(w:Component {component_id: $cid})
           RETURN a""",
        {"cid": component_id},
        lambda r: dict(r["a"]),
    )


def get_mep_flow_network(project_id, mep_system_type):
    return _query(
        """MATCH (a:Component {project_id: $pid, mep_system_type: $mst})
                 -[r:FLOWS_INTO]->
                 (b:Component {project_id: $pid})
           RETURN a, b, r.source as source""",
        {"pid": project_id, "mst": mep_system_type},
        lambda r: {
            "from": dict(r["a"]),
            "to": dict(r["b"]),
            "source": r["source"],
        },
    )


def get_wall_penetrations(project_id):
    return _query(
        """MATCH (mep:Component {project_id: $pid})-[:PENETRATES]->(w:Component)
           WHERE w.category IN ['IfcWall', 'IfcWallStandardCase', 'IfcSlab', 'IfcPlate']
           RETURN mep, w""",
        {"pid": project_id},
        lambda r: {"mep": dict(r["mep"]), "wall": dict(r["w"])},
    )


def get_space_boundaries(project_id, space_name):
    return _query(
        """MATCH (c:Component {project_id: $pid})-[:BOUNDS]->(s:Space {name: $sname})
           RETURN c""",
        {"pid": project_id, "sname": space_name},
        lambda r: dict(r["c"]),
    )


def get_space_contents(project_id, space_name):
    return _query(
        """MATCH (s:Space {project_id: $pid, name: $sname})-[:CONTAINS]->(c:Component)
           RETURN c""",
        {"pid": project_id, "sname": space_name},
        lambda r: dict(r["c"]),
    )


def get_building_structure(project_id):
    return _query(
        """MATCH (b:Building {project_id: $pid})
           MATCH (f:Floor)-[:PART_OF]->(b)
           MATCH (c:Component)-[:ON_FLOOR]->(f)
           RETURN b, f, c ORDER BY f.elevation, c.category""",
        {"pid": project_id},
        lambda r: {
            "building": dict(r["b"]),
            "floor": dict(r["f"]),
            "component": dict(r["c"]),
        },
    )


def get_reconstruction_data(project_id):
    return _query(
        """MATCH (c:Component {project_id: $pid})
           OPTIONAL MATCH (c)-[r]->(other:Component)
           RETURN c, type(r) as relationship_type, other
           ORDER BY c.pos_z, c.category""",
        {"pid": project_id},
        lambda r: {
            "component": dict(r["c"]),
            "relationship": r["relationship_type"],
            "connected_to": dict(r["other"]) if r["other"] else None,
        },
    )


def get_high_quality(project_id, min_score=0.8):
    return _query(
        """MATCH (c:Component {project_id: $pid})
           WHERE c.quality_score >= $min_score
           RETURN c ORDER BY c.quality_score DESC""",
        {"pid": project_id, "min_score": min_score},
        lambda r: dict(r["c"]),
    )


def get_adjacent_components(component_id):
    return _query(
        """MATCH (a:Component {component_id: $cid})-[r:ADJACENT_TO]->(b:Component)
           RETURN b, r.distance as distance""",
        {"cid": component_id},
        lambda r: {"component": dict(r["b"]), "distance": r["distance"]},
    )


def get_mep_systems(project_id):
    return _query(
        """MATCH (c:Component {project_id: $pid})-[:ASSIGNED_TO]->(s:System)
           RETURN s.name as system_name, s.system_type as system_type, collect(c) as components""",
        {"pid": project_id},
        lambda r: {
            "system_name": r["system_name"],
            "system_type": r["system_type"],
            "components": [dict(c) for c in r["components"]],
        },
    )


def get_all_component_relationships(component_id):
    return _query(
        """MATCH (a:Component {component_id: $cid})-[r]->(b)
           RETURN type(r) as relationship_type, b, r""",
        {"cid": component_id},
        lambda r: {
            "relationship_type": r["relationship_type"],
            "connected_to": dict(r["b"]),
            "properties": dict(r["r"]),
        },
    )


def get_by_normalized_category(project_id, normalized_category):
    return _query(
        """MATCH (c:Component {project_id: $pid, normalized_category: $cat})
           RETURN c""",
        {"pid": project_id, "cat": normalized_category},
        lambda r: dict(r["c"]),
    )
