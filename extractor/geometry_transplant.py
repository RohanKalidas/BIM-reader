"""
extractor/geometry_transplant.py — Transplants real IFC geometry from library components.

Given a fixture name and category (e.g. "Toilet", "IfcSanitaryTerminal"), this module:
1. Queries the database for matching components
2. Opens the source IFC file that component came from
3. Deep-copies all geometry entities (representations, profiles, materials) into the target model
4. Returns the transplanted representation, ready to attach to a new element

Caches opened IFC files and resolved geometry so repeated lookups are fast.
"""

import os
import logging
import math
import psycopg2.extras
import ifcopenshell

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")

# Search synonyms — same as server.py so we get consistent matches
SEARCH_SYNONYMS = {
    "toilet":      ["toilet","wc","water closet","sanitary","lavatory","commode"],
    "sink":        ["sink","basin","washbasin","lavatory","wash hand"],
    "shower":      ["shower","bath","tub","shower tray"],
    "bath":        ["bath","bathtub","tub"],
    "fridge":      ["fridge","refrigerator","refrigeration"],
    "refrigerator":["refrigerator","fridge"],
    "sofa":        ["sofa","couch","settee","lounge chair"],
    "couch":       ["couch","sofa","settee"],
    "table":       ["table","desk","worktop","counter"],
    "chair":       ["chair","seat","stool"],
    "bed":         ["bed","bunk","mattress"],
    "wardrobe":    ["wardrobe","closet","cupboard","cabinet","armoire"],
    "door":        ["door","entry","entrance"],
    "window":      ["window","glazing","glass"],
    "light":       ["light","lamp","luminaire","fixture","downlight"],
    "stove":       ["stove","oven","cooker","hob","range"],
    "washer":      ["washer","washing machine","laundry"],
    "nightstand":  ["nightstand","night stand","bedside","side table"],
    "tv":          ["tv","television","tv unit","tv stand","media"],
    "coffee table":["coffee table","center table","cocktail table"],
    "dining table":["dining table","dining"],
    "desk":        ["desk","work table","writing table"],
    "water heater":["water heater","boiler","hot water","geyser"],
    "cabinet":     ["cabinet","cupboard","storage","lower cabinet","upper cabinet"],
    "counter":     ["counter","countertop","worktop","benchtop"],
}


