"""
backfill.py — One-time classification of existing library components.

Walks the components table, calls the classifier for each unclassified
row, writes the four new fields back. Resumable: classified_at gets set
when each row succeeds, so a re-run only picks up the unclassified ones.

Usage:
    cd ~/BIM-reader
    python -m bim_multi_agent.backfill                    # all components
    python -m bim_multi_agent.backfill --limit 100        # first 100 (test)
    python -m bim_multi_agent.backfill --library-only     # only components in library
    python -m bim_multi_agent.backfill --workers 8        # parallelism

Cost estimate at default settings:
    21,902 components × Haiku ($0.0003/call) ≈ $7
    With 8 parallel workers: ~30 min wall time
    Single-threaded: ~3-4 hours
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2.extras

# These imports assume backfill.py lives in bim_multi_agent/ alongside
# canonical_vocab.py and classifier.py. Adjust if you put the package
# elsewhere.
from .canonical_vocab import validate_classification
from .classifier import classify_component_smart

# Use the project's existing DB connection helper.
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.db import get_db_connection  # noqa: E402

logger = logging.getLogger(__name__)

# ── Categories worth classifying ──────────────────────────────────────────
# These are component types that actually function as discrete fixtures
# the AI would request when generating a building. Walls, slabs, beams,
# proxies, etc. are structural/shell — no canonical name applies.
 
TARGET_CATEGORIES = (
    # Furniture
    "IfcFurniture",
    "IfcFurnishingElement",
    # Plumbing fixtures
    "IfcSanitaryTerminal",
    # Appliances
    "IfcElectricAppliance",
    # Lighting
    "IfcLightFixture",
    # Openings
    "IfcDoor",
    "IfcWindow",
    # Electrical
    "IfcOutlet",
    "IfcSwitchingDevice",
    "IfcCableSegment",
    "IfcCableCarrierSegment",
    "IfcElectricDistributionBoard",
    "IfcElectricFlowStorageDevice",
    # HVAC terminals + equipment
    "IfcAirTerminal",
    "IfcAirTerminalBox",
    "IfcDuctSegment",
    "IfcDuctFitting",
    "IfcUnitaryEquipment",
    "IfcBoiler",
    "IfcChiller",
    "IfcFan",
    # Plumbing distribution
    "IfcPipeSegment",
    "IfcPipeFitting",
    "IfcPump",
    "IfcValve",
    "IfcFlowMeter",
    "IfcTank",
    # Fire
    "IfcFireSuppressionTerminal",
    "IfcAlarm",
    # Medical
    "IfcMedicalDevice",
    # Circulation
    "IfcStair",
    "IfcStairFlight",
    "IfcRailing",
    # Finishes
    "IfcCovering",
    "IfcCurtainWall",
)

# ── Single-row classification + write ─────────────────────────────────────

def _classify_and_persist(row: dict) -> tuple[int, bool, str]:
    """
    Classify one component, write results back. Returns:
        (component_id, success, error_message_or_canonical_name)
    """
    cid = row["id"]
    try:
        result = classify_component_smart(
            ifc_class=row["category"],
            family_name=row.get("family_name") or "",
            type_name=row.get("type_name") or "",
            width_mm=row.get("width_mm"),
            height_mm=row.get("height_mm"),
            length_mm=row.get("length_mm"),
        )

        ok, err = validate_classification(
            result["canonical_name"],
            result["style_tags"],
            result["context_tags"],
            result["quality_class"],
        )
        if not ok:
            return cid, False, f"validation failed: {err}"

        with get_db_connection() as (conn, cursor):
            cursor.execute("""
                UPDATE components
                SET canonical_name = %s,
                    style_tags     = %s,
                    context_tags   = %s,
                    quality_class  = %s,
                    classified_at  = NOW()
                WHERE id = %s
            """, (
                result["canonical_name"],
                result["style_tags"],
                result["context_tags"],
                result["quality_class"],
                cid,
            ))

        return cid, True, result["canonical_name"]
    except Exception as e:
        return cid, False, str(e)


# ── Main backfill loop ────────────────────────────────────────────────────

def run_backfill(limit: int | None = None,
                 library_only: bool = False,
                 workers: int = 8,
                 batch_size: int = 200,
                 reclassify: bool = False) -> dict:
    """
    Classify all unclassified components.

    Args:
        limit: stop after this many (for testing)
        library_only: only components actually in the library table
        workers: parallel classifier calls (each is a thread)
        batch_size: rows per DB fetch
        reclassify: if True, redo even already-classified rows

    Returns: dict with success/error counts.
    """
    base_query = """
        SELECT c.id, c.category, c.family_name, c.type_name,
               c.width_mm, c.height_mm, c.length_mm
        FROM components c
    """
     where_clauses = []
    if library_only:
        base_query += " JOIN library l ON l.component_id = c.id "
    if not reclassify:
        where_clauses.append("c.classified_at IS NULL")
    if target_fixtures:
        cat_list = ",".join(f"'{c}'" for c in TARGET_CATEGORIES)
        where_clauses.append(f"c.category IN ({cat_list})")
    if where_clauses:
        base_query += " WHERE " + " AND ".join(where_clauses)
    base_query += " ORDER BY c.id"
    if limit:
        base_query += f" LIMIT {limit}"

    # Pull the whole work list up front.
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute(base_query)
        rows = cursor.fetchall()

    total = len(rows)
    if total == 0:
        print("No components to classify.")
        return {"total": 0, "success": 0, "errors": 0}

    print(f"Backfill: classifying {total:,} components with {workers} workers...")
    if total > 1000:
        est_cost_usd = total * 0.0003
        est_min = total / (workers * 60)  # rough: ~1 sec per call
        print(f"  Estimated cost:  ${est_cost_usd:.2f}")
        print(f"  Estimated time:  ~{est_min:.0f} min")
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return {"total": total, "success": 0, "errors": 0, "cancelled": True}

    started = time.time()
    n_success = 0
    n_error = 0
    errors_sample = []

    # Process in chunks to keep memory reasonable on big libraries.
    for chunk_start in range(0, total, batch_size):
        chunk = rows[chunk_start : chunk_start + batch_size]

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_classify_and_persist, dict(row)): row for row in chunk}
            for fut in as_completed(futures):
                cid, ok, info = fut.result()
                if ok:
                    n_success += 1
                else:
                    n_error += 1
                    if len(errors_sample) < 10:
                        errors_sample.append((cid, info))

        done = chunk_start + len(chunk)
        elapsed = time.time() - started
        rate = done / max(elapsed, 0.1)
        eta_min = (total - done) / max(rate, 0.1) / 60
        print(f"  [{done:>6,}/{total:,}] {n_success:>6} ok, {n_error:>4} err  "
              f"({rate:.1f}/s, ETA {eta_min:.1f} min)")

    elapsed = time.time() - started
    print(f"\nDone in {elapsed/60:.1f} min")
    print(f"  Successful: {n_success:,}")
    print(f"  Errors:     {n_error:,}")
    if errors_sample:
        print("  Sample errors:")
        for cid, msg in errors_sample[:5]:
            print(f"    component {cid}: {msg}")

    return {
        "total": total,
        "success": n_success,
        "errors": n_error,
        "elapsed_seconds": elapsed,
    }


# ── Stats helpers (for sanity-checking the classification) ────────────────

def show_classification_stats():
    """Print breakdown of how the library is now classified."""
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT canonical_name, COUNT(*) as n
            FROM components
            WHERE canonical_name IS NOT NULL
            GROUP BY canonical_name
            ORDER BY n DESC
            LIMIT 30
        """)
        print("\nTop 30 canonical_names:")
        for r in cursor.fetchall():
            print(f"  {r['canonical_name']:<35s} {r['n']:>6,}")

        cursor.execute("""
            SELECT quality_class, COUNT(*) as n
            FROM components
            WHERE quality_class IS NOT NULL
            GROUP BY quality_class
            ORDER BY n DESC
        """)
        print("\nQuality distribution:")
        for r in cursor.fetchall():
            print(f"  {r['quality_class']:<12s} {r['n']:>6,}")

        cursor.execute("""
            SELECT COUNT(*) FILTER (WHERE classified_at IS NOT NULL) AS classified,
                   COUNT(*) AS total
            FROM components
        """)
        r = cursor.fetchone()
        print(f"\nProgress: {r['classified']:,} / {r['total']:,} components classified")


def main():
    p = argparse.ArgumentParser(description="Backfill canonical classifications.")
    p.add_argument("--limit", type=int, help="Process only N components (testing)")
    p.add_argument("--library-only", action="store_true",
                   help="Only components in the saved library, not all uploads")
    p.add_argument("--workers", type=int, default=8,
                   help="Parallel classification workers (default 8)")
    p.add_argument("--batch-size", type=int, default=200,
                   help="DB fetch batch size (default 200)")
    p.add_argument("--reclassify", action="store_true",
                   help="Redo already-classified rows (rare)")
    p.add_argument("--stats", action="store_true",
                   help="Just print classification stats, don't classify")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.stats:
        show_classification_stats()
        return 0

    result = run_backfill(
        limit=args.limit,
        library_only=args.library_only,
        workers=args.workers,
        batch_size=args.batch_size,
        reclassify=args.reclassify,
    )
    if result.get("errors", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
