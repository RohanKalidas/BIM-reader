"""
generate.py
───────────
Takes a BuildingSpec dict produced by the AI and writes a valid IFC4 file.

BuildingSpec schema:
{
  "name": "Residential House",
  "floors": [
    {
      "name": "Ground Floor",
      "elevation": 0.0,
      "height": 3000,
      "components": [
        {
          "category": "IfcWall",
          "name": "Exterior Wall North",
          "material": "Brick",
          "pos_x": 0, "pos_y": 0, "pos_z": 0,
          "rot_z": 0,
          "width_mm": 290, "height_mm": 3000, "length_mm": 8000,
          "properties": {}
        }
      ]
    }
  ],
  "metadata": {
    "location": "Austin, TX",
    "estimated_cost_usd": 250000,
    "gross_floor_area_m2": 120,
    "building_type": "Residential"
  }
}
"""

import math
import json
import os
import sys
import ifcopenshell
import ifcopenshell.guid
from datetime import datetime


# ── Category map ───────────────────────────────────────────────────────────────

CATEGORY_MAP = {
    "IfcWall": "IfcWall", "IfcWallStandardCase": "IfcWallStandardCase",
    "IfcSlab": "IfcSlab", "IfcRoof": "IfcRoof",
    "IfcDoor": "IfcDoor", "IfcWindow": "IfcWindow",
    "IfcColumn": "IfcColumn", "IfcColumnStandardCase": "IfcColumnStandardCase",
    "IfcBeam": "IfcBeam", "IfcBeamStandardCase": "IfcBeamStandardCase",
    "IfcStair": "IfcStair", "IfcStairFlight": "IfcStairFlight",
    "IfcRailing": "IfcRailing", "IfcCurtainWall": "IfcCurtainWall",
    "IfcCovering": "IfcCovering", "IfcPlate": "IfcPlate", "IfcMember": "IfcMember",
    "IfcDuctSegment": "IfcDuctSegment", "IfcPipeSegment": "IfcPipeSegment",
    "IfcAirTerminal": "IfcAirTerminal", "IfcLightFixture": "IfcLightFixture",
    "IfcOutlet": "IfcOutlet", "IfcValve": "IfcValve", "IfcPump": "IfcPump",
    "IfcFan": "IfcFan", "IfcFlowSegment": "IfcFlowSegment",
    "IfcFurniture": "IfcFurniture", "IfcFurnishingElement": "IfcFurnishingElement",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_placement(model, px, py, pz, rot_z_deg=0.0):
    x, y, z = float(px or 0), float(py or 0), float(pz or 0)
    rz = math.radians(float(rot_z_deg or 0))
    cz, sz = math.cos(rz), math.sin(rz)
    location = model.createIfcCartesianPoint((x, y, z))
    axis     = model.createIfcDirection((0.0, 0.0, 1.0))
    ref_dir  = model.createIfcDirection((cz, sz, 0.0))
    axis2    = model.createIfcAxis2Placement3D(location, axis, ref_dir)
    return model.createIfcLocalPlacement(None, axis2)


def create_element(model, owner_history, category, name, placement):
    ifc_type = CATEGORY_MAP.get(category, "IfcBuildingElementProxy")
    try:
        return model.create_entity(
            ifc_type,
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_history,
            Name=name or category,
            ObjectPlacement=placement,
            Representation=None
        )
    except Exception:
        return model.create_entity(
            "IfcBuildingElementProxy",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_history,
            Name=name or category,
            ObjectPlacement=placement,
            Representation=None
        )


def attach_psets(model, owner_history, element, properties):
    if not properties:
        return
    for pset_name, pset_data in properties.items():
        if not isinstance(pset_data, dict):
            continue
        props = []
        for k, v in pset_data.items():
            if v is None:
                continue
            try:
                props.append(model.create_entity(
                    "IfcPropertySingleValue",
                    Name=str(k),
                    NominalValue=model.create_entity("IfcLabel", wrappedValue=str(v))
                ))
            except Exception:
                continue
        if not props:
            continue
        try:
            pset = model.create_entity(
                "IfcPropertySet",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_history,
                Name=pset_name,
                HasProperties=props
            )
            model.create_entity(
                "IfcRelDefinesByProperties",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_history,
                RelatedObjects=[element],
                RelatingPropertyDefinition=pset
            )
        except Exception:
            continue


def attach_material(model, owner_history, element, material_name):
    if not material_name:
        return
    try:
        mat = model.create_entity("IfcMaterial", Name=str(material_name))
        model.create_entity(
            "IfcRelAssociatesMaterial",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_history,
            RelatedObjects=[element],
            RelatingMaterial=mat
        )
    except Exception:
        pass


# ── Main ────────────────────────────────────────────────────────────────────────

def generate_ifc(spec: dict, output_path: str = None) -> str:
    name     = spec.get("name", "Generated Building")
    floors   = spec.get("floors", [])
    metadata = spec.get("metadata", {})

    total_comps = sum(len(f.get("components", [])) for f in floors)
    print(f"Generating IFC: {name} | {len(floors)} floors | {total_comps} components")

    model = ifcopenshell.file(schema="IFC4")

    # Owner history
    app = model.create_entity(
        "IfcApplication",
        ApplicationDeveloper=model.create_entity("IfcOrganization", Name="BIM Studio"),
        Version="1.0",
        ApplicationFullName="BIM Studio AI Generator",
        ApplicationIdentifier="BIM-STUDIO-GEN"
    )
    person = model.create_entity("IfcPerson", FamilyName="AI Architect")
    org    = model.create_entity("IfcOrganization", Name="BIM Studio")
    pao    = model.create_entity("IfcPersonAndOrganization", ThePerson=person, TheOrganization=org)
    oh     = model.create_entity(
        "IfcOwnerHistory",
        OwningUser=pao, OwningApplication=app,
        State="READWRITE", ChangeAction="ADDED",
        CreationDate=int(datetime.now().timestamp())
    )

    # Units
    units = model.create_entity("IfcUnitAssignment", Units=[
        model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT",    Name="METRE"),
        model.create_entity("IfcSIUnit", UnitType="AREAUNIT",      Name="SQUARE_METRE"),
        model.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT",    Name="CUBIC_METRE"),
        model.create_entity("IfcSIUnit", UnitType="PLANEANGLEUNIT",Name="RADIAN"),
    ])

    # World placement
    wo  = model.createIfcCartesianPoint((0.0, 0.0, 0.0))
    wa  = model.createIfcAxis2Placement3D(wo, None, None)
    wp  = model.createIfcLocalPlacement(None, wa)

    # Project → Site → Building
    proj = model.create_entity("IfcProject",
        GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
        Name=name, UnitsInContext=units)
    site = model.create_entity("IfcSite",
        GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
        Name=metadata.get("location", "Site"), ObjectPlacement=wp)
    bldg = model.create_entity("IfcBuilding",
        GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
        Name=name, ObjectPlacement=wp)

    model.create_entity("IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
        RelatingObject=proj, RelatedObjects=[site])
    model.create_entity("IfcRelAggregates",
        GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
        RelatingObject=site, RelatedObjects=[bldg])

    # Storeys
    ifc_storeys = []
    storey_elements = []

    for floor in floors:
        elev = float(floor.get("elevation", 0.0))
        sl   = model.createIfcCartesianPoint((0.0, 0.0, elev))
        sa   = model.createIfcAxis2Placement3D(sl, None, None)
        sp   = model.createIfcLocalPlacement(wp, sa)
        st   = model.create_entity("IfcBuildingStorey",
            GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
            Name=floor.get("name", f"Level {len(ifc_storeys)+1}"),
            ObjectPlacement=sp, Elevation=elev)
        ifc_storeys.append(st)
        storey_elements.append([])

    if ifc_storeys:
        model.create_entity("IfcRelAggregates",
            GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
            RelatingObject=bldg, RelatedObjects=ifc_storeys)

    # Components
    for fi, floor in enumerate(floors):
        elems = storey_elements[fi]
        for comp in floor.get("components", []):
            cat      = comp.get("category", "IfcBuildingElementProxy")
            cname    = comp.get("name", cat)
            material = comp.get("material", "")

            placement = make_placement(
                model,
                comp.get("pos_x", 0),
                comp.get("pos_y", 0),
                comp.get("pos_z", 0),
                comp.get("rot_z", 0)
            )

            element = create_element(model, oh, cat, cname, placement)

            # Dimensions pset
            dim_props = {}
            for k, dk in [("width_mm","Width"),("height_mm","Height"),("length_mm","Length")]:
                if comp.get(k) is not None:
                    dim_props[dk] = comp[k]

            all_props = {}
            if dim_props:
                all_props["BIM_Studio_Dimensions"] = dim_props
            if comp.get("properties"):
                all_props.update(comp["properties"])

            attach_psets(model, oh, element, all_props)
            attach_material(model, oh, element, material)
            elems.append(element)

        if elems:
            model.create_entity("IfcRelContainedInSpatialStructure",
                GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
                RelatingStructure=ifc_storeys[fi],
                RelatedElements=elems)

    # Project metadata pset
    if metadata:
        meta_props = []
        for k, v in metadata.items():
            if v is None:
                continue
            try:
                meta_props.append(model.create_entity(
                    "IfcPropertySingleValue",
                    Name=str(k),
                    NominalValue=model.create_entity("IfcLabel", wrappedValue=str(v))
                ))
            except Exception:
                pass
        if meta_props:
            pset = model.create_entity("IfcPropertySet",
                GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
                Name="BIM_Studio_Project_Info", HasProperties=meta_props)
            model.create_entity("IfcRelDefinesByProperties",
                GlobalId=ifcopenshell.guid.new(), OwnerHistory=oh,
                RelatedObjects=[bldg], RelatingPropertyDefinition=pset)

    # Write
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = name.replace(" ", "_").replace("/", "-")
        output_path = f"generated_{safe_name}_{ts}.ifc"

    model.write(output_path)
    print(f"  Output: {output_path}")
    return output_path


if __name__ == "__main__":
    test_spec = {
        "name": "Test House",
        "floors": [{
            "name": "Ground Floor", "elevation": 0.0, "height": 3000,
            "components": [
                {"category": "IfcWall", "name": "North Wall", "material": "Brick",
                 "pos_x": 0, "pos_y": 0, "pos_z": 0, "rot_z": 0,
                 "width_mm": 290, "height_mm": 3000, "length_mm": 10000},
                {"category": "IfcSlab", "name": "Ground Slab", "material": "Concrete",
                 "pos_x": 0, "pos_y": 0, "pos_z": 0, "rot_z": 0,
                 "width_mm": 200, "height_mm": 200, "length_mm": 10000}
            ]
        }],
        "metadata": {"location": "Test City", "estimated_cost_usd": 100000}
    }
    generate_ifc(test_spec, "test_output.ifc")
