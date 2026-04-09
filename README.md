# BIM Component Stripper

Ingests IFC files (exported from Revit or any BIM software), extracts all building components into a PostgreSQL database, builds a spatial relationship graph in Neo4j, and uses AI to enrich and organize the data. The goal is to strip a building into reusable lego blocks and store enough information to reconstruct it identically.

## What it does

1. Parses an IFC file and extracts all building components (walls, slabs, roofs, MEP systems, materials, etc.)
2. Captures spatial data — position, rotation, bounding box, and floor level for every component
3. Extracts explicit relationships directly from the IFC file (connections, fills, flow networks, space boundaries)
4. Stores everything in PostgreSQL with full parameters
5. Builds a Neo4j graph of nodes and relationships for spatial reasoning and reconstruction
6. Calculates missing dimensions using math and context-based estimation
7. Runs a spatial analyzer to calculate additional relationships from geometry
8. Optional AI enrichment for ambiguous components

## Stack

- Python 3
- ifcopenshell (IFC parsing)
- PostgreSQL (component metadata and parameters)
- Neo4j (spatial relationships and graph data)
- Claude AI via Anthropic API (optional enrichment)
- numpy (spatial math)

## Prerequisites

Before you start make sure you have:

- Python 3 installed — [python.org](https://python.org)
- PostgreSQL installed — [postgresql.org/download](https://postgresql.org/download)
- pgAdmin installed — comes with PostgreSQL or [pgadmin.org](https://pgadmin.org)
- Neo4j Desktop installed — [neo4j.com/download](https://neo4j.com/download)
- An IFC file to test with — export from Revit or download a sample from [buildingSMART](https://github.com/youshengCode/IfcSampleFiles)
- An Anthropic API key (optional, only needed for enricher) — [console.anthropic.com](https://console.anthropic.com)

## OS Notes

**Mac/Linux:** Use `python3` and `pip3` for all commands

**Windows:** Replace `python3` with `python` and `pip3` with `pip` in all commands.
File paths use backslashes e.g. `extractor\strip.py`

**Linux:** Install PostgreSQL via your package manager:
```bash
sudo apt install postgresql postgresql-contrib
```

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/yourusername/your-repo-name.git
cd your-repo-name
```

### 2. Install Python dependencies
```bash
pip3 install -r requirements.txt
```

### 3. Set up PostgreSQL

Open pgAdmin and:
1. Right click **Databases** → **Create** → **Database**
2. Name it `bim_components`
3. Click on `bim_components` in the left sidebar to select it
4. Open the **Query Tool** (lightning bolt icon at the top)
5. Make sure the top of the query tool says `bim_components` not `postgres`
6. Paste the entire contents of `database/schema.sql` and hit play
7. You should see `CREATE INDEX` and `Query returned successfully`

### 4. Set up Neo4j

1. Open Neo4j Desktop
2. Click **Create instance**
3. Give it a name like `bim-graph`
4. Set a password — write it down, you'll need it
5. Click **Create**
6. Click **Start** and wait until it says **Running**
7. You can view your graph at `http://localhost:7474` in any browser

### 5. Create your .env file

Create a file called `.env` in the root of the project. This file is never pushed to GitHub — you must create it manually on every machine:
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=bim_components
DB_USER=postgres
DB_PASSWORD=your_postgres_password
ANTHROPIC_API_KEY=your_anthropic_api_key
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_neo4j_password
```

Replace `your_postgres_password`, `your_anthropic_api_key`, and `your_neo4j_password` with your actual credentials. Do not add spaces around the `=` sign. `ANTHROPIC_API_KEY` is only needed if you plan to run the optional enricher.

### 6. Run the pipeline

Make sure Neo4j is running before starting. Then run:
```bash
python3 run.py path/to/your/file.ifc
```

That's it. The pipeline runs all steps automatically in order.

**If something fails midway**, you can resume from a specific step without re-running everything:
```bash
# Resume from graph builder (step 2)
python3 run.py --from 2

# Resume from dimension population (step 3)
python3 run.py --from 3

# Resume from spatial analysis (step 4)
python3 run.py --from 4
```

### 7. Optional — AI enrichment

The enricher is optional and only runs on ambiguous components that IFC doesn't clearly identify. Run it separately after the main pipeline:
```bash
python3 extractor/enricher.py
```

This requires an Anthropic API key in your `.env` file.

### 8. Verify in Neo4j browser

Open `http://localhost:7474` in your browser and log in with `neo4j` and your password. Run this query to see your full graph:
```cypher
MATCH (n) RETURN n
```

To see all relationships:
```cypher
MATCH (a)-[r]->(b) RETURN a, r, b
```

## Project Structure
```
bim-component-stripper/
├── README.md
├── requirements.txt
├── run.py                      ← run this to process an IFC file
├── .env.example
├── database/
│   ├── schema.sql
│   ├── db.py
│   └── graph_queries.py
└── extractor/
    ├── strip.py
    ├── graph_builder.py
    ├── populate_dimensions.py
    ├── spatial_analyzer.py
    └── enricher.py             ← optional, run separately
```

## Database Schema

### PostgreSQL

| Table | Description |
|---|---|
| `projects` | Tracks every IFC file processed |
| `components` | Every building element extracted |
| `spatial_data` | Position, rotation, and bounding box for every component |
| `relationships` | Explicit relationships extracted directly from IFC |
| `spaces` | Rooms and spaces from the building |
| `wall_types` | Detailed wall layer data |
| `mep_systems` | MEP connector and flow data |
| `materials` | All materials referenced in the building |

### Neo4j

| Node | Description |
|---|---|
| `Component` | Every building element as a graph node |
| `Space` | Rooms and zones |
| `Floor` | Building levels |
| `Building` | Top level container |
| `System` | MEP systems |

| Relationship | Description |
|---|---|
| `CONNECTS_TO` | Physical connection between components |
| `FILLS` | Door/window inside a wall opening |
| `VOIDS` | Opening cut into a wall or slab |
| `BOUNDS` | Component bounds a space |
| `CONTAINS` | Space contains a component |
| `FLOWS_INTO` | MEP flow connection |
| `PART_OF` | Component is part of a compound element |
| `ASSIGNED_TO` | Component assigned to an MEP system |
| `COVERED_BY` | Element has a covering |
| `ON_FLOOR` | Component is on a floor level |
| `SITS_ON` | Slab or roof sits on walls |
| `SUPPORTED_BY` | Structural load relationship |
| `ADJACENT_TO` | Components near each other |
| `PENETRATES` | MEP element goes through a wall or slab |
| `RUNS_ALONG` | MEP element runs alongside a wall |

## Useful pgAdmin Queries
```sql
-- See all projects processed
SELECT * FROM projects;

-- See all components
SELECT * FROM components;

-- See components with dimensions
SELECT id, family_name, category, width_mm, height_mm, length_mm, area_m2, volume_m3, quality_score
FROM components
ORDER BY quality_score DESC;

-- See spatial data for all components
SELECT c.family_name, s.pos_x, s.pos_y, s.pos_z, s.rot_z, s.level
FROM spatial_data s
JOIN components c ON c.id = s.component_id;

-- See all explicit relationships
SELECT c1.family_name as from_component, r.relationship_type, c2.family_name as to_component, r.properties
FROM relationships r
JOIN components c1 ON c1.id = r.component_a_id
JOIN components c2 ON c2.id = r.component_b_id;

-- See wall types and their layers
SELECT c.family_name, w.total_thickness, w.function, w.layers
FROM wall_types w
JOIN components c ON c.id = w.component_id;

-- See all spaces
SELECT * FROM spaces;

-- See all materials
SELECT * FROM materials;

-- Find components by category
SELECT family_name, category, quality_score
FROM components
WHERE category = 'IfcWall';

-- Find high quality components only
SELECT family_name, category, quality_score
FROM components
WHERE quality_score >= 0.8
ORDER BY quality_score DESC;

-- See AI enrichment for a specific component
SELECT family_name, parameters->'ai_enrichment'
FROM components
WHERE id = 1;
```

## Useful Neo4j Queries
```cypher
-- See all nodes
MATCH (n) RETURN n

-- See all relationships
MATCH (a)-[r]->(b) RETURN a, r, b

-- See all walls and what they connect to
MATCH (w:Component)-[r]->(b)
WHERE w.category IN ['IfcWall', 'IfcWallStandardCase']
RETURN w.family_name, type(r), b.family_name

-- See the full building structure
MATCH (b:Building)--(f:Floor)--(c:Component)
RETURN b, f, c

-- See MEP flow network
MATCH (a:Component)-[:FLOWS_INTO]->(b:Component)
RETURN a.family_name, b.family_name

-- Find all components on a specific floor
MATCH (c:Component)-[:ON_FLOOR]->(f:Floor)
RETURN c.family_name, c.category, f.name

-- Find what a wall connects to
MATCH (w:Component)-[r]->(b)
WHERE w.category IN ['IfcWall', 'IfcWallStandardCase']
RETURN w.family_name, type(r), b.family_name
```

## Example Queries
```sql
-- Find all walls longer than 3 meters
SELECT family_name, length_mm, width_mm
FROM components
WHERE category = 'IfcWall' AND length_mm > 3000;

-- Find high quality components
SELECT family_name, quality_score
FROM components
WHERE quality_score >= 0.8
ORDER BY quality_score DESC;

-- Find all exterior walls
SELECT family_name, width_mm, length_mm, height_mm
FROM components
WHERE category = 'IfcWall'
AND parameters->'Pset_WallCommon'->>'IsExternal' = 'true';
```

## Notes

- IFC files are ignored by git — don't commit large building files
- Never commit your `.env` file — it contains your database password and API key
- The `.env` file must be created manually on every machine — it is never pushed to GitHub
- Neo4j Desktop must be running before executing `run.py`
- Running the pipeline on a new IFC file creates a new project — old data is never overwritten
- To wipe and restart: delete the project from pgAdmin and re-run the pipeline
