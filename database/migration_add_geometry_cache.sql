-- Run once on existing databases (new installs get these from schema.sql).

ALTER TABLE projects ADD COLUMN IF NOT EXISTS ifc_schema TEXT DEFAULT 'IFC4';
UPDATE projects SET ifc_schema = 'IFC4' WHERE ifc_schema IS NULL;

ALTER TABLE components ADD COLUMN IF NOT EXISTS has_geometry BOOLEAN DEFAULT FALSE;
UPDATE components SET has_geometry = FALSE WHERE has_geometry IS NULL;

CREATE INDEX IF NOT EXISTS idx_components_has_geometry ON components(has_geometry) WHERE has_geometry = TRUE;
