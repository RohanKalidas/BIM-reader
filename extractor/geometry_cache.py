"""
geometry_cache.py — Persist actual IFC product geometry during strip, reload in reconstruct.

Writes one sidecar file per project: uploads/geometry_cache/{project_id}.ifc
Each source IfcElement with Representation gets a IfcBuildingElementProxy with the same
GlobalId and a copied ProductDefinitionShape (Body reps). Reconstruct copies that shape
into the rebuilt model and remaps geometric contexts.

Color / appearance in IFC:
- **IfcStyledItem** (often inside Body `Representation.Items`) wraps geometry and points to
  **IfcSurfaceStyle** / **IfcSurfaceStyleRendering** / **IfcColourRgb** — copied with `copy_deep`
  when present on those items.
- **IfcRelAssociatesMaterial** on the product — **IfcMaterial** may include
  **IfcMaterialDefinitionRepresentation** with styles. We copy material associations onto
  the cache proxy and again onto the reconstructed element so viewers can resolve colours.
"""

import os
import logging
import ifcopenshell
import ifcopenshell.guid
import ifcopenshell.util.element

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GEOM_DIR = os.path.join(REPO_ROOT, "uploads", "geometry_cache")
COPY_EXCLUDE = [
    "IfcGeometricRepresentationContext",
    "IfcGeometricRepresentationSubContext",
]


def copy_unit_assignment_to_model(target_model, source_model):
    """
    Copy IfcProject.UnitsInContext from source into target_model so numeric coordinates
    in copied geometry and in spatial data (mm vs m) match the declared length unit.
    """
    if not source_model or not target_model:
        return None
    try:
        projs = source_model.by_type("IfcProject")
        if not projs:
            return None
        ua = projs[0].UnitsInContext
        if not ua:
            return None
        return ifcopenshell.util.element.copy_deep(target_model, ua, exclude=COPY_EXCLUDE)
    except Exception as e:
        logger.debug("copy_unit_assignment_to_model: %s", e)
        return None


def default_unit_assignment(model):
    """Fallback when no source IFC is available (metre-based)."""
    return model.create_entity(
        "IfcUnitAssignment",
        Units=[
            model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE"),
            model.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE"),
            model.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE"),
            model.create_entity("IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN"),
        ],
    )


def _defining_types(element):
    """IfcRelDefinesByType targets (IFC4 IsTypedBy; IFC2x3 IsDefinedBy)."""
    out = []
    if element is None:
        return out
    for rel in getattr(element, "IsTypedBy", None) or []:
        if rel.is_a("IfcRelDefinesByType"):
            t = rel.RelatingType
            if t:
                out.append(t)
    if out:
        return out
    for rel in getattr(element, "IsDefinedBy", None) or []:
        if rel.is_a("IfcRelDefinesByType"):
            t = rel.RelatingType
            if t:
                out.append(t)
    return out


def geometry_cache_path(project_id: int) -> str:
    os.makedirs(GEOM_DIR, exist_ok=True)
    return os.path.join(GEOM_DIR, f"{int(project_id)}.ifc")


def open_geometry_cache(project_id: int):
    """Return opened ifcopenshell model or None if no cache file."""
    path = geometry_cache_path(project_id)
    if not os.path.isfile(path) or os.path.getsize(path) < 100:
        return None
    try:
        return ifcopenshell.open(path)
    except Exception as e:
        logger.warning("Could not open geometry cache %s: %s", path, e)
        return None


