"""
extractor/geometry_transplant.py — Transplants real IFC geometry from library components.

Uses ifcopenshell.util.element.copy_deep() to deep-copy geometry items
between IFC files, with exclude to skip context entities. Then remaps
any ContextOfItems references inside MappedRepresentations to the target
file's body context.

Excludes generated_*.ifc files.
Strict category matching.
"""

import os
import logging
import psycopg2.extras
import ifcopenshell
import ifcopenshell.util.element

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
            return model
        except Exception as e:
            logger.error("Failed to open %s: %s", filename, e)
            return None

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

        try:
            return self._copy_geometry(target_model, source_element, body_ctx)
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

    def _copy_geometry(self, target, source_element, body_ctx):
        """
        Core geometry copy. For each representation item:
        1. copy_deep with context exclusion
        2. Remap any ContextOfItems inside MappedRepresentations to target's body_ctx
        3. Wrap in fresh ShapeRepresentation with target's body_ctx
        """
        source_rep = source_element.Representation
        copied_reps = []

        for rep in source_rep.Representations:
            copied_items = []
            for item in rep.Items:
                try:
                    copied_item = ifcopenshell.util.element.copy_deep(
                        target, item,
                        exclude=["IfcGeometricRepresentationContext",
                                 "IfcGeometricRepresentationSubContext"]
                    )

                    # FIX: Remap context references inside MappedItems
                    # copy_deep excludes creating new context entities but leaves
                    # dangling references in MappedRepresentation.ContextOfItems.
                    # We need to point these to our target's body context.
                    self._remap_contexts(copied_item, body_ctx)

                    copied_items.append(copied_item)
                except Exception as e:
                    logger.debug("Failed to copy item %s: %s", item.is_a(), e)
                    continue

            if not copied_items:
                continue

            new_rep = target.createIfcShapeRepresentation(
                body_ctx,
                rep.RepresentationIdentifier or "Body",
                rep.RepresentationType or "SweptSolid",
                copied_items
            )
            copied_reps.append(new_rep)

        if not copied_reps:
            return None

        return target.createIfcProductDefinitionShape(None, None, copied_reps)

    def _remap_contexts(self, entity, body_ctx):
        """
        Recursively find and fix any ContextOfItems references inside
        a copied entity tree. This is needed because copy_deep with
        exclude=["IfcGeometricRepresentationContext"] leaves dangling
        references that point to stale/wrong context entities.
        """
        if entity is None:
            return

        # MappedItem -> MappingSource -> MappedRepresentation has ContextOfItems
        if entity.is_a("IfcMappedItem"):
            try:
                mapped_rep = entity.MappingSource.MappedRepresentation
                if mapped_rep and hasattr(mapped_rep, "ContextOfItems"):
                    mapped_rep.ContextOfItems = body_ctx
            except Exception:
                pass

        # Also check if entity itself has ContextOfItems (ShapeRepresentation)
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
        self._match_cache.clear()
        self._component_index = None
