"""
extractor/geometry_transplant.py — Transplants real IFC geometry from library components.

Uses copy_deep to copy geometry items between files, then wraps them in a
MappedItem with a scale factor from CartesianTransformationOperator3D to
handle unit conversion (e.g. source in mm, target in m → scale 0.001).

Also remaps ContextOfItems references to the target file's body context.
Excludes generated_*.ifc files. Strict category matching.
"""

import os
import logging
import psycopg2.extras
import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.unit

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")

SEARCH_SYNONYMS = {
    "toilet":        ["toilet","wc","water closet","sanitary","lavatory","commode"],
    "sink":          ["sink","basin","washbasin","lavatory","wash hand","counter top w sink"],
    "shower":        ["shower","shower tray"],
    "bath":          ["bath","bathtub","tub"],
    "fridge":        ["fridge","refrigerator","refrigeration"],
    "refrigerator":  ["refrigerator","fridge"],
    "sofa":          ["sofa","couch","settee"],
    "couch":         ["couch","sofa","settee"],
    "coffee table":  ["coffee table","center table","cocktail table"],
    "dining table":  ["dining table","table - dining","dining"],
    "table":         ["table","desk","worktop","counter"],
    "chair":         ["chair","seat","stool","bar chair","chair - dining"],
    "bed":           ["bed","bed-standard","bunk","mattress"],
    "wardrobe":      ["wardrobe","closet","cupboard","cabinet","armoire"],
    "nightstand":    ["nightstand","night stand","bedside","side table"],
    "door":          ["door","single-flush","doors_intsgl","doors_extdbl"],
    "window":        ["window","glazing","glass"],
    "light":         ["light","lamp","luminaire","fixture","downlight"],
    "stove":         ["stove","oven","cooker","hob","range"],
    "washer":        ["washer","washing machine","laundry"],
    "tv unit":       ["tv","television","tv unit","media","cabinet 1"],
    "lower cabinets":["cabinet","base cabinet","lower cabinet","kitchen island"],
    "upper cabinets":["cabinet","upper cabinet","wall cabinet"],
    "desk":          ["desk","work table","writing table"],
    "water heater":  ["water heater","boiler","hot water","geyser"],
}

RELATED_CATEGORIES = {
    "IfcFurniture":          {"IfcFurniture", "IfcFurnishingElement"},
    "IfcFurnishingElement":  {"IfcFurniture", "IfcFurnishingElement"},
    "IfcSanitaryTerminal":   {"IfcSanitaryTerminal", "IfcFurnishingElement", "IfcFlowTerminal"},
    "IfcElectricAppliance":  {"IfcElectricAppliance", "IfcFurnishingElement", "IfcFlowTerminal"},
    "IfcLightFixture":       {"IfcLightFixture", "IfcFlowTerminal"},
    "IfcDoor":               {"IfcDoor"},
    "IfcWindow":             {"IfcWindow"},
}


