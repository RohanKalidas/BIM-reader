# BIM Component Stripper

Ingests IFC files (exported from Revit or any BIM software) and extracts all building components into a PostgreSQL database. Uses AI to enrich, clean, and analyze the extracted data.

## What it does

1. Parses an IFC file and extracts all building components (walls, slabs, roofs, MEP systems, materials, etc.)
2. Stores everything in a PostgreSQL database with full parameters
3. Uses Claude AI to enrich each component with descriptions, quality scores, duplicate detection, and missing data flags
4. Calculates missing dimensions using math and context-based estimation
5. Detects duplicate components across buildings
6. Flags missing data and assigns quality scores to every component
7. Organizes components into normalized categories for easy querying

## Stack

- Python 3
- ifcopenshell (IFC parsing)
- PostgreSQL (database)
- Claude AI via Anthropic API (enrichment)

## Prerequisites

Before you start make sure you have:

- Python 3 installed — [python.org](https://python.org)
- PostgreSQL installed — [postgresql.org/download](https://postgresql.org/download)
- pgAdmin installed — comes with PostgreSQL or [pgadmin.org](https://pgadmin.org)
- An Anthropic API key — [console.anthropic.com](https://console.anthropic.com)
- An IFC file to test with — export from Revit or download a sample from [buildingSMART](https://github.com/buildingSMART/Sample-Test-Files)

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

### 3. Set up the database

Open pgAdmin and:
1. Right click **Databases** → **Create** → **Database**
2. Name it `bim_components`
3. Click on `bim_components` in the sidebar
4. Open the **Query Tool** (lightning bolt icon)
5. Paste the contents of `database/schema.sql` and hit play

### 4. Create your .env file

Create a file called `.env` in the root of the project:
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=bim_components
DB_USER=postgres
DB_PASSWORD=your_postgres_password
ANTHROPIC_API_KEY=your_anthropic_api_key
```

### 5. Run the pipeline

**Step 1 — Extract components from an IFC file:**
```bash
python3 extractor/strip.py path/to/your/file.ifc
```

**Step 2 — Enrich components with AI:**
```bash
python3 extractor/enricher.py
```

**Step 3 — Populate dimension columns:**
```bash
python3 extractor/populate_dimensions.py
```

## Project Structure
```
bim-component-stripper/
├── README.md
├── requirements.txt
├── .env.example
├── database/
│   ├── schema.sql
│   └── db.py
└── extractor/
    ├── strip.py
    ├── enricher.py
    └── populate_dimensions.py
```

## Database Schema

| Table | Description |
|---|---|
| `projects` | Tracks every IFC file processed |
| `components` | Every building element extracted |
| `wall_types` | Detailed wall layer data |
| `mep_systems` | MEP connector and flow data |
| `materials` | All materials referenced in the building |

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

-- See wall types and their layers
SELECT c.family_name, w.total_thickness, w.function, w.layers
FROM wall_types w
JOIN components c ON c.id = w.component_id;

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
WHERE id = 26;
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

- IFC files are ignored by git (see .gitignore) — don't commit large building files
- Never commit your .env file — it contains your database password and API key
- The Anthropic API costs a small amount per run — enriching 15 components costs fractions of a cent. However, Anthropic provides you with credits which should last for more than necessary.
