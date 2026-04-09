-- BIM Component Stripper
-- PostgreSQL Schema

-- Every .rvt file that gets uploaded and processed
CREATE TABLE projects (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    uploaded_at     TIMESTAMP DEFAULT NOW(),
    processed_at    TIMESTAMP,
    status          TEXT DEFAULT 'pending'  -- pending | processing | done | failed
);

-- The core table. Every extracted component from every project lives here.
CREATE TABLE components (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    
    -- What kind of thing is this?
    category        TEXT NOT NULL,   -- 'wall' | 'door' | 'window' | 'duct' | 'pipe' | 'floor' | etc.
    family_name     TEXT,            -- Revit family name e.g. "Basic Wall"
    type_name       TEXT,            -- Revit type name e.g. "Exterior - Brick on CMU"
    revit_id        TEXT,            -- original element ID inside the .rvt file
    
    -- All parameters as flexible JSON
    parameters      JSONB DEFAULT '{}',
    
    -- Extracted dimension columns
    width_mm        FLOAT,
    height_mm       FLOAT,
    length_mm       FLOAT,
    area_m2         FLOAT,
    volume_m3       FLOAT,
    quality_score   FLOAT,

    created_at      TIMESTAMP DEFAULT NOW()
);

-- Spatial data for every component — position, rotation, bounding box
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
    -- bounding_box example:
    -- {"min_x": 0, "min_y": 0, "min_z": 0, "max_x": 6000, "max_y": 200, "max_z": 3000}
    level           TEXT,            -- which floor/storey this component is on
    elevation       FLOAT            -- elevation in mm from ground
);

-- Wall types get their own table since they have a specific layered structure
CREATE TABLE wall_types (
    id              SERIAL PRIMARY KEY,
    component_id    INTEGER REFERENCES components(id) ON DELETE CASCADE,
    total_thickness FLOAT,           -- in millimeters
    function        TEXT,            -- 'exterior' | 'interior' | 'retaining' | etc.
    layers          JSONB DEFAULT '[]'
    -- layers example:
    -- [
    --   {"name": "Brick", "material": "Masonry - Brick", "thickness": 92},
    --   {"name": "Insulation", "material": "Insulation - Rigid", "thickness": 50},
    --   {"name": "Concrete Block", "material": "Concrete - CMU", "thickness": 190}
    -- ]
);

-- MEP systems (mechanical, electrical, plumbing) have connector info
CREATE TABLE mep_systems (
    id              SERIAL PRIMARY KEY,
    component_id    INTEGER REFERENCES components(id) ON DELETE CASCADE,
    system_type     TEXT,            -- 'supply air' | 'return air' | 'domestic hot water' | etc.
    flow_rate       FLOAT,
    pressure_drop   FLOAT,
    connectors      JSONB DEFAULT '[]'
    -- connectors example:
    -- [
    --   {"type": "supply", "size": 12, "shape": "round"},
    --   {"type": "return", "size": 10, "shape": "round"}
    -- ]
);

-- Materials referenced by components
CREATE TABLE materials (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    category        TEXT,            -- 'concrete' | 'masonry' | 'wood' | 'metal' | etc.
    properties      JSONB DEFAULT '{}'
    -- properties example:
    -- {
    --   "thermal_conductivity": 0.72,
    --   "density": 2400,
    --   "color": "#C8B89A"
    -- }
);

-- Indexes for the queries you'll run most often
CREATE INDEX idx_components_project    ON components(project_id);
CREATE INDEX idx_components_category   ON components(category);
CREATE INDEX idx_components_family     ON components(family_name);
CREATE INDEX idx_components_parameters ON components USING GIN(parameters);
CREATE INDEX idx_materials_project     ON materials(project_id);
CREATE INDEX idx_spatial_component     ON spatial_data(component_id);
