"""
extractor/geometry_transplant.py — Transplants real IFC geometry from library components.

Excludes generated_*.ifc files (our own box output).
Uses type representations when available (origin-based, reusable).
Falls back to instance representation items stripped of placement.
"""

import os
import logging
import psycopg2.extras
import ifcopenshell

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")

# Search synonyms — maps fixture template names to terms found in Revit family names
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


class GeometryLibrary:
    def __init__(self, db_connection_func, upload_folder=None):
        self._get_db = db_connection_func
        self._upload_folder = upload_folder or UPLOAD_FOLDER
        self._ifc_cache = {}
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

        logger.info("Loaded %d real components into geometry library (excluding generated files)",
                    len(self._component_index))

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
        """
        Find best matching component. Strict category matching —
        if category is specified, ONLY components of that category (or related) are considered.
        """
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

        # Related categories — e.g. IfcFurniture and IfcFurnishingElement are interchangeable
        related_cats = set()
        if category:
            related_cats.add(category)
            if category in ("IfcFurniture", "IfcFurnishingElement"):
                related_cats.update(("IfcFurniture", "IfcFurnishingElement"))
            if category == "IfcSanitaryTerminal":
                related_cats.update(("IfcSanitaryTerminal", "IfcFurnishingElement"))

        best = None
        best_score = -1

        for comp in self._component_index:
            comp_name = (comp["family_name"] or "").lower()
            comp_type = (comp["type_name"] or "").lower()
            comp_cat  = comp["category"]

            # STRICT category filter — skip if wrong category
            if category and comp_cat not in related_cats:
                continue

            # Name matching
            score = 0
            name_matched = False
            for term in terms:
                if term in comp_name or term in comp_type:
                    score += 10
                    if term == comp_name or term == comp_type:
                        score += 5
                    # Bonus for longer matches (more specific)
                    score += min(len(term), 5)
                    name_matched = True
                    break

            if not name_matched:
                continue

            # Quality score bonus
            if comp.get("quality_score"):
                score += comp["quality_score"] * 3

            # Size similarity
            if target_w and comp.get("width_mm") and comp["width_mm"] > 0:
                ratio = min(target_w * 1000, comp["width_mm"]) / max(target_w * 1000, comp["width_mm"])
                score += ratio * 2
            if target_h and comp.get("height_mm") and comp["height_mm"] > 0:
                ratio = min(target_h * 1000, comp["height_mm"]) / max(target_h * 1000, comp["height_mm"])
                score += ratio * 2

            # Verify source file exists
            filepath = os.path.join(self._upload_folder, comp["filename"])
            if not os.path.exists(filepath):
                continue

            if score > best_score:
                best_score = score
                best = comp

        self._match_cache[cache_key] = best
        if best:
            print(f"      MATCH '{name}' -> {best['family_name'][:50]} [{best['category']}] from {best['filename']} (score={best_score:.0f})")
        else:
            print(f"      NO MATCH for '{name}' (category={category})")

        return best

    def transplant_geometry(self, target_model, source_component, body_ctx):
        """Deep-copy geometry from source into target model at origin."""
        if not source_component:
            return None

        source_model = self._open_ifc(source_component["filename"])
        if not source_model:
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
            return None

        if not source_element.Representation:
            return None

        try:
            return self._extract_and_copy(target_model, source_model, source_element, body_ctx)
        except Exception as e:
            logger.warning("Transplant failed for %s: %s",
                          source_component.get("family_name", revit_id), e)
            return None

    def _extract_and_copy(self, target, source_model, source_element, body_ctx):
        """
        Extract geometry and copy to target.
        Strategy 1: Type representation maps (origin-based, designed for reuse)
        Strategy 2: Instance representation items (raw geometry, no placement)
        """
        # Strategy 1: Type representation
        type_rep = self._get_type_representation(source_element)
        if type_rep:
            result = self._copy_via_mapped_rep(target, type_rep, body_ctx)
            if result:
                return result

        # Strategy 2: Instance representation items
        return self._copy_instance_rep_at_origin(target, source_element.Representation, body_ctx)

    def _get_type_representation(self, element):
        """Get RepresentationMaps from the element's type definition."""
        # IFC4
        try:
            if hasattr(element, 'IsTypedBy') and element.IsTypedBy:
                for rel in element.IsTypedBy:
                    if hasattr(rel, 'RelatingType') and rel.RelatingType:
                        t = rel.RelatingType
                        if hasattr(t, 'RepresentationMaps') and t.RepresentationMaps:
                            return t.RepresentationMaps
        except Exception:
            pass
        # IFC2X3
        try:
            if hasattr(element, 'IsDefinedBy') and element.IsDefinedBy:
                for rel in element.IsDefinedBy:
                    if rel.is_a('IfcRelDefinesByType'):
                        t = rel.RelatingType
                        if hasattr(t, 'RepresentationMaps') and t.RepresentationMaps:
                            return t.RepresentationMaps
        except Exception:
            pass
        return None

    def _copy_via_mapped_rep(self, target, rep_maps, body_ctx):
        """Copy type representation maps as MappedItems (inherently origin-based)."""
        copied_reps = []
        for rep_map in rep_maps:
            try:
                copied_map = target.add(rep_map)
                origin = target.createIfcCartesianPoint((0.0, 0.0, 0.0))
                mapping_target = target.createIfcCartesianTransformationOperator3D(
                    None, None, origin, 1.0, None
                )
                mapped_item = target.createIfcMappedItem(copied_map, mapping_target)
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

    def _copy_instance_rep_at_origin(self, target, source_rep, body_ctx):
        """Copy raw geometry items from instance representation."""
        copied_reps = []
        for rep in source_rep.Representations:
            try:
                copied_items = []
                for item in rep.Items:
                    try:
                        copied_items.append(target.add(item))
                    except Exception as e:
                        logger.debug("Failed to copy item: %s", e)
                        continue

                if not copied_items:
                    continue

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
        """Copy material associations from source to target."""
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
                    try:
                        copied_mat = target_model.add(rel.RelatingMaterial)
                        target_model.createIfcRelAssociatesMaterial(
                            ifcopenshell.guid.new(), oh, None, None,
                            [target_element], copied_mat
                        )
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    def clear_cache(self):
        self._ifc_cache.clear()
        self._match_cache.clear()
        self._component_index = None
