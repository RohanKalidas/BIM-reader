"""
cli.py — Command-line entry point for the multi-agent pipeline.

Usage:

    # Generate a building from a prompt (full pipeline, agents lay out rooms)
    python cli.py generate "Victorian cottage in New England, deep green siding, turret"

    # Generate from a pre-built layout JSON (skips the Layout Agent)
    python cli.py from-layout layout.json --style "modern" --render out.ifc

    # Generate + render to IFC (requires ifcopenshell)
    python cli.py generate "..." --render out.ifc

    # Run an edit on a prior generation (requires --load to point at a JSON dump)
    python cli.py edit --load prior.json --target facade "Make it brick instead"

    # Dump intermediate agent outputs to JSON
    python cli.py generate "..." --dump output.json

    # A/B test — run the same prompt through monolithic (if available) + multi-agent
    python cli.py compare "..." --monolithic-spec path/to/current-output.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _render_to_ifc(spec_dict: dict, output_path: str) -> None:
    """
    Call generate.py's generate_ifc() with the merged spec.

    Expects the user's BIM Studio path on sys.path so `import generate` works.
    """
    try:
        from generate import generate_ifc  # type: ignore
    except ImportError:
        print(
            "ERROR: could not import generate.py. Run from the BIM-reader root,\n"
            "or add the BIM-reader directory to PYTHONPATH:\n"
            "  export PYTHONPATH=/path/to/BIM-reader:$PYTHONPATH",
            file=sys.stderr,
        )
        sys.exit(2)
    generate_ifc(spec_dict, output_path)
    print(f"→ IFC written to {output_path}")


def _dump_result(result, path: str) -> None:
    """Save the PipelineResult to a JSON file for later loading or debugging."""
    payload = {
        "spec": result.spec.model_dump(),
        "brief": result.brief.model_dump(),
        "layout": result.layout.model_dump(),
        "facade": result.facade.model_dump(),
        "mep": result.mep.model_dump(),
        "runs": [r.model_dump() for r in result.runs],
        "total_duration_s": result.total_duration_s,
    }
    Path(path).write_text(json.dumps(payload, indent=2))
    print(f"→ Full result dumped to {path}")


def _load_result(path: str):
    """Load a prior PipelineResult from disk."""
    from .schemas import AgentRun, Brief, Facade, Layout, MEPStrategy, PipelineResult
    payload = json.loads(Path(path).read_text())
    # Rebuild spec from parts instead of trusting saved spec
    from .orchestrator import merge_to_spec
    brief = Brief(**payload["brief"])
    layout = Layout(**payload["layout"])
    facade = Facade(**payload["facade"])
    mep = MEPStrategy(**payload["mep"])
    spec = merge_to_spec(brief, layout, facade, mep)
    runs = [AgentRun(**r) for r in payload["runs"]]
    return PipelineResult(
        spec=spec, brief=brief, layout=layout, facade=facade, mep=mep,
        runs=runs, total_duration_s=payload["total_duration_s"],
    )


def _load_layout(path: str):
    """Load and validate a Layout JSON file from disk."""
    from .schemas import Layout
    payload = json.loads(Path(path).read_text())
    return Layout(**payload)


def _print_summary(result) -> None:
    print("\n── Pipeline summary ──")
    print(f"  Total time: {result.total_duration_s}s")
    for r in result.runs:
        print(f"  {r.agent:16s}  {r.duration_s:>5.2f}s  "
              f"in={r.input_tokens:>5} out={r.output_tokens:>5}")
    print(f"  Style:      {result.brief.architectural_style}")
    print(f"  Floors:     {len(result.layout.floors)}")
    print(f"  Rooms:      {sum(len(f.rooms) for f in result.layout.floors)}")
    print(f"  Footprint:  {result.layout.footprint_width:.1f}m × {result.layout.footprint_depth:.1f}m")
    print(f"  Facade features: {len(result.facade.exterior_features)}")
    for feat in result.facade.exterior_features:
        params = feat.model_dump()
        params.pop("type", None)
        short = ", ".join(f"{k}={v}" for k, v in list(params.items())[:3])
        print(f"    - {feat.type}: {short}")
    print(f"  HVAC:       {result.mep.hvac_type}, {result.mep.hvac_zones} zones")
    print(f"  Palette:    {result.spec.metadata['style_palette']}")
    print()


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_generate(args: argparse.Namespace) -> int:
    from .orchestrator import generate_building_multi_agent
    t0 = time.time()
    result = generate_building_multi_agent(
        args.prompt,
        parallel_specialists=not args.sequential,
    )
    _print_summary(result)
    if args.dump:
        _dump_result(result, args.dump)
    if args.render:
        _render_to_ifc(result.spec.model_dump(), args.render)
    return 0


def cmd_from_layout(args: argparse.Namespace) -> int:
    """Run the MAS pipeline starting from a user-supplied Layout JSON file."""
    from .orchestrator import generate_building_from_layout

    layout = _load_layout(args.layout_path)
    print(f"Loaded layout: {len(layout.floors)} floor(s), "
          f"{sum(len(f.rooms) for f in layout.floors)} room(s), "
          f"{layout.footprint_width:.1f}m × {layout.footprint_depth:.1f}m")

    result = generate_building_from_layout(
        layout,
        style_hint=args.style or "",
        name=args.name,
        front_elevation=args.front_elevation,
        location=args.location,
        parallel_specialists=not args.sequential,
    )
    _print_summary(result)
    if args.dump:
        _dump_result(result, args.dump)
    if args.render:
        _render_to_ifc(result.spec.model_dump(), args.render)
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    from .orchestrator import edit_building
    prev = _load_result(args.load)
    print(f"\nLoaded prior result ({prev.total_duration_s}s). Running edit on {args.target}...\n")
    result = edit_building(prev, args.edit_request, target=args.target)
    _print_summary(result)
    if args.dump:
        _dump_result(result, args.dump)
    if args.render:
        _render_to_ifc(result.spec.model_dump(), args.render)

    # Show which runs were new vs cached (by timing)
    print("── Cache-vs-fresh breakdown ──")
    prev_durations = {r.agent: r.duration_s for r in prev.runs}
    for r in result.runs:
        prev_d = prev_durations.get(r.agent)
        if prev_d is not None and abs(r.duration_s - prev_d) < 0.01:
            print(f"  {r.agent:16s}  CACHED  ({r.duration_s}s)")
        else:
            print(f"  {r.agent:16s}  FRESH   ({r.duration_s}s)")
    return 0


# ── Main ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BIM Studio multi-agent pipeline")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="Generate a building from a prompt")
    g.add_argument("prompt", help="User prompt, e.g. 'Victorian cottage...'")
    g.add_argument("--render", metavar="IFC_PATH", help="Render to IFC at this path")
    g.add_argument("--dump",   metavar="JSON_PATH", help="Save full result to JSON")
    g.add_argument("--sequential", action="store_true",
                   help="Run facade+MEP sequentially instead of in parallel")
    g.set_defaults(func=cmd_generate)

    fl = sub.add_parser("from-layout",
                        help="Generate a building from a pre-supplied Layout JSON")
    fl.add_argument("layout_path", help="Path to a Layout JSON file")
    fl.add_argument("--style", default="",
                    help="Style hint (e.g. 'modern', 'victorian'). Empty = let agent guess.")
    fl.add_argument("--name", default="Building", help="Building name")
    fl.add_argument("--front-elevation", default="south",
                    choices=["north", "south", "east", "west"],
                    help="Which side has the main entry (default: south)")
    fl.add_argument("--location", default=None, help="City/region (helps MEP via climate)")
    fl.add_argument("--render", metavar="IFC_PATH", help="Render to IFC at this path")
    fl.add_argument("--dump",   metavar="JSON_PATH", help="Save full result to JSON")
    fl.add_argument("--sequential", action="store_true",
                    help="Run facade+MEP sequentially instead of in parallel")
    fl.set_defaults(func=cmd_from_layout)

    e = sub.add_parser("edit", help="Edit a prior generation")
    e.add_argument("--load",   required=True, metavar="JSON_PATH", help="Prior result JSON")
    e.add_argument("--target", required=True, choices=["brief", "layout", "facade", "mep"])
    e.add_argument("edit_request", help="What to change, e.g. 'Make it brick'")
    e.add_argument("--render", metavar="IFC_PATH")
    e.add_argument("--dump",   metavar="JSON_PATH")
    e.set_defaults(func=cmd_edit)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
