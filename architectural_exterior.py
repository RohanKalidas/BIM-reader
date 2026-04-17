"""
architectural_exterior.py — Exterior accent dispatcher.

BIM Studio v11 approach: the AI emits a list of feature dicts in
metadata.exterior_features, and this module dispatches each one to a
primitive builder in exterior_primitives.py.

Backwards compatible: if metadata contains no exterior_features but has
a legacy architectural_style like "victorian" or "farmhouse", we fall back
to a small set of default features per style so old specs still render
with some character.
"""

from typing import Any, Callable, Dict, List

import logging

logger = logging.getLogger(__name__)


def _style_key(metadata: Dict[str, Any]) -> str:
    return str(
        metadata.get("architectural_style")
        or metadata.get("exterior_style")
        or metadata.get("style")
        or ""
    ).lower().strip()


# When the AI doesn't emit exterior_features, these defaults give a building
# some character based on its declared style. Claude should almost always
# emit features itself — these are a safety net, not the main path.
_DEFAULT_FEATURES_BY_STYLE: Dict[str, List[Dict[str, Any]]] = {
    "victorian": [
        {"type": "turret",    "corner": "sw", "radius": 1.3, "cap": "conical", "spire": True},
        {"type": "porch",     "sides": ["south"], "depth": 1.8, "column_style": "turned", "has_roof": True},
        {"type": "gable",     "side": "south", "height": 1.8, "width": 2.6},
        {"type": "dormer",    "side": "south", "position": 0.3},
    ],
    "tudor": [
        {"type": "half_timber_band", "side": "south", "count": 6},
        {"type": "gable",            "side": "south", "height": 2.0, "width": 3.0},
        {"type": "chimney",          "height": 5.0},
    ],
    "farmhouse": [
        {"type": "porch",    "sides": ["south"], "depth": 2.0, "column_style": "square",
                             "column_color_key": "accent", "has_roof": True},
        {"type": "gable",    "side": "south", "height": 1.6, "width": 2.8, "color_key": "trim"},
    ],
    "cape_cod": [
        {"type": "dormer",  "side": "south", "position": 0.3},
        {"type": "dormer",  "side": "south", "position": 0.7},
        {"type": "shutter", "side": "south", "position": 0.2},
        {"type": "shutter", "side": "south", "position": 0.5},
        {"type": "shutter", "side": "south", "position": 0.8},
    ],
    "colonial": [
        {"type": "portico", "side": "south", "width": 3.5, "column_count": 4, "pediment": True},
        {"type": "shutter", "side": "south", "position": 0.25},
        {"type": "shutter", "side": "south", "position": 0.75},
    ],
    "neoclassical": [
        {"type": "portico", "side": "south", "width": 5.5, "column_count": 6, "pediment": True, "projection": 2.2},
    ],
    "craftsman": [
        {"type": "porch", "sides": ["south"], "depth": 1.8, "column_style": "square",
                          "column_size": 0.28, "has_roof": True},
        {"type": "gable", "side": "south", "height": 1.3, "width": 3.8, "color_key": "roof"},
    ],
    "ranch": [
        {"type": "porch",  "sides": ["south"], "depth": 1.4, "column_style": "square", "has_roof": True},
        {"type": "canopy", "side": "south", "position": 0.7, "width": 2.0, "projection": 0.9},
    ],
    "modern": [
        {"type": "canopy",  "side": "south", "position": 0.5, "width": 3.5, "projection": 1.4},
        {"type": "parapet", "height": 0.8},
    ],
    "contemporary": [
        {"type": "canopy",  "side": "south", "position": 0.5, "width": 3.5, "projection": 1.4},
        {"type": "parapet", "height": 0.8},
    ],
    "mid_century_modern": [
        {"type": "canopy",       "side": "south", "width": 4.5, "projection": 1.8},
        {"type": "vertical_fin", "side": "south", "count": 5, "depth": 0.4},
    ],
    "industrial": [
        {"type": "parapet", "height": 1.2, "stepped": False},
    ],
    "mediterranean": [
        {"type": "parapet", "height": 0.7},
        {"type": "awning",  "side": "south", "position": 0.5, "width": 1.8},
    ],
    "spanish_revival": [
        {"type": "parapet", "height": 0.7},
        {"type": "awning",  "side": "south", "position": 0.3, "width": 1.6},
        {"type": "awning",  "side": "south", "position": 0.7, "width": 1.6},
    ],
    "commercial_office": [
        {"type": "parapet", "height": 1.0},
        {"type": "canopy",  "side": "south", "width": 4.5, "projection": 1.5},
    ],
    "warehouse": [
        {"type": "parapet", "height": 0.9},
    ],
}


def _resolve_default_features(style: str) -> List[Dict[str, Any]]:
    """Look up default features, using styles.resolve_style aliases if available."""
    if not style:
        return []
    try:
        from styles import resolve_style
        key = resolve_style(style)
    except Exception:
        key = style.strip().lower().replace("-", "_").replace(" ", "_")
    return _DEFAULT_FEATURES_BY_STYLE.get(key, [])


def build_exterior_accents(
    m, body, storey,
    min_x: float, min_y: float, max_x: float, max_y: float,
    elev: float, ceil_h: float,
    metadata: Dict[str, Any],
    color_rep: Callable,
    place_element: Callable,
    box_rep: Callable,
) -> None:
    """
    Main entry point. Signature unchanged from v10 so generate.py can keep
    calling us the same way.

    Priority order:
    1. metadata.exterior_features explicitly set by the AI
    2. default features for metadata.architectural_style
    3. nothing (plain box)
    """
    w = max_x - min_x
    d = max_y - min_y
    if w < 2 or d < 2:
        return

    bounds = (min_x, min_y, max_x, max_y)

    try:
        from exterior_primitives import build_exterior_features
    except ImportError as e:
        logger.warning("exterior_primitives not importable: %s", e)
        return

    features = metadata.get("exterior_features")
    source = "AI-emitted"

    if not features:
        style = _style_key(metadata)
        features = _resolve_default_features(style)
        source = f"default-for-{style}" if style else "none"

    if not features:
        logger.info("No exterior features to build (style=%r).", _style_key(metadata))
        return

    logger.info("Building %d exterior features (%s)", len(features), source)
    build_exterior_features(
        m, body, storey, bounds, elev, ceil_h, features,
        box_rep=box_rep, place_element=place_element, color_rep=color_rep,
    )