class GeometryLibrary:
    def __init__(self, db_connection_func, upload_folder=None):
        self._get_db = db_connection_func
        self._upload_folder = upload_folder or UPLOAD_FOLDER
        self._ifc_cache = {}           # filename -> ifcopenshell model
        self._unit_scale_cache = {}    # filename -> unit scale factor
        self._match_cache = {}
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
                  AND p.filename NOT LIKE 'generated_%%'
                ORDER BY c.quality_score DESC NULLS LAST
            """)
            self._component_index = cursor.fetchall()
        logger.info("Loaded %d real components (excluding generated files)",
                    len(self._component_index))

    def _open_ifc(self, filename):
        if filename in self._ifc_cache:
            return self._ifc_cache[filename]
        filepath = os.path.join(self._upload_folder, filename)
        if not os.path.exists(filepath):
            return None
        try:
            model = ifcopenshell.open(filepath)
            self._ifc_cache[filename] = model
            # Cache the unit scale for this file
            try:
                self._unit_scale_cache[filename] = ifcopenshell.util.unit.calculate_unit_scale(model)
            except Exception:
                self._unit_scale_cache[filename] = 1.0
            return model
        except Exception as e:
            logger.error("Failed to open %s: %s", filename, e)
            return None

    def _get_unit_factor(self, source_filename, target_model):
        """
        Calculate the scale factor to convert geometry from source file units
        to target file units.
        
        E.g. source in mm (scale=0.001), target in m (scale=1.0) → factor = 0.001
        """
        src_scale = self._unit_scale_cache.get(source_filename, 1.0)
        try:
            tgt_scale = ifcopenshell.util.unit.calculate_unit_scale(target_model)
        except Exception:
            tgt_scale = 1.0

        if tgt_scale == 0:
            tgt_scale = 1.0

        factor = src_scale / tgt_scale
        return factor

    def find_component(self, name, category=None, target_w=None, target_d=None, target_h=None):
        cache_key = (name.lower(), category)
        if cache_key in self._match_cache:
            return self._match_cache[cache_key]

        self._load_component_index()

        name_lower = name.lower()
        terms = [name_lower]
        for key, syns in SEARCH_SYNONYMS.items():
            if name_lower == key or name_lower in syns:
                terms.extend(syns)
                if key not in terms:
                    terms.append(key)
        terms = list(set(terms))

        allowed_cats = RELATED_CATEGORIES.get(category, {category} if category else None)

        best = None
        best_score = -1

        for comp in self._component_index:
            if allowed_cats and comp["category"] not in allowed_cats:
                continue

            comp_name = (comp["family_name"] or "").lower()
            comp_type = (comp["type_name"] or "").lower()

            score = 0
            name_matched = False
            for term in terms:
                if term in comp_name or term in comp_type:
                    score += 10
                    if term == comp_name or term == comp_type:
                        score += 5
                    score += min(len(term), 5)
                    name_matched = True
                    break

            if not name_matched:
                continue

            if comp.get("quality_score"):
                score += comp["quality_score"] * 3
            if target_w and comp.get("width_mm") and comp["width_mm"] > 0:
                ratio = min(target_w * 1000, comp["width_mm"]) / max(target_w * 1000, comp["width_mm"])
                score += ratio * 2
            if target_h and comp.get("height_mm") and comp["height_mm"] > 0:
                ratio = min(target_h * 1000, comp["height_mm"]) / max(target_h * 1000, comp["height_mm"])
                score += ratio * 2

            filepath = os.path.join(self._upload_folder, comp["filename"])
            if not os.path.exists(filepath):
                continue

            if score > best_score:
                best_score = score
                best = comp

        self._match_cache[cache_key] = best
        if best:
            print(f"      MATCH '{name}' -> {best['family_name'][:50]} [{best['category']}] "
                  f"from {best['filename']} (score={best_score:.0f})")
        else:
            print(f"      NO MATCH for '{name}' (category={category})")
        return best

    def transplant_geometry(self, target_model, source_component, body_ctx):
        if not source_component:
            return None

        source_model = self._open_ifc(source_component["filename"])
        if not source_model:
            return None

        source_element = self._find_element(source_model, source_component["revit_id"],
                                             source_component["category"])
        if not source_element or not source_element.Representation:
            return None

        # Get unit conversion factor
        unit_factor = self._get_unit_factor(source_component["filename"], target_model)

        try:
            return self._copy_geometry(target_model, source_element, body_ctx, unit_factor)
        except Exception as e:
            logger.warning("Transplant failed for %s: %s",
                          source_component.get("family_name", "?"), e)
            return None

    def _find_element(self, model, revit_id, category):
        try:
            return model.by_guid(revit_id)
        except Exception:
            pass
        try:
            for el in model.by_type(category):
                if el.GlobalId == revit_id:
                    return el
        except Exception:
            pass
        return None

    def _copy_geometry(self, target, source_element, body_ctx, unit_factor):
        """
        Copy geometry from source element into target model.
        
        1. copy_deep each representation item (excluding context refs)
        2. Remap context references inside MappedItems
        3. Wrap everything in a new MappedItem with scale factor for unit conversion
        4. Return ProductDefinitionShape
        """
        source_rep = source_element.Representation
        all_copied_items = []

        for rep in source_rep.Representations:
            # Only copy Body representations (skip Axis, BoundingBox, etc.)
            rep_id = rep.RepresentationIdentifier or ""
            if rep_id not in ("Body", "body", ""):
                continue

            for item in rep.Items:
                try:
                    copied_item = ifcopenshell.util.element.copy_deep(
                        target, item,
                        exclude=["IfcGeometricRepresentationContext",
                                 "IfcGeometricRepresentationSubContext"]
                    )
                    # Fix dangling context refs inside MappedItems
                    self._remap_contexts(copied_item, body_ctx)
                    all_copied_items.append(copied_item)
                except Exception as e:
                    logger.debug("Failed to copy item %s: %s", item.is_a(), e)
                    continue

        if not all_copied_items:
            return None

        # Create an inner ShapeRepresentation holding the raw geometry
        inner_rep = target.createIfcShapeRepresentation(
            body_ctx, "Body", "SweptSolid", all_copied_items
        )

        # Wrap in a RepresentationMap + MappedItem with unit scale factor
        origin = target.createIfcCartesianPoint((0.0, 0.0, 0.0))
        map_origin = target.createIfcAxis2Placement3D(origin, None, None)
        rep_map = target.createIfcRepresentationMap(map_origin, inner_rep)

        # Apply unit conversion scale via the MappingTarget transform
        needs_scale = abs(unit_factor - 1.0) > 1e-6
        if needs_scale:
            mapping_target = target.createIfcCartesianTransformationOperator3D(
                None, None, origin, unit_factor, None
            )
        else:
            mapping_target = target.createIfcCartesianTransformationOperator3D(
                None, None, origin, 1.0, None
            )

        mapped_item = target.createIfcMappedItem(rep_map, mapping_target)

        # Outer representation
        outer_rep = target.createIfcShapeRepresentation(
            body_ctx, "Body", "MappedRepresentation", [mapped_item]
        )

        return target.createIfcProductDefinitionShape(None, None, [outer_rep])

    def _remap_contexts(self, entity, body_ctx):
        """Fix dangling ContextOfItems references in copied entities."""
        if entity is None:
            return

        if entity.is_a("IfcMappedItem"):
            try:
                mapped_rep = entity.MappingSource.MappedRepresentation
                if mapped_rep and hasattr(mapped_rep, "ContextOfItems"):
                    mapped_rep.ContextOfItems = body_ctx
            except Exception:
                pass

        if hasattr(entity, "ContextOfItems"):
            try:
                entity.ContextOfItems = body_ctx
            except Exception:
                pass

    def transplant_materials(self, target_model, oh, target_element, source_component):
        if not source_component:
            return
        source_model = self._open_ifc(source_component["filename"])
        if not source_model:
            return
        source_element = self._find_element(
            source_model, source_component["revit_id"], source_component["category"])
        if not source_element:
            return
        try:
            if not hasattr(source_element, 'HasAssociations'):
                return
            for rel in source_element.HasAssociations:
                if rel.is_a("IfcRelAssociatesMaterial"):
                    try:
                        copied_mat = ifcopenshell.util.element.copy_deep(
                            target_model, rel.RelatingMaterial,
                            exclude=["IfcGeometricRepresentationContext",
                                     "IfcGeometricRepresentationSubContext"])
                        target_model.createIfcRelAssociatesMaterial(
                            ifcopenshell.guid.new(), oh, None, None,
                            [target_element], copied_mat)
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    def clear_cache(self):
        self._ifc_cache.clear()
        self._unit_scale_cache.clear()
        self._match_cache.clear()
        self._component_index = None
