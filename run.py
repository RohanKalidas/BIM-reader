import sys
import os
import argparse
from extractor.strip import extract
from extractor.graph_builder import build_graph
from extractor.populate_dimensions import populate_dimensions
from extractor.spatial_analyzer import analyze

STEPS = {
    1: "Extracting components from IFC file",
    2: "Building Neo4j graph",
    3: "Populating dimensions",
    4: "Analyzing spatial relationships"
}

def run_pipeline(filepath, start_from=1):
    print("=" * 50)
    print("BIM COMPONENT STRIPPER")
    print("=" * 50)

    if start_from > 1:
        print(f"\nResuming from step {start_from}: {STEPS[start_from]}")

    if start_from <= 1:
        print(f"\n[1/4] {STEPS[1]}...")
        extract(filepath)

    if start_from <= 2:
        print(f"\n[2/4] {STEPS[2]}...")
        build_graph()

    if start_from <= 3:
        print(f"\n[3/4] {STEPS[3]}...")
        populate_dimensions()

    if start_from <= 4:
        print(f"\n[4/4] {STEPS[4]}...")
        analyze()

    print("\n" + "=" * 50)
    print("Pipeline complete.")
    print("=" * 50)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BIM Component Stripper Pipeline")
    parser.add_argument("filepath", nargs="?", help="Path to IFC file")
    parser.add_argument(
        "--from",
        dest="start_from",
        type=int,
        choices=[1, 2, 3, 4],
        default=1,
        help="Step to resume from (1=strip, 2=graph, 3=dimensions, 4=spatial)"
    )

    args = parser.parse_args()

    # If resuming from step 2+ filepath is optional
    if args.start_from == 1 and not args.filepath:
        print("Error: filepath is required when starting from step 1")
        print("Usage: python3 run.py path/to/file.ifc")
        print("       python3 run.py path/to/file.ifc --from 2")
        print("       python3 run.py --from 3")
        sys.exit(1)

    if args.filepath and not os.path.exists(args.filepath):
        print(f"Error: File not found: {args.filepath}")
        sys.exit(1)

    run_pipeline(args.filepath, start_from=args.start_from)