def _is_body_shape_representation(rep) -> bool:
    """
    Skip non-body shape reps (Axis, BoundingBox, FootPrint, Reference, …).
    Copying those into the cache makes viewers show extra wireframes, bbox
    solids, and bogus faces (e.g. huge circles from axis geometry).

    Revit often leaves RepresentationIdentifier empty but sets RepresentationType
    to Axis or BoundingBox — we must filter on RepresentationType first.
    """
    if not rep:
        return False
    rtype = (getattr(rep, "RepresentationType", None) or "").strip().lower()
    # IFC uses this for the *kind* of shape rep (Axis, BoundingBox, SweptSolid, …)
    if rtype in (
        "axis",
        "boundingbox",
        "box",
        "curve",
        "curve2d",
        "curve3d",
        "point",
        "annotation",
        "outline",
        "reference",
        "geometriccurveset",
        "centreline",
        "centerline",
        "footprint",
        "foot print",
        "sketch",
        "2d",
        "2dsketch",
    ):
        return False

    rid = (getattr(rep, "RepresentationIdentifier", None) or "").strip()
    if not rid:
        return True
    r = rid.lower().replace(" ", "")
    if r in (
        "axis",
        "boundingbox",
        "box",
        "footprint",
        "reference",
        "ref",
        "annotation",
        "plan",
        "surface",
        "lightsource",
        "light",
        "clearance",
        "profile",
        "centreline",
        "centerline",
    ):
        return False
    # Substring match to catch identifiers like "Body_Clearance", "LightSource_1",
    # "Surface_Symbolic" that some Revit exports emit alongside the real body rep.
    for kw in ("clearance", "symbolic", "annotation", "axis", "light", "reference"):
        if kw in r and r != "body":
            return False
    return True


def _should_skip_representation_item(item) -> bool:
    """
    Skip items that often produce bogus giant faces / disks / infinite planes in viewers
    when copy_deep'd into a sidecar file.
    """
    if item is None:
        return True
    name = item.is_a()
    # Bounding / wire / clipping planes
    if name in (
        "IfcBoundingBox",
        "IfcGeometricCurveSet",
        "IfcHalfSpaceSolid",
        "IfcPolygonalBoundedHalfSpace",
        "IfcBoxedHalfSpace",
    ):
        return True
    # Swept-disk solids (pipes, handrails) often render as huge circular slabs if
    # axis/radius are wrong after copy; skip in cache (still in DB metadata).
    if name == "IfcSweptDiskSolid":
        return True
    # Full revolutions can become vertical disks when axis/placement are off-axis
    if name == "IfcRevolvedAreaSolid":
        return True
    return False


def _remap_contexts(entity, body_ctx):
    """Point ContextOfItems to target body context (recursive)."""
    if entity is None:
        return
    if entity.is_a("IfcMappedItem"):
        try:
            mr = entity.MappingSource.MappedRepresentation
            if mr and hasattr(mr, "ContextOfItems"):
                mr.ContextOfItems = body_ctx
        except Exception:
            pass
    if hasattr(entity, "ContextOfItems"):
        try:
            entity.ContextOfItems = body_ctx
        except Exception:
            pass
    if hasattr(entity, "Items"):
        try:
            for it in entity.Items or []:
                _remap_contexts(it, body_ctx)
        except Exception:
            pass


def _copy_product_shape_to_model(target, body_ctx, source_element):
    """
    Copy source_element.Representation into target model; return IfcProductDefinitionShape or None.
    """
    if not source_element or not source_element.Representation:
        return None
    source_pds = source_element.Representation
    new_reps = []
    try:
        for rep in source_pds.Representations or []:
            if not _is_body_shape_representation(rep):
                continue
            new_items = []
            for item in rep.Items or []:
                try:
                    if _should_skip_representation_item(item):
                        continue
                    copied = ifcopenshell.util.element.copy_deep(
                        target, item, exclude=COPY_EXCLUDE
                    )
                    _remap_contexts(copied, body_ctx)
                    new_items.append(copied)
                except Exception as e:
                    logger.debug("copy_deep item failed: %s", e)
            if not new_items:
                continue
            rid = rep.RepresentationIdentifier or "Body"
            rtype = rep.RepresentationType or "SweptSolid"
            new_rep = target.createIfcShapeRepresentation(
                body_ctx, rid, rtype, new_items
            )
            new_reps.append(new_rep)
        if not new_reps:
            return None
        return target.createIfcProductDefinitionShape(None, None, new_reps)
    except Exception as e:
        logger.debug("copy product shape failed: %s", e)
        return None


