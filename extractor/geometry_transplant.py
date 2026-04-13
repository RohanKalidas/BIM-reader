"""
extractor/geometry_transplant.py — Transplants real IFC geometry from library components.

Key design decision: We extract geometry from the TYPE definition (IfcTypeProduct)
when available, not the instance. Type geometry is defined at local origin (0,0,0)
and is meant to be reused — exactly what we need for transplanting into new positions.

When no type representation exists, we fall back to the instance representation
but reset its coordinate system to origin.

Caches opened IFC files and resolved geometry so repeated lookups are fast.
"""

import os
import logging
import math
import psycopg2.extras
import ifcopenshell
import ifcopenshell.util.placement as placement_util

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")

# Search synonyms for fuzzy matching
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
    """

    def __init__(self, db_connection_func, upload_folder=None):
        self._get_db = db_connection_func
        self._upload_folder = upload_folder or UPLOAD_FOLDER
        self._ifc_cache = {}
        self._match_cache = {}
        self._rep_cache = {}       # (filename, revit_id) -> copied ProductDefinitionShape
        self._component_index = None

    def _load_component_index(self):
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
        if filename in self._ifc_cache:
            return self._ifc_cache[filename]

        filepath = os.path.join(self._upload_folder, filename)
        if not os.path.exists(filepath):
            logger.warning("Source IFC not found: %s", filepath)
            return None

        try:
            model = ifcopenshell.open(filepath)
            self._ifc_cache[filename] = model
            logger.info("Opened source IFC: %s", filename)
            return model
        except Exception as e:
            logger.error("Failed to open %s: %s", filename, e)
            return None

    def find_component(self, name, category=None, target_w=None, target_d=None, target_h=None):
        """Find best matching component from the database."""
        cache_key = (name.lower(), category)
        if cache_key in self._match_cache:
            return self._match_cache[cache_key]

        self._load_component_index()

        name_lower = name.lower()
        terms = [name_lower]
        for key, syns in SEARCH_SYNONYMS.items():
            if name_lower in syns or name_lower == key:
                terms.extend(syns)
                if key not in terms:
                    terms.append(key)
        terms = list(set(terms))

        best = None
        best_score = -1

        for comp in self._component_index:
            score = 0
            comp_name = (comp["family_name"] or "").lower()
            comp_type = (comp["type_name"] or "").lower()
            comp_cat  = (comp["category"] or "").lower()

            name_matched = False
            for term in terms:
                if term in comp_name or term in comp_type:
                    score += 10
                    if term == comp_name or term == comp_type:
                        score += 5
                    name_matched = True
                    break

            if not name_matched:
                for term in terms:
                    if term in comp_cat:
                        score += 3
                        name_matched = True
                        break

            if not name_matched:
                continue

            if category and comp["category"] == category:
                score += 8

            if comp.get("quality_score"):
                score += comp["quality_score"] * 3

            if target_w and comp.get("width_mm"):
                ratio = min(target_w * 1000, comp["width_mm"]) / max(target_w * 1000, comp["width_mm"], 1)
                score += ratio * 2
            if target_h and comp.get("height_mm"):
                ratio = min(target_h * 1000, comp["height_mm"]) / max(target_h * 1000, comp["height_mm"], 1)
                score += ratio * 2

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
        return best

    def transplant_geometry(self, target_model, source_component, body_ctx):
        """
        Deep-copy geometry from a source component into the target model.
        
        Strategy:
        1. Try to get geometry from the TYPE definition (origin-based, reusable)
        2. Fall back to instance representation with MappedItem wrapping
           (which references the geometry at its local origin)
        3. Last resort: raw copy of representation items
        
        Returns IfcProductDefinitionShape or None.
        """
        if not source_component:
            return None

        # Check rep cache first
        cache_key = (source_component["filename"], source_component["revit_id"])
        if cache_key in self._rep_cache:
            # Return a fresh copy of the cached shape
            cached = self._rep_cache[cache_key]
            if cached is None:
                return None
            try:
                return target_model.add(cached)
            except Exception:
                pass

        source_model = self._open_ifc(source_component["filename"])
        if not source_model:
            self._rep_cache[cache_key] = None
            return None

        revit_id = source_component["revit_id"]
        source_element = None
        try:
            source_element = source_model.by_guid(revit_id)
        except Exception:
            try:
                for el in source_model.by_type(source_component["category"]):
                    if el.GlobalId == revit_id:
                        source_element = el
                        break
            except Exception:
                pass

        if not source_element:
            logger.warning("Element %s not found in %s", revit_id, source_component["filename"])
            self._rep_cache[cache_key] = None
            return None

        if not source_element.Representation:
            self._rep_cache[cache_key] = None
            return None

        try:
            result = self._extract_and_copy(target_model, source_model, source_element, body_ctx)
            # Don't cache the result directly since it belongs to target_model
            # Just mark as successfully processed
            if result:
                self._rep_cache[cache_key] = "OK"
            else:
                self._rep_cache[cache_key] = None
            return result
        except Exception as e:
            logger.warning("Transplant failed for %s: %s",
                          source_component.get("family_name", revit_id), e)
            self._rep_cache[cache_key] = None
            return None

    def _extract_and_copy(self, target, source_model, source_element, body_ctx):
        """
        Extract geometry and copy it to target. Tries multiple strategies
        to get origin-based geometry.
        """
        # Strategy 1: Get the type's representation maps
        # Type geometry is already at local origin — perfect for reuse
        type_rep = self._get_type_representation(source_element)
        if type_rep:
            return self._copy_via_mapped_rep(target, source_model, type_rep, body_ctx)

        # Strategy 2: Use the instance representation but create a MappedItem
        # that references it at origin. This effectively strips the placement.
        source_rep = source_element.Representation
        return self._copy_instance_rep_at_origin(target, source_model, source_rep, body_ctx)

    def _get_type_representation(self, element):
        """
        Get the IfcRepresentationMap from the element's type definition.
        Type representations are defined at origin and designed for reuse.
        """
        # IFC4: IsTypedBy
        try:
            if hasattr(element, 'IsTypedBy') and element.IsTypedBy:
                for rel in element.IsTypedBy:
                    if hasattr(rel, 'RelatingType') and rel.RelatingType:
                        type_def = rel.RelatingType
                        if hasattr(type_def, 'RepresentationMaps') and type_def.RepresentationMaps:
                            return type_def.RepresentationMaps
        except Exception:
            pass

        # IFC2X3: IsDefinedBy
        try:
            if hasattr(element, 'IsDefinedBy') and element.IsDefinedBy:
                for rel in element.IsDefinedBy:
                    if rel.is_a('IfcRelDefinesByType'):
                        type_def = rel.RelatingType
                        if hasattr(type_def, 'RepresentationMaps') and type_def.RepresentationMaps:
                            return type_def.RepresentationMaps
        except Exception:
            pass

        return None

    def _copy_via_mapped_rep(self, target, source_model, rep_maps, body_ctx):
        """
        Copy type representation maps into the target model.
        Creates IfcMappedItem references which are inherently origin-based.
        """
        copied_reps = []

        for rep_map in rep_maps:
            try:
                # Deep copy the entire representation map (includes all geometry)
                copied_map = target.add(rep_map)

                # Create a mapped item that references this map at identity transform
                origin = target.createIfcCartesianPoint((0.0, 0.0, 0.0))
                axis_x = target.createIfcDirection((1.0, 0.0, 0.0))
                axis_z = target.createIfcDirection((0.0, 0.0, 1.0))
                mapping_target = target.createIfcCartesianTransformationOperator3D(
                    axis_x, axis_z, origin, 1.0, None
                )

                mapped_item = target.createIfcMappedItem(copied_map, mapping_target)

                # Create shape representation containing the mapped item
                rep = target.createIfcShapeRepresentation(
                    body_ctx, "Body", "MappedRepresentation", [mapped_item]
                )
                copied_reps.append(rep)
            except Exception as e:
                logger.debug("Failed to copy rep map: %s", e)
                continue

        if not copied_reps:
            return None

        return target.createIfcProductDefinitionShape(None, None, copied_reps)

    def _copy_instance_rep_at_origin(self, target, source_model, source_rep, body_ctx):
        """
        Copy instance representation items directly.
        For each shape representation, copy its items into the target
        and wrap them in a new ShapeRepresentation with the target's context.
        
        This approach copies the raw geometry items (solids, surfaces, etc.)
        without any placement transform, so they end up at origin.
        """
        copied_reps = []

        for rep in source_rep.Representations:
            try:
                copied_items = []
                for item in rep.Items:
                    try:
                        copied_item = target.add(item)
                        copied_items.append(copied_item)
                    except Exception as e:
                        logger.debug("Failed to copy rep item: %s", e)
                        continue

                if not copied_items:
                    continue

                # Determine the representation type
                rep_type = rep.RepresentationType or "SweptSolid"
                rep_id = rep.RepresentationIdentifier or "Body"

                new_rep = target.createIfcShapeRepresentation(
                    body_ctx, rep_id, rep_type, copied_items
                )
                copied_reps.append(new_rep)
            except Exception as e:
                logger.debug("Failed to copy representation: %s", e)
                continue

        if not copied_reps:
            return None

        return target.createIfcProductDefinitionShape(None, None, copied_reps)

    def transplant_materials(self, target_model, oh, target_element, source_component):
        """Copy material associations from source to target element."""
        if not source_component:
            return

        source_model = self._open_ifc(source_component["filename"])
        if not source_model:
            return

        try:
            source_element = source_model.by_guid(source_component["revit_id"])
        except Exception:
            return

        try:
            if not hasattr(source_element, 'HasAssociations'):
                return
            for rel in source_element.HasAssociations:
                if rel.is_a("IfcRelAssociatesMaterial"):
                    mat = rel.RelatingMaterial
                    try:
                        copied_mat = target_model.add(mat)
                        target_model.createIfcRelAssociatesMaterial(
                            ifcopenshell.guid.new(), oh, None, None,
                            [target_element], copied_mat
                        )
                    except Exception as e:
                        logger.debug("Material copy failed: %s", e)
                    return
        except Exception as e:
            logger.debug("Material transplant failed: %s", e)

    def clear_cache(self):
        self._ifc_cache.clear()
        self._match_cache.clear()
        self._rep_cache.clear()
        self._component_index = None