class GeometryLibrary:
    """
    Manages geometry transplantation from source IFC files into generated models.
    
    Caches:
    - _ifc_cache: {filepath: ifcopenshell.file} — opened IFC files
    - _match_cache: {(name, category): component_row} — database lookup results
    - _entity_map: {source_entity_id: target_entity} — prevents duplicate copies
    """

    def __init__(self, db_connection_func, upload_folder=None):
        """
        Args:
            db_connection_func: callable that returns (conn, cursor) context manager
            upload_folder: path to uploaded IFC files
        """
        self._get_db = db_connection_func
        self._upload_folder = upload_folder or UPLOAD_FOLDER
        self._ifc_cache = {}        # filepath -> ifcopenshell model
        self._match_cache = {}      # (search_key) -> component row
        self._entity_map = {}       # (source_file, source_entity_id) -> target entity
        self._component_index = None  # lazy-loaded list of all available components

    def _load_component_index(self):
        """Load all components with their source file info for matching."""
        if self._component_index is not None:
            return
        
        with self._get_db(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
            cursor.execute("""
                SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
                       c.width_mm, c.height_mm, c.length_mm, c.quality_score,
                       c.parameters->>'_material' as material,
                       p.filename
                FROM components c
                JOIN projects p ON p.id = c.project_id
                WHERE p.status = 'done'
                  AND c.revit_id IS NOT NULL
                ORDER BY c.quality_score DESC NULLS LAST
            """)
            self._component_index = cursor.fetchall()
        
        logger.info("Loaded %d components into geometry library", len(self._component_index))

    def _open_ifc(self, filename):
        """Open an IFC file, with caching."""
        if filename in self._ifc_cache:
            return self._ifc_cache[filename]
        
        filepath = os.path.join(self._upload_folder, filename)
        if not os.path.exists(filepath):
            logger.warning("Source IFC file not found: %s", filepath)
            return None
        
        try:
            model = ifcopenshell.open(filepath)
            self._ifc_cache[filename] = model
            logger.info("Opened source IFC: %s (%s)", filename, model.schema)
            return model
        except Exception as e:
            logger.error("Failed to open IFC %s: %s", filename, e)
            return None

    def find_component(self, name, category=None, target_w=None, target_d=None, target_h=None):
        """
        Find the best matching component from the database.
        
        Args:
            name: fixture name to search for (e.g. "Toilet", "Sofa", "Door")
            category: IFC category to prefer (e.g. "IfcSanitaryTerminal")
            target_w/d/h: desired dimensions in metres (for size matching)
        
        Returns:
            dict with component row data, or None
        """
        cache_key = (name.lower(), category)
        if cache_key in self._match_cache:
            return self._match_cache[cache_key]
        
        self._load_component_index()
        
        # Build search terms
        name_lower = name.lower()
        terms = [name_lower]
        
        # Expand with synonyms
        for key, syns in SEARCH_SYNONYMS.items():
            if name_lower in syns or name_lower == key:
                terms.extend(syns)
                if key not in terms:
                    terms.append(key)
        terms = list(set(terms))
        
        # Score each component
        best = None
        best_score = -1
        
        for comp in self._component_index:
            score = 0
            comp_name = (comp["family_name"] or "").lower()
            comp_type = (comp["type_name"] or "").lower()
            comp_cat  = (comp["category"] or "").lower()
            
            # Name matching
            name_matched = False
            for term in terms:
                if term in comp_name or term in comp_type:
                    score += 10
                    name_matched = True
                    # Bonus for exact match
                    if term == comp_name or term == comp_type:
                        score += 5
                    break
            
            if not name_matched:
                # Also check category as fallback
                for term in terms:
                    if term in comp_cat:
                        score += 3
                        name_matched = True
                        break
            
            if not name_matched:
                continue
            
            # Category matching bonus
            if category and comp["category"] == category:
                score += 8
            
            # Quality score bonus
            if comp.get("quality_score"):
                score += comp["quality_score"] * 3
            
            # Size similarity bonus (if target dimensions given)
            if target_w and comp.get("width_mm"):
                ratio = min(target_w * 1000, comp["width_mm"]) / max(target_w * 1000, comp["width_mm"])
                score += ratio * 2
            if target_h and comp.get("height_mm"):
                ratio = min(target_h * 1000, comp["height_mm"]) / max(target_h * 1000, comp["height_mm"])
                score += ratio * 2
            
            # Check that source file exists
            filepath = os.path.join(self._upload_folder, comp["filename"])
            if not os.path.exists(filepath):
                continue
            
            if score > best_score:
                best_score = score
                best = comp
        
        self._match_cache[cache_key] = best
        if best:
            logger.info("Matched '%s' -> %s [%s] from %s (score=%.1f)",
                        name, best["family_name"], best["category"], best["filename"], best_score)
        else:
            logger.debug("No match for '%s' (category=%s)", name, category)
        
        return best

    def transplant_geometry(self, target_model, source_component, body_ctx):
        """
        Deep-copy the geometry (IfcRepresentation) from a source component
        into the target model.
        
        Args:
            target_model: the ifcopenshell.file being built
            source_component: component row dict from find_component()
            body_ctx: the body representation sub-context of the target model
        
        Returns:
            IfcProductDefinitionShape in the target model, or None on failure
        """
        if not source_component:
            return None
        
        source_model = self._open_ifc(source_component["filename"])
        if not source_model:
            return None
        
        # Find the element in the source model by GlobalId
        revit_id = source_component["revit_id"]
        source_element = None
        try:
            source_element = source_model.by_guid(revit_id)
        except Exception:
            # Fallback: search by type
            try:
                for el in source_model.by_type(source_component["category"]):
                    if el.GlobalId == revit_id:
                        source_element = el
                        break
            except Exception:
                pass
        
        if not source_element:
            logger.warning("Could not find element %s in source file %s",
                          revit_id, source_component["filename"])
            return None
        
        if not source_element.Representation:
            logger.debug("Element %s has no representation", revit_id)
            return None
        
        # Deep-copy the representation into the target model
        try:
            return self._deep_copy_representation(
                target_model, source_model, source_element, body_ctx
            )
        except Exception as e:
            logger.warning("Geometry transplant failed for %s: %s",
                          source_component.get("family_name", revit_id), e)
            return None

    def _deep_copy_representation(self, target, source_model, source_element, body_ctx):
        """
        Recursively copy all geometry entities from source to target model.
        Uses ifcopenshell's add() to deep-copy entity trees.
        """
        source_rep = source_element.Representation
        
        # Collect all entities in the representation tree
        # ifcopenshell.file.add() does a deep copy of an entity and all its references
        copied_reps = []
        for rep in source_rep.Representations:
            try:
                # Deep copy the entire representation and all referenced entities
                copied_rep = target.add(rep)
                
                # Remap the context reference to our target model's context
                if copied_rep.ContextOfItems:
                    copied_rep.ContextOfItems = body_ctx
                
                copied_reps.append(copied_rep)
            except Exception as e:
                logger.debug("Failed to copy representation %s: %s",
                            rep.RepresentationIdentifier, e)
                continue
        
        if not copied_reps:
            return None
        
        # Create the ProductDefinitionShape wrapper
        prod_shape = target.createIfcProductDefinitionShape(None, None, copied_reps)
        return prod_shape

    def transplant_materials(self, target_model, oh, target_element, source_component):
        """
        Copy material associations from the source component to the target element.
        """
        if not source_component:
            return
        
        source_model = self._open_ifc(source_component["filename"])
        if not source_model:
            return
        
        revit_id = source_component["revit_id"]
        try:
            source_element = source_model.by_guid(revit_id)
        except Exception:
            return
        
        # Find material associations on the source element
        try:
            if not hasattr(source_element, 'HasAssociations'):
                return
            for rel in source_element.HasAssociations:
                if rel.is_a("IfcRelAssociatesMaterial"):
                    mat = rel.RelatingMaterial
                    copied_mat = target_model.add(mat)
                    target_model.createIfcRelAssociatesMaterial(
                        ifcopenshell.guid.new(), oh, None, None,
                        [target_element], copied_mat
                    )
                    return  # Only need the first material association
        except Exception as e:
            logger.debug("Material transplant failed for %s: %s", revit_id, e)

    def get_source_dimensions(self, source_component):
        """
        Get the real dimensions of a source component in metres.
        Returns (width_m, depth_m, height_m) or (None, None, None).
        """
        if not source_component:
            return None, None, None
        
        w = source_component.get("width_mm")
        h = source_component.get("height_mm")
        l = source_component.get("length_mm")
        
        return (
            w / 1000.0 if w else None,
            l / 1000.0 if l else None,  # length is often depth
            h / 1000.0 if h else None,
        )

    def clear_cache(self):
        """Clear all caches. Call between generation runs if needed."""
        self._ifc_cache.clear()
        self._match_cache.clear()
        self._entity_map.clear()
        self._component_index = None
