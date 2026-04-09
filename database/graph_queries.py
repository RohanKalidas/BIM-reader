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
        return [record["c"] for record in result]
    driver.close()

# --- Get all walls in a project ---
def get_walls(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            WHERE c.normalized_category = 'wall'
            RETURN c
            ORDER BY c.length_mm DESC
        """, project_id=project_id)
        return [record["c"] for record in result]
    driver.close()

# --- Get all components on a specific floor ---
def get_components_on_floor(project_id, floor_name):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})-[:ON_FLOOR]->(f:Floor {name: $floor_name})
            RETURN c
        """, project_id=project_id, floor_name=floor_name)
        return [record["c"] for record in result]
    driver.close()

# --- Get all components a wall connects to ---
def get_wall_connections(component_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component {component_id: $component_id})-[r:CONNECTS_TO]->(b:Component)
            RETURN b, r.angle as angle, r.distance as distance
        """, component_id=component_id)
        return [{"component": record["b"], "angle": record["angle"], "distance": record["distance"]} for record in result]
    driver.close()

# --- Get everything embedded in a wall ---
def get_wall_openings(component_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component)-[:EMBEDDED_IN]->(w:Component {component_id: $component_id})
            RETURN a
        """, component_id=component_id)
        return [record["a"] for record in result]
    driver.close()

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
        return [record["c"] for record in result]
    driver.close()

# --- Get MEP flow network for a system type ---
def get_mep_flow_network(project_id, mep_system_type):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (a:Component {project_id: $project_id, mep_system_type: $mep_system_type})
                  -[r:FLOWS_INTO]->
                  (b:Component {project_id: $project_id, mep_system_type: $mep_system_type})
            RETURN a, b
        """, project_id=project_id, mep_system_type=mep_system_type)
        return [{"from": record["a"], "to": record["b"]} for record in result]
    driver.close()

# --- Get all components that penetrate walls ---
def get_wall_penetrations(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (mep:Component {project_id: $project_id})-[:PENETRATES]->(w:Component {normalized_category: 'wall'})
            RETURN mep, w
        """, project_id=project_id)
        return [{"mep": record["mep"], "wall": record["w"]} for record in result]
    driver.close()

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
        return [{"building": record["b"], "floor": record["f"], "component": record["c"]} for record in result]
    driver.close()

# --- Get reconstruction data for a project ---
def get_reconstruction_data(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id})
            OPTIONAL MATCH (c)-[r]->(other:Component)
            RETURN c, type(r) as relationship_type, other
            ORDER BY c.pos_z, c.category
        """, project_id=project_id)
        return [
            {
                "component": record["c"],
                "relationship": record["relationship_type"],
                "connected_to": record["other"]
            }
            for record in result
        ]
    driver.close()

# --- Get components by normalized category ---
def get_by_category(project_id, normalized_category):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (c:Component {project_id: $project_id, normalized_category: $category})
            RETURN c
        """, project_id=project_id, category=normalized_category)
        return [record["c"] for record in result]
    driver.close()

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
        return [record["c"] for record in result]
    driver.close()

# --- Get all floors in a project ---
def get_floors(project_id):
    driver = get_neo4j()
    with driver.session() as session:
        result = session.run("""
            MATCH (f:Floor {project_id: $project_id})
            RETURN f
            ORDER BY f.elevation
        """, project_id=project_id)
        return [record["f"] for record in result]
    driver.close()
