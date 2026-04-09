import sys
import os
import argparse
from extractor.strip import extract
from extractor.graph_builder import build_graph
from extractor.populate_dimensions import populate_dimensions
from extractor.spatial_analyzer import analyze
from reconstruct import reconstruct
 
STEPS = {
    1: "Extracting components from IFC file",
    2: "Building Neo4j graph",
    3: "Populating dimensions",
    4: "Analyzing spatial relationships",
    5: "Reconstructing IFC file"
}
 
def run_pipeline(filepath, start_from=1, project_id=None, skip_reconstruct=False):
    print("=" * 50)
    print("BIM COMPONENT STRIPPER")
    print("=" * 50)
 
    if start_from > 1 and project_id:
        print(f"\nResuming from step {start_from}: {STEPS[start_from]}")
        print(f"Project id: {project_id}")
 
    if start_from <= 1:
        print(f"\n[1/5] {STEPS[1]}...")
        project_id = extract(filepath)
        print(f"  Project id: {project_id}")
 
    if start_from <= 2:
        print(f"\n[2/5] {STEPS[2]}...")
        build_graph(project_id=project_id)
 
    if start_from <= 3:
        print(f"\n[3/5] {STEPS[3]}...")
        populate_dimensions(project_id=project_id)
 
    if start_from <= 4:
        print(f"\n[4/5] {STEPS[4]}...")
        analyze(project_id=project_id)
 
    if start_from <= 5 and not skip_reconstruct:
        print(f"\n[5/5] {STEPS[5]}...")
        output_path = reconstruct(project_id)
        print(f"  Output: {output_path}")
 
    print("\n" + "=" * 50)
    print(f"Pipeline complete. Project id: {project_id}")
    print("=" * 50)
 
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BIM Component Stripper Pipeline")
    parser.add_argument("filepath", nargs="?", help="Path to IFC file")
    parser.add_argument(
        "--from",
        dest="start_from",
        type=int,
        choices=[1, 2, 3, 4, 5],
        default=1,
        help="Step to resume from (1=strip, 2=graph, 3=dimensions, 4=spatial, 5=reconstruct)"
    )
    parser.add_argument(
        "--project",
        dest="project_id",
        type=int,
        help="Project id to resume from (required when using --from 2-5)"
    )
    parser.add_argument(
        "--skip-reconstruct",
        dest="skip_reconstruct",
        action="store_true",
        help="Skip the reconstruction step"
    )
 
    args = parser.parse_args()
 
    if args.start_from == 1 and not args.filepath:
        print("Error: filepath is required when starting from step 1")
        print("Usage: python3 run.py path/to/file.ifc")
        print("       python3 run.py path/to/file.ifc --from 2 --project 3")
        sys.exit(1)
 
    if args.start_from > 1 and not args.project_id:
        print(f"Error: --project is required when resuming from step {args.start_from}")
        print(f"Usage: python3 run.py --from {args.start_from} --project YOUR_PROJECT_ID")
        sys.exit(1)
 
    if args.filepath and not os.path.exists(args.filepath):
        print(f"Error: File not found: {args.filepath}")
        sys.exit(1)
 
    run_pipeline(
        args.filepath,
        start_from=args.start_from,
        project_id=args.project_id,
        skip_reconstruct=args.skip_reconstruct
    )
