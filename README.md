# BIM Component Stripper

Ingests Revit (.rvt) files and extracts reusable parametric components
into a PostgreSQL database — families, wall types, MEP systems, and materials.

Part of a larger generative architecture pipeline.

## Stack
- Python
- ifcopenshell (IFC parsing)
- PostgreSQL

## Setup
1. Install PostgreSQL and create a database called `bim_components`
2. Run `psql -d bim_components -f database/schema.sql`
3. Install dependencies: `pip install -r requirements.txt`
4. Run the extractor: `python extractor/strip.py path/to/file.rvt`