def _minimal_host_model(schema: str, source_model=None):
    """Minimal project + geometric context for hosting geometry proxies."""
    from datetime import datetime

    model = ifcopenshell.file(schema=schema)
    origin = model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    wcs = model.create_entity("IfcAxis2Placement3D", Location=origin, Axis=None, RefDirection=None)
    geom_context = model.create_entity(
        "IfcGeometricRepresentationContext",
        ContextIdentifier="Model",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1.0e-5,
        WorldCoordinateSystem=wcs,
        TrueNorth=None,
    )
    # IFC2X3 has no IfcGeometricRepresentationSubContext
    if "IFC4" in schema or "IFC4X3" in schema:
        body_ctx = model.create_entity(
            "IfcGeometricRepresentationSubContext",
            ContextIdentifier="Body",
            ContextType="Model",
            ParentContext=geom_context,
            TargetView="MODEL_VIEW",
        )
    else:
        body_ctx = geom_context
    units = copy_unit_assignment_to_model(model, source_model) or default_unit_assignment(model)
    application = model.create_entity(
        "IfcApplication",
        ApplicationDeveloper=model.create_entity("IfcOrganization", Name="BIM geometry cache"),
        Version="1.0",
        ApplicationFullName="Geometry cache",
        ApplicationIdentifier="GEOM-CACHE",
    )
    person = model.create_entity("IfcPerson", FamilyName="Cache")
    org = model.create_entity("IfcOrganization", Name="Cache")
    po = model.create_entity("IfcPersonAndOrganization", ThePerson=person, TheOrganization=org)
    owner_history = model.create_entity(
        "IfcOwnerHistory",
        OwningUser=po,
        OwningApplication=application,
        State="READWRITE",
        ChangeAction="ADDED",
        CreationDate=int(datetime.now().timestamp()),
    )
    world_axis = model.create_entity(
        "IfcAxis2Placement3D",
        Location=origin,
        Axis=None,
        RefDirection=None,
    )
    world_placement = model.create_entity("IfcLocalPlacement", PlacementRelTo=None, RelativePlacement=world_axis)
    project = model.create_entity(
        "IfcProject",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name="GeometryCache",
        RepresentationContexts=[geom_context],
        UnitsInContext=units,
    )
    site = model.create_entity(
        "IfcSite",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name="CacheSite",
        ObjectPlacement=world_placement,
    )
    building = model.create_entity(
        "IfcBuilding",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name="CacheBuilding",
        ObjectPlacement=world_placement,
    )
    storey = model.create_entity(
        "IfcBuildingStorey",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name="CacheLevel",
        ObjectPlacement=world_placement,
        Elevation=0.0,
    )
    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        RelatingObject=project,
        RelatedObjects=[site],
    )
    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        RelatingObject=site,
        RelatedObjects=[building],
    )
    model.create_entity(
        "IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        RelatingObject=building,
        RelatedObjects=[storey],
    )
    return model, body_ctx, world_placement, owner_history, storey


