-- BIM Component Stripper
-- PostgreSQL Schema

-- Every IFC file that gets uploaded and processed
CREATE TABLE projects (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    uploaded_at     TIMESTAMP DEFAULT NOW(),
    processed_at    TIMESTAMP,
    status          TEXT DEFAULT 'pending'
);

-- The core table. Every extracted component from every project lives here.
CREATE TABLE components (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    category        TEXT NOT NULL,
    family_name     TEXT,
    type_name       TEXT,
    revit_id        TEXT,
    parameters      JSONB DEFAULT '{}',
    width_mm        FLOAT,
    height_mm       FLOAT,
    length_mm       FLOAT,
    area_m2         FLOAT,
    volume_m3       FLOAT,
    quality_score   FLOAT,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Spatial data for every component
CREATE TABLE spatial_data (
    id              SERIAL PRIMARY KEY,
    component_id    INTEGER REFERENCES components(id) ON DELETE CASCADE,
    pos_x           FLOAT,
    pos_y           FLOAT,
    pos_z           FLOAT,
    rot_x           FLOAT,
    rot_y           FLOAT,
    rot_z           FLOAT,
    bounding_box    JSONB DEFAULT '{}',
    level           TEXT,
    elevation       FLOAT
);

-- Explicit relationships between components extracted directly from IFC
CREATE TABLE relationships (
    id                  SERIAL PRIMARY KEY,
    project_id          INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    component_a_id      INTEGER REFERENCES components(id) ON DELETE CASCADE,
    component_b_id      INTEGER REFERENCES components(id) ON DELETE CASCADE,
    relationship_type   TEXT NOT NULL,
    -- Types:
    -- CONNECTS_TO       — physical connection between elements
    -- FILLS             — door/window fills a wall opening
    -- VOIDS             — opening cuts through a wall/slab
    -- BOUNDS            — element bounds a space/room
    -- CONTAINS          — space contains an element
    -- FLOWS_INTO        — MEP flow connection
    -- PART_OF           — element is part of a compound element
    -- COVERED_BY        — element has a covering
    -- ASSIGNED_TO       — element assigned to a system
    properties          JSONB DEFAULT '{}',
    source              TEXT DEFAULT 'explicit',
    -- explicit = read directly from IFC
    -- calculated = derived from geometry
    created_at          TIMESTAMP DEFAULT NOW()
);

-- Spaces and rooms
CREATE TABLE spaces (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    revit_id        TEXT,
    name            TEXT,
    long_name       TEXT,
    level           TEXT,
    elevation       FLOAT,
    area_m2         FLOAT,
    volume_m3       FLOAT,
    parameters      JSONB DEFAULT '{}'
);

-- Wall types
CREATE TABLE wall_types (
    id              SERIAL PRIMARY KEY,
    component_id    INTEGER REFERENCES components(id) ON DELETE CASCADE,
    total_thickness FLOAT,
    function        TEXT,
    layers          JSONB DEFAULT '[]'
);

-- MEP systems
CREATE TABLE mep_systems (
    id              SERIAL PRIMARY KEY,
    component_id    INTEGER REFERENCES components(id) ON DELETE CASCADE,
    system_type     TEXT,
    system_name     TEXT,
    flow_rate       FLOAT,
    pressure_drop   FLOAT,
    connectors      JSONB DEFAULT '[]'
);

-- Materials
CREATE TABLE materials (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    category        TEXT,
    properties      JSONB DEFAULT '{}'
);

-- Indexes
CREATE INDEX idx_components_project     ON components(project_id);
CREATE INDEX idx_components_category    ON components(category);
CREATE INDEX idx_components_family      ON components(family_name);
CREATE INDEX idx_components_parameters  ON components USING GIN(parameters);
CREATE INDEX idx_spatial_component      ON spatial_data(component_id);
CREATE INDEX idx_relationships_project  ON relationships(project_id);
CREATE INDEX idx_relationships_a        ON relationships(component_a_id);
CREATE INDEX idx_relationships_b        ON relationships(component_b_id);
CREATE INDEX idx_relationships_type     ON relationships(relationship_type);
CREATE INDEX idx_spaces_project         ON spaces(project_id);
CREATE INDEX idx_materials_project      ON materials(project_id);
