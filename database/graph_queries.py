import os
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

# --- Neo4j connection ---
def get_neo4j():
    return GraphDatabase.driver(
        os.getenv("NEO4J_URI"),
        auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD"))
    )

# --- Get all components in a project ---
def get_all_components(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            RETURN c
            ORDER BY c.category
        """, project_id=project_id)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get all walls in a project ---
def get_walls(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            WHERE c.category IN ['IfcWall', 'IfcWallStandardCase', 'IfcWallElementedCase']
            RETURN c
            ORDER BY c.length_mm DESC
        """, project_id=project_id)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get all slabs in a project ---
def get_slabs(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            WHERE c.category IN ['IfcSlab', 'IfcPlate']
            RETURN c
        """, project_id=project_id)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get all MEP components in a project ---
def get_mep_components(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            WHERE c.is_mep = true
            RETURN c
            ORDER BY c.mep_system_type
        """, project_id=project_id)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get all structural components in a project ---
def get_structural_components(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            WHERE c.is_structural = true
            RETURN c
            ORDER BY c.category
        """, project_id=project_id)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get all components on a specific floor ---
def get_components_on_floor(project_id, floor_name):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})-[:ON_FLOOR]->(f:Floor {name: $floor_name})
            RETURN c
        """, project_id=project_id, floor_name=floor_name)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get all floors in a project ---
def get_floors(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (f:Floor {project_id: $project_id})
            RETURN f
            ORDER BY f.elevation
        """, project_id=project_id)
        data = [dict(record["f"]) for record in result]
    driver.close()
    return data

# --- Get all spaces in a project ---
def get_spaces(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (s:Space {project_id: $project_id})
            RETURN s
            ORDER BY s.level, s.name
        """, project_id=project_id)
        data = [dict(record["s"]) for record in result]
    driver.close()
    return data

# --- Get all components a component connects to ---
def get_connections(component_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component {component_id: $component_id})-[r:CONNECTS_TO]->(b:Component)
            RETURN b, r.angle as angle, r.distance as distance, r.source as source
        """, component_id=component_id)
        data = [
            {
                "component": dict(record["b"]),
                "angle": record["angle"],
                "distance": record["distance"],
                "source": record["source"]
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get everything embedded in a wall ---
def get_wall_openings(component_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component)-[:FILLS]->(w:Component {component_id: $component_id})
            RETURN a
        """, component_id=component_id)
        data = [dict(record["a"]) for record in result]
    driver.close()
    return data

# --- Get MEP flow network for a system type ---
def get_mep_flow_network(project_id, mep_system_type):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component {project_id: $project_id, mep_system_type: $mep_system_type})
                  -[r:FLOWS_INTO]->
                  (b:Component {project_id: $project_id})
            RETURN a, b, r.source as source
        """, project_id=project_id, mep_system_type=mep_system_type)
        data = [
            {
                "from": dict(record["a"]),
                "to": dict(record["b"]),
                "source": record["source"]
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get all MEP penetrations through walls and slabs ---
def get_wall_penetrations(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (mep:Component {project_id: $project_id})-[:PENETRATES]->(w:Component)
            WHERE w.category IN ['IfcWall', 'IfcWallStandardCase', 'IfcSlab', 'IfcPlate']
            RETURN mep, w
        """, project_id=project_id)
        data = [
            {
                "mep": dict(record["mep"]),
                "wall": dict(record["w"])
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get components that bound a space ---
def get_space_boundaries(project_id, space_name):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})-[:BOUNDS]->(s:Space {name: $space_name})
            RETURN c
        """, project_id=project_id, space_name=space_name)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get components contained in a space ---
def get_space_contents(project_id, space_name):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (s:Space {project_id: $project_id, name: $space_name})-[:CONTAINS]->(c:Component)
            RETURN c
        """, project_id=project_id, space_name=space_name)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get full building structure ---
def get_building_structure(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (b:Building {project_id: $project_id})
            MATCH (f:Floor)-[:PART_OF]->(b)
            MATCH (c:Component)-[:ON_FLOOR]->(f)
            RETURN b, f, c
            ORDER BY f.elevation, c.category
        """, project_id=project_id)
        data = [
            {
                "building": dict(record["b"]),
                "floor": dict(record["f"]),
                "component": dict(record["c"])
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get full reconstruction data ---
def get_reconstruction_data(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            OPTIONAL MATCH (c)-[r]->(other:Component)
            RETURN c, type(r) as relationship_type, other
            ORDER BY c.pos_z, c.category
        """, project_id=project_id)
        data = [
            {
                "component": dict(record["c"]),
                "relationship": record["relationship_type"],
                "connected_to": dict(record["other"]) if record["other"] else None
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get high quality components ---
def get_high_quality(project_id, min_score=0.8):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            WHERE c.quality_score >= $min_score
            RETURN c
            ORDER BY c.quality_score DESC
        """, project_id=project_id, min_score=min_score)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data

# --- Get components adjacent to a specific component ---
def get_adjacent_components(component_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component {component_id: $component_id})-[r:ADJACENT_TO]->(b:Component)
            RETURN b, r.distance as distance
        """, component_id=component_id)
        data = [
            {
                "component": dict(record["b"]),
                "distance": record["distance"]
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get all MEP systems in a project ---
def get_mep_systems(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})-[:ASSIGNED_TO]->(s:System)
            RETURN s.name as system_name, s.system_type as system_type, collect(c) as components
        """, project_id=project_id)
        data = [
            {
                "system_name": record["system_name"],
                "system_type": record["system_type"],
                "components": [dict(c) for c in record["components"]]
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get all relationships for a component ---
def get_all_component_relationships(component_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component {component_id: $component_id})-[r]->(b)
            RETURN type(r) as relationship_type, b, r
        """, component_id=component_id)
        data = [
            {
                "relationship_type": record["relationship_type"],
                "connected_to": dict(record["b"]),
                "properties": dict(record["r"])
            }
            for record in result
        ]
    driver.close()
    return data

# --- Get components by normalized category ---
def get_by_normalized_category(project_id, normalized_category):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id, normalized_category: $category})
            RETURN c
        """, project_id=project_id, category=normalized_category)
        data = [dict(record["c"]) for record in result]
    driver.close()
    return data