class GeometryCacheWriter:
    """Build a sidecar IFC with one proxy per scraped element (same GlobalId, real shape)."""

    def __init__(self, project_id: int, schema: str):
        self.project_id = project_id
        self.schema = schema
        self.path = geometry_cache_path(project_id)
        self._model = None
        self._body_ctx = None
        self._placement = None
        self._owner_history = None
        self._storey = None
        self._proxies = []
        self.count = 0

    def _ensure(self, source_model=None):
        if self._model is None:
            self._model, self._body_ctx, self._placement, self._owner_history, self._storey = _minimal_host_model(
                self.schema, source_model
            )

    def try_add(self, source_model, element) -> bool:
        """
        Copy element geometry into cache model. Returns True if at least one representation was stored.
        """
        if not element.Representation:
            return False
        self._ensure(source_model)
        pds = _copy_product_shape_to_model(self._model, self._body_ctx, element)
        if not pds:
            return False
        try:
            proxy = self._model.createIfcBuildingElementProxy(
                GlobalId=element.GlobalId,
                OwnerHistory=self._owner_history,
                Name=f"GEOM_CACHE::{element.is_a()}",
                ObjectPlacement=self._placement,
                Representation=pds,
            )
            copy_material_associations_to_element(
                self._model, self._owner_history, element, proxy
            )
            self._proxies.append(proxy)
            self.count += 1
            return True
        except Exception as e:
            logger.debug("Cache proxy create failed for %s: %s", element.GlobalId, e)
            return False

    def write_if_nonempty(self):
        if self._model is None or self.count == 0:
            if os.path.isfile(self.path):
                try:
                    os.remove(self.path)
                except OSError:
                    pass
            return None
        if self._proxies:
            try:
                self._model.create_entity(
                    "IfcRelContainedInSpatialStructure",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=self._owner_history,
                    RelatingStructure=self._storey,
                    RelatedElements=self._proxies,
                )
            except Exception as e:
                logger.warning("Could not link geometry proxies to storey: %s", e)
        try:
            self._model.write(self.path)
            logger.info("Wrote geometry cache: %s (%d elements)", self.path, self.count)
            return self.path
        except Exception as e:
            logger.warning("Failed to write geometry cache: %s", e)
            return None


def copy_material_associations_to_element(target_model, owner_history, source_element, target_element):
    """
    Copy IfcRelAssociatesMaterial from source product to target product (material subtree
    may contain surface colours / rendering).
    If the instance has no material, copy from IfcTypeProduct (Revit often assigns there).
    """
    if not source_element or not target_element:
        return
    try:
        assocs = list(getattr(source_element, "HasAssociations", None) or [])
    except Exception:
        return
    for rel in assocs:
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        try:
            copied_mat = ifcopenshell.util.element.copy_deep(
                target_model,
                rel.RelatingMaterial,
                exclude=COPY_EXCLUDE,
            )
            target_model.createIfcRelAssociatesMaterial(
                ifcopenshell.guid.new(),
                owner_history,
                None,
                None,
                [target_element],
                copied_mat,
            )
        except Exception as e:
            logger.debug("copy_material_associations_to_element: %s", e)
    # Type-level materials (common for furniture / hosted types)
    try:
        has_direct = any(r.is_a("IfcRelAssociatesMaterial") for r in assocs)
    except Exception:
        has_direct = False
    if has_direct:
        return
    for ptype in _defining_types(source_element):
        try:
            tassocs = getattr(ptype, "HasAssociations", None) or []
        except Exception:
            continue
        for rel in tassocs:
            if not rel.is_a("IfcRelAssociatesMaterial"):
                continue
            try:
                copied_mat = ifcopenshell.util.element.copy_deep(
                    target_model,
                    rel.RelatingMaterial,
                    exclude=COPY_EXCLUDE,
                )
                target_model.createIfcRelAssociatesMaterial(
                    ifcopenshell.guid.new(),
                    owner_history,
                    None,
                    None,
                    [target_element],
                    copied_mat,
                )
            except Exception as e:
                logger.debug("copy_material_associations_to_element (type): %s", e)


def copy_cached_geometry_to_element(
    target_model, body_ctx, cache_element, target_element, owner_history=None
):
    """
    Copy ProductDefinitionShape from cache proxy onto target_element; remap contexts.
    If owner_history is set, also copy material associations from the cache proxy.
    Returns True on success.
    """
    if not cache_element or not cache_element.Representation:
        return False
    try:
        new_pds = ifcopenshell.util.element.copy_deep(
            target_model, cache_element.Representation, exclude=COPY_EXCLUDE
        )
        for rep in new_pds.Representations or []:
            rep.ContextOfItems = body_ctx
            for item in rep.Items or []:
                _remap_contexts(item, body_ctx)
        target_element.Representation = new_pds
        if owner_history is not None:
            copy_material_associations_to_element(
                target_model, owner_history, cache_element, target_element
            )
        return True
    except Exception as e:
        logger.debug("copy_cached_geometry_to_element failed: %s", e)
        return False
