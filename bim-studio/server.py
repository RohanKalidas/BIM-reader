"""
bim-studio/server.py — Flask web application for BIM Studio.

Uses centralized database module. Adds input validation, secure filenames,
and proper connection management.
"""

import os
import sys

# Add parent directory to path FIRST so database package is findable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import json
import subprocess
import logging
import psycopg2.extras
import anthropic
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, send_file, request, Response, stream_with_context
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from database.db import get_db_connection
from aps_upload import upload_to_aps, get_token
load_dotenv()

# ── Multi-agent pipeline import (with graceful fallback) ─────────────────
# Makes bim_multi_agent/ importable regardless of cwd. BASE_DIR is
# bim-studio/; the package lives one level up in BIM-reader/bim_multi_agent/.
_MULTI_AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _MULTI_AGENT_DIR not in sys.path:
    sys.path.insert(0, _MULTI_AGENT_DIR)

try:
    from bim_multi_agent.orchestrator import generate_building_multi_agent, edit_building
    from bim_multi_agent.agents import (
        run_brief_agent, run_layout_agent, run_facade_agent, run_mep_agent,
    )
    from bim_multi_agent.orchestrator import merge_to_spec as ma_merge_to_spec
    from bim_multi_agent.schemas import PipelineResult as _MA_PipelineResult
    from bim_multi_agent.orchestrator import generate_building_from_layout
    from bim_multi_agent.schemas import Layout as _MA_Layout
    _MULTI_AGENT_AVAILABLE = True
    print("[multi_agent] pipeline loaded successfully")
except Exception as _ma_err:
    print(f"[multi_agent] import failed, /api/generate/multi_agent will 500: {_ma_err}")
    _MULTI_AGENT_AVAILABLE = False

# In-memory cache of the last PipelineResult per building name. Used by
# the edit endpoint to avoid re-running the full pipeline. Persists until
# Flask restart — replace with Redis/DB for production.
_LAST_RESULTS = {}

logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max upload

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
REPO_DIR         = os.path.join(BASE_DIR, "..")
UPLOAD_FOLDER    = os.path.join(REPO_DIR, "uploads")
GENERATED_FOLDER = os.path.join(REPO_DIR, "generated")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)


def safe_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_dimension_m(mm_value, fallback):
    value = safe_float(mm_value)
    if value is None or value <= 0:
        return fallback
    return round(value / 1000.0, 3)


def _find_component_defaults(cursor, categories, terms=(), fallback=None):
    fallback = fallback or {}
    clauses = []
    params = []
    if categories:
        clauses.append("c.category = ANY(%s)")
        params.append(list(categories))
    if terms:
        like_parts = []
        for term in terms:
            like_parts.append(
                "(LOWER(COALESCE(c.family_name,'')) LIKE %s OR LOWER(COALESCE(c.type_name,'')) LIKE %s)"
            )
            params.extend([f"%{term.lower()}%", f"%{term.lower()}%"])
        clauses.append("(" + " OR ".join(like_parts) + ")")

    where_sql = " AND ".join(clauses) if clauses else "TRUE"
    params.append(1)
    query = f"""
        SELECT
            AVG(NULLIF(c.width_mm, 0)) AS width_mm,
            AVG(NULLIF(c.length_mm, 0)) AS length_mm,
            AVG(NULLIF(c.height_mm, 0)) AS height_mm,
            COUNT(*) AS count
        FROM (
            SELECT c.*
            FROM library l
            JOIN components c ON c.id = l.component_id
            WHERE {where_sql}
            UNION ALL
            SELECT c.*
            FROM components c
            JOIN projects p ON p.id = c.project_id
            WHERE p.status = 'done' AND {where_sql}
            LIMIT %s
        ) c
    """
    params = params[:-1] + params[:-1] + [params[-1]]
    try:
        cursor.execute(query, params)
        row = cursor.fetchone()
    except Exception as e:
        logger.debug("Library grounding query failed: %s", e)
        return dict(fallback)

    if not row or not row.get("count"):
        return dict(fallback)

    return {
        "width_m": _safe_dimension_m(row.get("width_mm"), fallback.get("width_m")),
        "depth_m": _safe_dimension_m(row.get("length_mm"), fallback.get("depth_m")),
        "height_m": _safe_dimension_m(row.get("height_mm"), fallback.get("height_m")),
        "sample_count": int(row.get("count") or 0),
    }


def _find_wall_defaults(cursor):
    fallback = {
        "exterior_thickness_m": 0.20,
        "interior_thickness_m": 0.12,
    }
    try:
        cursor.execute("""
            SELECT
                AVG(CASE WHEN LOWER(COALESCE(w.function, '')) LIKE '%external%' THEN NULLIF(w.total_thickness, 0) END) AS ext_mm,
                AVG(CASE WHEN LOWER(COALESCE(w.function, '')) NOT LIKE '%external%' THEN NULLIF(w.total_thickness, 0) END) AS int_mm
            FROM wall_types w
            JOIN components c ON c.id = w.component_id
            JOIN projects p ON p.id = c.project_id
            WHERE p.status = 'done'
        """)
        row = cursor.fetchone()
    except Exception as e:
        logger.debug("Wall grounding query failed: %s", e)
        return fallback

    if not row:
        return fallback

    return {
        "exterior_thickness_m": _safe_dimension_m(row.get("ext_mm"), fallback["exterior_thickness_m"]),
        "interior_thickness_m": _safe_dimension_m(row.get("int_mm"), fallback["interior_thickness_m"]),
    }


def ground_spec_with_library(spec):
    if not isinstance(spec, dict):
        return spec

    grounded = json.loads(json.dumps(spec))
    metadata = grounded.setdefault("metadata", {})

    fixture_queries = {
        "door": {"categories": ["IfcDoor"], "terms": ["door"], "fallback": {"width_m": 0.9, "depth_m": 0.1, "height_m": 2.1}},
        "window": {"categories": ["IfcWindow"], "terms": ["window"], "fallback": {"width_m": 1.4, "depth_m": 0.15, "height_m": 1.2}},
        "toilet": {"categories": ["IfcSanitaryTerminal"], "terms": ["toilet", "wc"], "fallback": {"width_m": 0.38, "depth_m": 0.65, "height_m": 0.45}},
        "sink": {"categories": ["IfcSanitaryTerminal"], "terms": ["sink", "basin"], "fallback": {"width_m": 0.5, "depth_m": 0.4, "height_m": 0.85}},
        "shower tray": {"categories": ["IfcSanitaryTerminal"], "terms": ["shower"], "fallback": {"width_m": 0.9, "depth_m": 0.9, "height_m": 0.1}},
        "counter": {"categories": ["IfcFurniture", "IfcFurnishingElement"], "terms": ["counter", "worktop"], "fallback": {"width_m": 2.0, "depth_m": 0.6, "height_m": 0.9}},
        "stove": {"categories": ["IfcElectricAppliance"], "terms": ["stove", "oven", "range"], "fallback": {"width_m": 0.6, "depth_m": 0.6, "height_m": 0.9}},
        "fridge": {"categories": ["IfcElectricAppliance"], "terms": ["fridge", "refrigerator"], "fallback": {"width_m": 0.7, "depth_m": 0.7, "height_m": 1.8}},
        "bed": {"categories": ["IfcFurniture"], "terms": ["bed"], "fallback": {"width_m": 1.6, "depth_m": 2.0, "height_m": 0.5}},
        "wardrobe": {"categories": ["IfcFurniture"], "terms": ["wardrobe", "closet"], "fallback": {"width_m": 1.2, "depth_m": 0.6, "height_m": 2.1}},
        "nightstand": {"categories": ["IfcFurniture"], "terms": ["nightstand", "side table"], "fallback": {"width_m": 0.45, "depth_m": 0.4, "height_m": 0.5}},
        "sofa": {"categories": ["IfcFurniture"], "terms": ["sofa", "couch"], "fallback": {"width_m": 2.2, "depth_m": 0.9, "height_m": 0.8}},
        "coffee table": {"categories": ["IfcFurniture"], "terms": ["coffee table"], "fallback": {"width_m": 1.0, "depth_m": 0.55, "height_m": 0.42}},
        "tv unit": {"categories": ["IfcFurniture", "IfcFurnishingElement"], "terms": ["tv", "media"], "fallback": {"width_m": 1.5, "depth_m": 0.4, "height_m": 0.5}},
        "table": {"categories": ["IfcFurniture"], "terms": ["table", "desk"], "fallback": {"width_m": 1.6, "depth_m": 0.9, "height_m": 0.75}},
        "chair": {"categories": ["IfcFurniture"], "terms": ["chair", "seat"], "fallback": {"width_m": 0.45, "depth_m": 0.45, "height_m": 0.85}},
        "desk": {"categories": ["IfcFurniture"], "terms": ["desk"], "fallback": {"width_m": 1.4, "depth_m": 0.7, "height_m": 0.75}},
        "rack": {"categories": ["IfcElectricAppliance"], "terms": ["rack", "server"], "fallback": {"width_m": 0.6, "depth_m": 0.8, "height_m": 2.0}},
        "heater": {"categories": ["IfcElectricAppliance"], "terms": ["heater", "boiler"], "fallback": {"width_m": 0.55, "depth_m": 0.55, "height_m": 1.5}},
        "washer": {"categories": ["IfcElectricAppliance"], "terms": ["washer", "washing machine"], "fallback": {"width_m": 0.6, "depth_m": 0.6, "height_m": 0.9}},
    }

    grounding = {
        "source": "library+database",
        "wall_defaults": {
            "exterior_thickness_m": 0.20,
            "interior_thickness_m": 0.12,
        },
        "openings": {
            "door_width": 0.9,
            "door_height": 2.1,
            "window_width": 1.4,
            "window_height": 1.2,
        },
        "fixtures": {},
    }

    try:
        with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
            grounding["wall_defaults"] = _find_wall_defaults(cursor)
            door_dims = _find_component_defaults(cursor, ["IfcDoor"], ["door"], fixture_queries["door"]["fallback"])
            window_dims = _find_component_defaults(cursor, ["IfcWindow"], ["window"], fixture_queries["window"]["fallback"])
            grounding["openings"] = {
                "door_width": float(door_dims.get("width_m") or 0.9),
                "door_height": float(door_dims.get("height_m") or 2.1),
                "window_width": float(window_dims.get("width_m") or 1.4),
                "window_height": float(window_dims.get("height_m") or 1.2),
            }
            for name, query in fixture_queries.items():
                grounding["fixtures"][name] = _find_component_defaults(
                    cursor, query["categories"], query["terms"], query["fallback"]
                )
    except Exception as e:
        logger.debug("Spec grounding failed: %s", e)

    metadata["grounding"] = grounding
    return grounded


def run_pipeline(filepath):
    """Run pipeline on a single IFC file. Returns (project_id, stats, cached)."""
    filename = os.path.basename(filepath)

    # Check if already processed
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT id FROM projects WHERE filename = %s AND status = 'done' LIMIT 1", (filename,))
        existing = cursor.fetchone()
        if existing:
            project_id = existing["id"]
            cursor.execute("SELECT COUNT(*) as total FROM components WHERE project_id = %s", (project_id,))
            total = cursor.fetchone()["total"]
            cursor.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id = %s", (project_id,))
            rels = cursor.fetchone()["total"]
            print(f"Already processed: {filename} (project_id={project_id})")
            return project_id, {"components": total, "relationships": rels}, True

    result = subprocess.run(
        ["python3", "run.py", filepath],
        capture_output=True, text=True, timeout=300, cwd=REPO_DIR)
    print("STDOUT:", result.stdout[-500:])
    if result.returncode != 0:
        raise Exception(f"Pipeline failed: {result.stderr[:300]}")

    project_id = None
    for line in result.stdout.splitlines():
        if "Project id:" in line:
            try:
                project_id = int(line.split("Project id:")[-1].strip())
                break
            except ValueError:
                pass

    if not project_id:
        with get_db_connection() as (conn, cursor):
            cursor.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                project_id = row[0]

    if not project_id:
        raise Exception("Could not determine project_id")

    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT COUNT(*) as total FROM components WHERE project_id=%s", (project_id,))
        total = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id=%s", (project_id,))
        rels = cursor.fetchone()["total"]

    return project_id, {"components": total, "relationships": rels}, False


# ── Static ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── APS Token ───────────────────────────────────────────────────────────────
@app.route("/api/aps/token")
def aps_token():
    try:
        return jsonify({"access_token": get_token(), "expires_in": 3600})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Single IFC Upload ────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload_ifc():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".ifc"):
        return jsonify({"error": "Only .ifc files accepted"}), 400

    # Sanitize filename to prevent path traversal
    safe_name = secure_filename(f.filename)
    if not safe_name.lower().endswith(".ifc"):
        return jsonify({"error": "Invalid filename"}), 400

    filepath = os.path.join(UPLOAD_FOLDER, safe_name)
    f.save(filepath)
    print(f"Saved IFC to {filepath}")

    try:
        project_id, stats, cached = run_pipeline(filepath)
        print(f"Pipeline done. project_id={project_id} cached={cached}")

        # If cached, reuse existing APS URN
        if cached:
            with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
                cursor.execute("SELECT aps_urn FROM projects WHERE id=%s", (project_id,))
                row = cursor.fetchone()
                if row and row.get("aps_urn"):
                    return jsonify({"status": "done", "cached": True, "project_id": project_id,
                                    "aps": {"urn": row["aps_urn"]}, "stats": stats})

        aps_result = {"urn": None}
        try:
            aps_result = upload_to_aps(filepath)
            with get_db_connection() as (conn, cursor):
                cursor.execute("UPDATE projects SET aps_urn=%s WHERE id=%s", (aps_result["urn"], project_id))
        except Exception as aps_err:
            print(f"APS upload failed (non-fatal): {aps_err}")

        return jsonify({"status": "done", "project_id": project_id, "aps": aps_result, "stats": stats})
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# ── Bulk IFC Upload ──────────────────────────────────────────────────────────
@app.route("/api/upload/bulk", methods=["POST"])
def upload_bulk():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    ifc_files = []
    for f in files:
        if f.filename and f.filename.lower().endswith(".ifc"):
            safe_name = secure_filename(f.filename)
            if safe_name.lower().endswith(".ifc"):
                ifc_files.append((f, safe_name))

    if not ifc_files:
        return jsonify({"error": "No valid .ifc files found"}), 400

    # Save all files first
    saved = []
    for f, safe_name in ifc_files:
        fp = os.path.join(UPLOAD_FOLDER, safe_name)
        f.save(fp)
        saved.append((safe_name, fp))

    def stream():
        total = len(saved)
        processed = 0
        failed = 0
        total_components = 0

        for idx, (filename, filepath) in enumerate(saved):
            yield f"data: {json.dumps({'type':'start','file':filename,'index':idx,'total':total})}\n\n"
            try:
                yield f"data: {json.dumps({'type':'step','file':filename,'step':'pipeline','message':'Running pipeline...'})}\n\n"
                project_id, stats, cached = run_pipeline(filepath)
                total_components += stats["components"]

                if cached:
                    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
                        cursor.execute("SELECT aps_urn FROM projects WHERE id=%s", (project_id,))
                        row = cursor.fetchone()
                        aps_result = {"urn": row["aps_urn"]} if row and row.get("aps_urn") else {}
                    processed += 1
                    yield f"data: {json.dumps({'type':'done','file':filename,'project_id':project_id,'stats':stats,'aps':aps_result,'cached':True})}\n\n"
                else:
                    aps_result = {}
                    try:
                        aps_result = upload_to_aps(filepath)
                        with get_db_connection() as (conn, cursor):
                            cursor.execute("UPDATE projects SET aps_urn=%s WHERE id=%s", (aps_result["urn"], project_id))
                    except Exception as aps_err:
                        print(f"APS upload failed (non-fatal): {aps_err}")
                    processed += 1
                    yield f"data: {json.dumps({'type':'done','file':filename,'project_id':project_id,'stats':stats,'aps':aps_result})}\n\n"

            except Exception as e:
                import traceback
                print(traceback.format_exc())
                failed += 1
                yield f"data: {json.dumps({'type':'error','file':filename,'error':str(e)})}\n\n"

        yield f"data: {json.dumps({'type':'complete','processed':processed,'failed':failed,'total_components':total_components})}\n\n"

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Projects ────────────────────────────────────────────────────────────────
@app.route("/api/projects")
def get_projects():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT id,name,filename,status,processed_at,aps_urn FROM projects ORDER BY id DESC")
        rows = [dict(r) for r in cursor.fetchall()]
    return jsonify(rows)


@app.route("/api/projects/<int:pid>/components")
def get_components(pid):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""SELECT c.id,c.category,c.family_name,c.type_name,c.revit_id,
            c.width_mm,c.height_mm,c.length_mm,c.area_m2,c.volume_m3,c.quality_score,c.parameters,
            s.pos_x,s.pos_y,s.pos_z,s.rot_x,s.rot_y,s.rot_z,s.bounding_box,s.level,s.elevation
            FROM components c LEFT JOIN spatial_data s ON s.component_id=c.id
            WHERE c.project_id=%s ORDER BY COALESCE(s.pos_z,0),c.category""", (pid,))
        rows = cursor.fetchall()

    out = []
    for r in rows:
        d = dict(r)
        for k in ["pos_x","pos_y","pos_z","rot_x","rot_y","rot_z","elevation",
                   "width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
            d[k] = safe_float(d.get(k))
        out.append(d)
    return jsonify(out)


@app.route("/api/projects/<int:pid>/stats")
def get_stats(pid):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT COUNT(*) as total FROM components WHERE project_id=%s", (pid,))
        total = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id=%s", (pid,))
        rels = cursor.fetchone()["total"]
    return jsonify({"total": total, "relationships": rels})


# ── Component lookup ─────────────────────────────────────────────────────────
@app.route("/api/component/by-revit-id/<revit_id>")
def get_component_by_revit_id(revit_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""SELECT c.id,c.category,c.family_name,c.type_name,c.revit_id,
            c.width_mm,c.height_mm,c.length_mm,c.area_m2,c.volume_m3,c.quality_score,c.parameters,
            s.pos_x,s.pos_y,s.pos_z,s.level,s.elevation,p.name as project_name
            FROM components c LEFT JOIN spatial_data s ON s.component_id=c.id
            JOIN projects p ON p.id=c.project_id WHERE c.revit_id=%s LIMIT 1""", (revit_id,))
        row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Component not found"}), 404
    d = dict(row)
    for k in ["pos_x","pos_y","pos_z","elevation","width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
        d[k] = safe_float(d.get(k))
    return jsonify(d)


# ── Library ──────────────────────────────────────────────────────────────────
@app.route("/api/library", methods=["GET"])
def get_library():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""SELECT c.id,c.category,c.family_name,c.type_name,c.revit_id,
            c.width_mm,c.height_mm,c.length_mm,c.area_m2,c.volume_m3,c.quality_score,
            l.saved_at,l.notes,s.level,p.name as project_name
            FROM library l JOIN components c ON c.id=l.component_id
            LEFT JOIN spatial_data s ON s.component_id=c.id
            JOIN projects p ON p.id=c.project_id ORDER BY l.saved_at DESC""")
        rows = cursor.fetchall()

    out = []
    for r in rows:
        d = dict(r)
        for k in ["width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
            d[k] = safe_float(d.get(k))
        out.append(d)
    return jsonify(out)


@app.route("/api/library/save", methods=["POST"])
def save_to_library():
    data = request.json or {}
    component_id = data.get("component_id")
    revit_id = data.get("revit_id")
    notes = data.get("notes", "")

    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        # Resolve component_id from revit_id if needed
        if not component_id and revit_id:
            cursor.execute("SELECT id FROM components WHERE revit_id=%s LIMIT 1", (revit_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"error": "Not found"}), 404
            component_id = row["id"]

        if not component_id:
            return jsonify({"error": "component_id or revit_id required"}), 400

        # Already in library?
        cursor.execute("SELECT id FROM library WHERE component_id=%s", (component_id,))
        if cursor.fetchone():
            return jsonify({"status": "already_saved", "component_id": component_id})

        # Fingerprint duplicate check
        cursor.execute("""
            SELECT c.category, c.family_name, c.width_mm, c.height_mm, c.length_mm
            FROM components c WHERE c.id = %s
        """, (component_id,))
        comp = cursor.fetchone()
        if comp:
            category    = comp["category"]
            family_name = comp["family_name"] or ""
            w = round(float(comp["width_mm"]))  if comp["width_mm"]  is not None else None
            h = round(float(comp["height_mm"])) if comp["height_mm"] is not None else None
            l = round(float(comp["length_mm"])) if comp["length_mm"] is not None else None

            cursor.execute("""
                SELECT l.id FROM library l
                JOIN components c2 ON c2.id = l.component_id
                WHERE c2.category = %s
                  AND COALESCE(c2.family_name, '') = %s
                  AND ((%s IS NULL AND c2.width_mm  IS NULL) OR ROUND(c2.width_mm::numeric)  = %s)
                  AND ((%s IS NULL AND c2.height_mm IS NULL) OR ROUND(c2.height_mm::numeric) = %s)
                  AND ((%s IS NULL AND c2.length_mm IS NULL) OR ROUND(c2.length_mm::numeric) = %s)
                LIMIT 1
            """, (category, family_name, w, w, h, h, l, l))
            if cursor.fetchone():
                return jsonify({"status": "duplicate", "component_id": component_id,
                                "message": "A component with identical characteristics already exists in the library"})

        cursor.execute("INSERT INTO library (component_id,notes) VALUES (%s,%s) RETURNING id", (component_id, notes))
        lib_id = cursor.fetchone()["id"]

    return jsonify({"status": "saved", "library_id": lib_id, "component_id": component_id})


@app.route("/api/library/remove", methods=["POST"])
def remove_from_library():
    data = request.json or {}
    component_id = data.get("component_id")
    if not component_id:
        return jsonify({"error": "component_id required"}), 400
    with get_db_connection() as (conn, cursor):
        cursor.execute("DELETE FROM library WHERE component_id=%s", (component_id,))
    return jsonify({"status": "removed"})


@app.route("/api/library/clear", methods=["POST"])
def clear_library():
    with get_db_connection() as (conn, cursor):
        cursor.execute("DELETE FROM library")
    return jsonify({"status": "cleared"})


# ── Reconstruct ──────────────────────────────────────────────────────────────
@app.route("/api/reconstruct", methods=["POST"])
def reconstruct_endpoint():
    data = request.json or {}
    project_id = data.get("project_id")
    if not project_id:
        return jsonify({"error": "project_id required"}), 400
    try:
        result = subprocess.run(["python3", "reconstruct.py", str(project_id)],
            capture_output=True, text=True, timeout=120, cwd=REPO_DIR)
        if result.returncode != 0:
            return jsonify({"error": result.stderr}), 500
        output_file = None
        for line in result.stdout.splitlines():
            if "Output:" in line:
                output_file = line.split("Output:")[-1].strip()
        return jsonify({"status": "done", "output": output_file, "log": result.stdout})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out"}), 500


# ── Library tool functions (called by AI during generation) ──────────────────

def library_get_categories():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT c.category, COUNT(*) as count
            FROM library l JOIN components c ON c.id = l.component_id
            GROUP BY c.category ORDER BY count DESC
        """)
        return [{"category": r["category"], "count": r["count"]} for r in cursor.fetchall()]


SEARCH_SYNONYMS = {
    "toilet":    ["toilet","wc","water closet","sanitary","lavatory","commode"],
    "wc":        ["wc","toilet","water closet","lavatory"],
    "sink":      ["sink","basin","washbasin","lavatory","wash hand"],
    "bath":      ["bath","bathtub","tub","shower"],
    "shower":    ["shower","bath","tub"],
    "fridge":    ["fridge","refrigerator","refrigeration"],
    "refrigerator":["refrigerator","fridge","refrigeration"],
    "sofa":      ["sofa","couch","settee","lounge"],
    "couch":     ["couch","sofa","settee"],
    "table":     ["table","desk","worktop","counter"],
    "chair":     ["chair","seat","stool"],
    "bed":       ["bed","bunk","mattress"],
    "wardrobe":  ["wardrobe","closet","cupboard","cabinet"],
    "door":      ["door","entry","entrance"],
    "window":    ["window","glazing","glass"],
    "light":     ["light","lamp","luminaire","fixture","downlight"],
    "duct":      ["duct","ducting","hvac","air","vent"],
    "pipe":      ["pipe","piping","plumbing","water","drain"],
    "boiler":    ["boiler","heater","water heater","hot water"],
    "radiator":  ["radiator","heater","panel"],
    "fan":       ["fan","ventilator","extract"],
    "stove":     ["stove","oven","cooker","hob","range"],
    "microwave": ["microwave","oven"],
    "dishwasher":["dishwasher","dish"],
}


def library_search(query="", category="", limit=12):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cat = f"%{category.lower()}%" if category else "%"

        q_lower = query.lower()
        terms = [q_lower] if q_lower else []
        if q_lower in SEARCH_SYNONYMS:
            terms = SEARCH_SYNONYMS[q_lower]
        for key, syns in SEARCH_SYNONYMS.items():
            if q_lower in syns and key not in terms:
                terms.append(key)
                terms.extend(syns)
        terms = list(set(terms)) if terms else [""]

        like_clauses = []
        params = []
        for term in terms:
            t = f"%{term}%"
            like_clauses.append(
                "(LOWER(COALESCE(c.family_name,'')) LIKE %s OR LOWER(COALESCE(c.type_name,'')) LIKE %s OR LOWER(c.category) LIKE %s)"
            )
            params.extend([t, t, t])

        where_names = " OR ".join(like_clauses) if like_clauses else "1=1"
        params.extend([cat, limit])

        cursor.execute(f"""
            SELECT c.id, c.category, c.family_name, c.type_name,
                   c.width_mm, c.height_mm, c.length_mm,
                   c.parameters->>'_material' as material
            FROM library l JOIN components c ON c.id = l.component_id
            WHERE ({where_names})
              AND LOWER(c.category) LIKE %s
            ORDER BY c.category, c.family_name
            LIMIT %s
        """, params)
        rows = cursor.fetchall()

    results = []
    for r in rows:
        results.append({
            "id":       r["id"],
            "category": r["category"],
            "name":     r["family_name"] or r["type_name"] or r["category"],
            "w_mm":     round(r["width_mm"])  if r["width_mm"]  else None,
            "h_mm":     round(r["height_mm"]) if r["height_mm"] else None,
            "l_mm":     round(r["length_mm"]) if r["length_mm"] else None,
            "material": r["material"],
        })
    return results


# Tool definitions for the API
LIBRARY_TOOLS = [
    {
        "name": "get_library_categories",
        "description": "Get a list of all component categories available in the library with counts.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "search_library",
        "description": "Search the component library by name and/or category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string", "description": "Name to search for"},
                "category": {"type": "string", "description": "IFC category to filter by"},
                "limit":    {"type": "integer", "description": "Max results (default 12, max 25)", "default": 12}
            },
            "required": []
        }
    }
]


def process_tool_call(tool_name, tool_input):
    if tool_name == "get_library_categories":
        cats = library_get_categories()
        return json.dumps(cats) if cats else "Library is empty. Generate parametrically."
    elif tool_name == "search_library":
        query    = tool_input.get("query", "")
        category = tool_input.get("category", "")
        limit    = min(int(tool_input.get("limit", 12)), 25)
        results  = library_search(query, category, limit)
        return json.dumps(results) if results else f"No results for query='{query}' category='{category}'."
    return "Unknown tool"


GENERATE_SYSTEM_PROMPT = """You are an expert AI architect integrated into BIM Studio.

You design any building type: houses, apartments, offices, gyms, warehouses, hospitals, schools, hotels, retail, industrial.

You have library search tools. Use them to understand what components are available.

CONVERSATION BEHAVIOUR
When the request is vague, ask ONE focused question covering:
- Building type and purpose
- Location (city/country — affects codes, climate, seismic zone)
- Size (m², floors, rooms, capacity)
- Budget
- Timeline
- Special requirements

Do NOT generate a spec until you have enough to make real decisions.

When you have enough information:
1. Call get_library_categories to see what's in the library.
2. Write a thorough briefing — site analysis, structural system, MEP, cost breakdown by trade, timeline, risks, code compliance. Be genuinely useful.
3. Tell the user their building is being generated.
4. Silently append the spec inside <building_spec> tags. The user NEVER sees it.

SPEC FORMAT — room-based with coordinates. The system generates walls, doors, windows, floors, ceilings, and fixtures for each room. You MUST provide x and y coordinates for every room.

ROOM COORDINATE RULES — CRITICAL:
- You MUST provide x and y for every room (metres, origin at southwest corner of building)
- x = east-west position, y = north-south position
- width = east-west dimension, depth = north-south dimension
- Rooms that share a wall MUST have exactly matching coordinates along that edge
- Example: Room A at x=0, width=5 and Room B at x=5 share their east/west wall
- Example: Room A at y=0, depth=4 and Room B at y=4 share their north/south wall
- ALL rooms in the same row MUST have the same depth so their walls align
- There must be NO gaps between rooms — every room edge must touch another room or the building perimeter
- Think of it as a grid: rooms tile together like a spreadsheet with no empty cells
- height = floor-to-ceiling in metres (2.7 residential, 3.0+ commercial)

ROOM LAYOUT STRATEGY:
1. Decide the building footprint (e.g. 12m x 10m for a small apartment)
2. Divide into rows (e.g. row 1: y=0 to y=4, row 2: y=4 to y=5.5, row 3: y=5.5 to y=9)
3. All rooms in the same row MUST have the same depth
4. Fill each row completely — room widths must add up to the building width
5. Public rooms (living, kitchen, dining) go in one row
6. Corridors/hallways span the full building width as their own row
7. Private rooms (bedrooms, bathrooms) go in another row
8. ALWAYS include a small utility / mechanical room (1.2m x 1.5m minimum) somewhere interior. Name it "Utility", "Mechanical", or "Mech". The generator places the air handler, water heater, and electrical panel here. Without it they get placed in the nearest room, which looks odd.
9. Every room gets exterior=true if it touches the building perimeter

MEP IS AUTOMATIC — do not list ducts, pipes, outlets, panels, diffusers, or
smoke detectors in the rooms array. The generator populates every
conditioned room with:
- 1 ceiling supply diffuser + branch duct (IfcAirTerminal + IfcDuctSegment)
- 1 return grille for rooms > 9 m²
- 1 ceiling light fixture + 2 wall outlets (IfcLightFixture, IfcOutlet)
- 1 smoke detector (IfcFireSuppressionTerminal)
- In wet rooms: cold + hot water pipes and a DWV stack (IfcPipeSegment)
- Central equipment in the utility room: air handler (IfcUnitaryEquipment),
  water heater (IfcTank), electrical panel (IfcElectricDistributionBoard)
- Outdoor condenser on a pad beside the building
- For commercial buildings: sprinkler head per room (IfcFireSuppressionTerminal)

In your briefing to the user, describe the MEP system ("central split-system
heat pump, 2.5 tons sized for 800 sqft in zone 2A climate; standard 40-gal
electric water heater; 100A main panel") so they know what they're getting.
Do NOT mention every duct or outlet — just the sized equipment.

ARCHITECTURE STYLE (metadata) — ALWAYS set this. Never leave the building style-less.

REQUIRED fields:
- "architectural_style": one of the built-in keys below, OR any descriptive phrase the user gives (the generator has fuzzy aliasing — "queen anne" -> victorian, "greek revival" -> neoclassical, "tuscan" -> mediterranean, "mcm" -> mid_century_modern, etc.).
  Built-in keys:
    victorian, tudor, colonial, neoclassical, craftsman, farmhouse, ranch,
    modern, contemporary, mid_century_modern, industrial,
    mediterranean, spanish_revival, cape_cod,
    commercial_office, warehouse
- "front_elevation": "south" | "north" | "east" | "west". Must match how rooms are laid out (public / entry side).
- "style_palette": an object of hex colors overriding the style defaults. Use this when the user specifies colors ("dark green Victorian", "white modern with black trim"). Example:
    "style_palette": {
      "ext_wall": "#AE4C5D",
      "trim": "#F0E8D6",
      "roof": "#3B2F2F",
      "accent": "#5A2F4A",
      "window_glass": "#8FC8E8"
    }
  Keys: ext_wall, int_wall, floor, roof, ceiling, trim, accent, window_glass. Any omitted key falls back to the style default.
- "style_notes": 1-2 sentences explaining the massing/massing choices ("steep cross-gable with full-width wraparound porch, turret at southwest corner"). Helps the user understand the design decision.

PICKING A STYLE WHEN THE USER DOESN'T NAME ONE:
- Florida / Gulf coast residential -> mediterranean or spanish_revival
- New England residential -> cape_cod or colonial
- Midwest / Texas suburban -> ranch or farmhouse
- Urban loft conversion -> industrial
- Office / tech HQ -> commercial_office or modern
- When the user says "classic", "traditional", or "old style" -> colonial or victorian depending on era cues
- Default if truly ambiguous -> contemporary

FIXTURE TYPES (auto-populated based on room name):
- "Living Room" / "Lounge" — sofa, coffee table, TV stand, light
- "Kitchen" — counter, sink, stove, refrigerator, light
- "Dining Room" — dining table, chairs, light
- "Bedroom" / "Master Bedroom" / "Guest Bedroom" — bed, wardrobe, nightstand, light
- "Bathroom" / "En-suite" — toilet, sink, shower, light
- "Hallway" / "Corridor" / "Foyer" — light only
- "Utility" / "Laundry" — water heater, light
- "Office" / "Study" — desk, chair, light
- "Garage" — light only

COST ESTIMATION — ALWAYS calculate from first principles, never work backwards from budget.

Step 1: Calculate realistic cost based on building type, size, location, and materials.
  Use these USD/m2 benchmarks:
  Basic residential $800-1,400 | Mid residential $1,400-2,200 | High-end $2,200-4,000+
  Commercial office $1,800-3,500 | Retail $1,200-2,500 | Industrial $400-900
  Sports/gym $1,500-3,000 | Hospital $4,000-8,000+
  Adjust for location: Florida +5%, NYC +40%, rural -15%, etc.

Step 2: Break down by trade using these proportions:
  Structure 25-35% | Envelope 20-25% | Fit-out 15-25% | HVAC 8-15% | Plumbing 5-10% | Electrical 8-12% | Fire 2-4% | Site 5-10%

Step 3: Compare to the user's budget:
  - If budget >= realistic cost: confirm it's achievable, note contingency available
  - If budget is 10-30% short: warn it's tight, suggest value engineering options
  - If budget is >30% short: clearly state the budget is insufficient, give the realistic cost

NEVER adjust your cost estimate to match the user's budget. Always estimate honestly first.

EXAMPLE SPEC (one-bedroom apartment, 8m x 9.5m footprint):

<building_spec>
{
  "name": "One Bedroom Apartment",
  "floors": [
    {
      "name": "Ground Floor",
      "elevation": 0.0,
      "height": 2.7,
      "rooms": [
        {"name":"Living Room",  "x":0.0, "y":0.0, "width":5.0, "depth":4.0, "exterior":true},
        {"name":"Kitchen",      "x":5.0, "y":0.0, "width":3.0, "depth":4.0, "exterior":true},
        {"name":"Hallway",      "x":0.0, "y":4.0, "width":8.0, "depth":1.5, "exterior":false},
        {"name":"Bedroom",      "x":0.0, "y":5.5, "width":4.5, "depth":4.0, "exterior":true},
        {"name":"Bathroom",     "x":4.5, "y":5.5, "width":2.0, "depth":4.0, "exterior":true},
        {"name":"Utility",      "x":6.5, "y":5.5, "width":1.5, "depth":4.0, "exterior":true}
      ]
    }
  ],
  "metadata": {
    "architectural_style": "ranch",
    "front_elevation": "south",
    "style_palette": {
      "ext_wall": "#D1BF94",
      "trim": "#E6DCBE",
      "accent": "#80593A",
      "roof": "#5A4F40"
    },
    "style_notes": "Long low-profile ranch with hip roof, deep eaves, wide horizontal windows facing the south garden.",
    "location": "City, Country",
    "building_type": "Residential",
    "estimated_cost_usd": 300000,
    "gross_floor_area_m2": 76,
    "floors_above_ground": 1,
    "estimated_duration_months": 7,
    "structural_system": "CMU load-bearing walls",
    "primary_material": "8-inch CMU with stucco finish",
    "building_code": "Florida Building Code 2020",
    "cost_breakdown": {
      "structure_usd": 75000,
      "envelope_usd": 60000,
      "fitout_usd": 52500,
      "hvac_usd": 36000,
      "plumbing_usd": 21000,
      "electrical_usd": 24000,
      "fire_usd": 7500,
      "site_prelim_usd": 24000
    }
  }
}
</building_spec>

CRITICAL CHECKS before outputting spec:
1. Do all rooms in the same row have the SAME depth? If not, fix it.
2. Do room widths in each row add up to the same building width? If not, fix it.
3. Are there any gaps? If row 1 ends at y=4.0, row 2 MUST start at y=4.0.
4. Does every room have x and y coordinates? If not, add them.
"""



@app.route("/api/generate/stream", methods=["POST"])
def generate_stream():
    data         = request.json or {}
    message      = data.get("message", "")
    history      = data.get("history", [])
    session_spec = data.get("session_spec")

    session_context = ""
    if session_spec:
        session_context = f"\n\nCURRENT SPEC (user is refining):\n{json.dumps(session_spec, indent=1)}"

    messages = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message + session_context})

    def generate():
        full_text   = ""
        visible_buf = ""
        in_spec     = False
        msgs        = list(messages)

        while True:
            tool_uses    = {}
            tool_results = []
            has_tool_use = False
            response_content_blocks = []

            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=16000,
                system=GENERATE_SYSTEM_PROMPT,
                tools=LIBRARY_TOOLS,
                messages=msgs
            ) as stream:
                for event in stream:
                    etype = event.type

                    if etype == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            chunk = delta.text
                            full_text += chunk
                            if not in_spec:
                                visible_buf += chunk
                                if "<building_spec>" in visible_buf:
                                    before = visible_buf.split("<building_spec>")[0].rstrip()
                                    if before:
                                        yield f"data: {json.dumps({'type':'text','text':before})}\n\n"
                                    in_spec     = True
                                    visible_buf = ""
                                else:
                                    hold = len("<building_spec>") - 1
                                    safe = visible_buf[:-hold] if len(visible_buf) > hold else ""
                                    if safe:
                                        yield f"data: {json.dumps({'type':'text','text':safe})}\n\n"
                                    visible_buf = visible_buf[len(safe):]
                            else:
                                if "</building_spec>" in full_text:
                                    in_spec     = False
                                    visible_buf = ""

                        elif hasattr(delta, "partial_json"):
                            bid = event.index
                            if bid in tool_uses:
                                tool_uses[bid]["input_str"] += delta.partial_json

                    elif etype == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            has_tool_use = True
                            tool_uses[event.index] = {
                                "id":        block.id,
                                "name":      block.name,
                                "input_str": ""
                            }

                    elif etype == "content_block_stop":
                        bid = event.index
                        if bid in tool_uses:
                            tu = tool_uses[bid]
                            try:
                                tool_input = json.loads(tu["input_str"]) if tu["input_str"] else {}
                            except Exception:
                                tool_input = {}
                            print(f"Tool call: {tu['name']}({json.dumps(tool_input)})")
                            yield f"data: {json.dumps({'type':'tool','tool':tu['name'],'input':tool_input})}\n\n"
                            result = process_tool_call(tu["name"], tool_input)
                            print(f"Tool result preview: {result[:120]}")
                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": tu["id"],
                                "content":     result
                            })

                final_msg = stream.get_final_message()
                response_content_blocks = final_msg.content

            if has_tool_use:
                msgs.append({"role": "assistant", "content": response_content_blocks})
                msgs.append({"role": "user",      "content": tool_results})
                continue

            break

        # Flush remaining visible text
        if visible_buf.strip():
            yield f"data: {json.dumps({'type':'text','text':visible_buf})}\n\n"

        # Extract spec
        spec = None
        if "<building_spec>" in full_text and "</building_spec>" in full_text:
            try:
                start = full_text.index("<building_spec>") + len("<building_spec>")
                end   = full_text.index("</building_spec>")
                spec  = json.loads(full_text[start:end].strip())
                spec  = ground_spec_with_library(spec)
                print(f"Spec parsed: {spec.get('name')} | {len(spec.get('floors',[]))} floors")
            except Exception as e:
                print(f"Spec parse error: {e}")

        yield f"data: {json.dumps({'type':'spec','spec':spec})}\n\n"
        yield f"data: {json.dumps({'type':'done'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Multi-agent generation ──────────────────────────────────────────────
@app.route("/api/generate/multi_agent", methods=["POST"])
def generate_multi_agent_stream():
    """
    Multi-agent generation endpoint. SSE contract matches /api/generate/stream
    so the existing frontend JS works unchanged: it streams 'text' events for
    progress, emits one 'spec' event when the building_spec is ready (which
    triggers the frontend's autoGenerateAndView), then 'done'.

    Flow: Brief (Sonnet) → Layout (Haiku) → Facade + MEP in parallel (Haiku)
          → merge_to_spec → ground_spec_with_library → stream.
    """
    if not _MULTI_AGENT_AVAILABLE:
        return jsonify({"error": "Multi-agent pipeline not available — check bim_multi_agent/ imports"}), 500

    data = request.json or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Empty prompt"}), 400

    def _sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        import time as _time
        import concurrent.futures
        try:
            yield _sse({"type": "text", "text":
                "**Multi-agent pipeline** — 4 specialists coordinating.\n\n"})
            yield _sse({"type": "text", "text":
                "**Step 1/4: Brief agent** setting design intent...\n"})

            t0 = _time.time()

            # Step 1: Brief (Sonnet)
            brief, brief_run = run_brief_agent(message)
            yield _sse({"type": "text", "text":
                f"✓ Style: **{brief.architectural_style}**, "
                f"{brief.total_sqft:.0f} sqft, {brief.floors_count} floor(s)  \n"
                f"Notes: {brief.style_notes}\n\n"})

            # Step 2: Layout (Haiku)
            yield _sse({"type": "text", "text":
                "**Step 2/4: Layout agent** placing rooms...\n"})
            layout, layout_run = run_layout_agent(brief)
            room_names = [r.name for f in layout.floors for r in f.rooms]
            yield _sse({"type": "text", "text":
                f"✓ {len(layout.floors)} floor(s), "
                f"{layout.footprint_width:.1f}m × {layout.footprint_depth:.1f}m, "
                f"{len(room_names)} rooms: {', '.join(room_names)}\n\n"})

            # Steps 3+4: Facade + MEP in parallel (Haiku)
            yield _sse({"type": "text", "text":
                "**Step 3+4/4: Facade + MEP** running in parallel...\n"})
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                facade_future = pool.submit(run_facade_agent, brief, layout)
                mep_future    = pool.submit(run_mep_agent, brief, layout)
                facade, facade_run = facade_future.result()
                mep, mep_run       = mep_future.result()

            feature_summary = ", ".join(f.type for f in facade.exterior_features)
            yield _sse({"type": "text", "text":
                f"✓ Facade: {len(facade.exterior_features)} features ({feature_summary})  \n"
                f"✓ MEP: {mep.hvac_type}, {mep.hvac_zones} zone(s), equipment in {mep.equipment_location}\n\n"})

            # Merge into the spec shape generate.py expects
            building_spec = ma_merge_to_spec(brief, layout, facade, mep)
            spec_dict = building_spec.model_dump()
            spec_dict = ground_spec_with_library(spec_dict)

            total = _time.time() - t0
            yield _sse({"type": "text", "text":
                f"**Done in {total:.1f}s** — "
                f"brief={brief_run.duration_s}s, "
                f"layout={layout_run.duration_s}s, "
                f"facade={facade_run.duration_s}s, "
                f"mep={mep_run.duration_s}s.  \n"
                f"Generating 3D model...\n"})

            # Cache full PipelineResult so the edit endpoint can reuse it
            result = _MA_PipelineResult(
                spec=building_spec, brief=brief, layout=layout,
                facade=facade, mep=mep,
                runs=[brief_run, layout_run, facade_run, mep_run],
                total_duration_s=round(total, 2),
            )
            cache_key = spec_dict.get("name", "building")
            _LAST_RESULTS[cache_key] = result

            # Emit the spec — frontend's SSE handler triggers IFC build + xeokit load
            yield _sse({"type": "spec", "spec": spec_dict})
            yield _sse({"type": "done"})

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[multi_agent] error: {e}\n{tb}")
            yield _sse({"type": "text", "text": f"\n\n**Pipeline error:** {e}\n"})
            yield _sse({"type": "done"})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Layout-first generation ─────────────────────────────────────────────
@app.route("/api/generate/from_layout", methods=["POST"])
def generate_from_layout_stream():
    """
    Layout-first generation endpoint. Same SSE contract as
    /api/generate/multi_agent so the frontend's streaming + autoview flow
    works unchanged. Difference: skips the Layout Agent because the user
    supplies a Layout JSON directly.
 
    Body: { layout: <Layout dict>, style_hint: str, name: str,
            front_elevation: str, location: str | null }
 
    Flow: Brief (style/palette only, layout fixed) → Facade + MEP in
    parallel → merge_to_spec → ground_spec_with_library → stream.
    """
    if not _MULTI_AGENT_AVAILABLE:
        return jsonify({"error": "Multi-agent pipeline not available"}), 500
 
    data = request.json or {}
    layout_dict = data.get("layout") or {}
    style_hint = (data.get("style_hint") or "").strip()
    name = (data.get("name") or "Building").strip()
    front_elevation = (data.get("front_elevation") or "south").strip()
    location = data.get("location")
 
    # Validate the layout up-front so we can fail fast with a useful message.
    try:
        layout = _MA_Layout(**layout_dict)
    except Exception as e:
        return jsonify({"error": f"Invalid layout JSON: {e}"}), 400
 
    if not layout.floors or not any(f.rooms for f in layout.floors):
        return jsonify({"error": "Layout has no rooms"}), 400
 
    def _sse(obj):
        return f"data: {json.dumps(obj)}\n\n"
 
    def generate():
        import time as _time
        try:
            n_rooms = sum(len(f.rooms) for f in layout.floors)
            yield _sse({"type": "text", "text":
                f"**Layout-first pipeline** — {len(layout.floors)} floor(s), "
                f"{n_rooms} room(s), "
                f"{layout.footprint_width:.1f}m × {layout.footprint_depth:.1f}m\n\n"})
            yield _sse({"type": "text", "text":
                "**Step 1/3: Brief agent** classifying typology + picking palette...\n"})
 
            t0 = _time.time()
 
            # Run the layout-first pipeline (Brief → Facade ‖ MEP).
            result = generate_building_from_layout(
                layout,
                style_hint=style_hint,
                name=name,
                front_elevation=front_elevation,
                location=location,
                parallel_specialists=True,
            )
            brief = result.brief
            facade = result.facade
            mep = result.mep
 
            yield _sse({"type": "text", "text":
                f"✓ Style: **{brief.architectural_style}**, "
                f"palette: {', '.join(brief.style_palette.keys())}  \n"
                f"Notes: {brief.style_notes}\n\n"})
            yield _sse({"type": "text", "text":
                "**Step 2+3/3: Facade + MEP** ran in parallel against your layout.\n"})
 
            feature_summary = ", ".join(f.type for f in facade.exterior_features)
            yield _sse({"type": "text", "text":
                f"✓ Facade: {len(facade.exterior_features)} features ({feature_summary})  \n"
                f"✓ MEP: {mep.hvac_type}, {mep.hvac_zones} zone(s), "
                f"equipment in {mep.equipment_location}\n\n"})
 
            # Spec for the renderer
            spec_dict = result.spec.model_dump()
            spec_dict = ground_spec_with_library(spec_dict)
 
            total = _time.time() - t0
            run_summary = ", ".join(
                f"{r.agent}={r.duration_s}s" for r in result.runs
            )
            yield _sse({"type": "text", "text":
                f"**Done in {total:.1f}s** — {run_summary}.  \n"
                f"Generating 3D model...\n"})
 
            # Cache for the edit endpoint
            cache_key = spec_dict.get("name", "building")
            _LAST_RESULTS[cache_key] = result
 
            # Spec event triggers the frontend's autoGenerateAndView()
            yield _sse({"type": "spec", "spec": spec_dict})
            yield _sse({"type": "done"})
 
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[from_layout] error: {e}\n{tb}")
            yield _sse({"type": "text", "text": f"\n\n**Pipeline error:** {e}\n"})
            yield _sse({"type": "done"})
 
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/generate/multi_agent/edit", methods=["POST"])
def generate_multi_agent_edit():
    """
    Re-run one specialist with an edit request. Surgical by default —
    only the targeted agent runs. Set cascade=True to also rerun downstream
    agents (only meaningful for 'layout' and 'brief' targets).

    Request:
      {
        "edit_request": str,
        "target": "palette"|"materials"|"facade"|"mep"|"layout"|"brief",
        "cascade": bool (optional, default false),
        "cache_key": str (optional)
      }

    Edit cost by target (rough):
      palette   — no LLM call, instant. For "change to red brick".
      materials — 1 Haiku call, ~5s. For "swap clapboard for stucco".
      facade    — 1 Haiku call, ~6s. For "add a cupola and change the style".
      mep       — 1 Haiku call, ~5s. For "switch to gas heating".
      layout    — 1 Haiku call, ~5s (+cascade ~11s). For "add a bedroom".
      brief     — 1 Sonnet call, ~5s (+cascade ~17s). For style/sqft changes.
    """
    if not _MULTI_AGENT_AVAILABLE:
        return jsonify({"error": "Multi-agent pipeline not available"}), 500

    data = request.json or {}
    edit_request = (data.get("edit_request") or "").strip()
    target = data.get("target", "facade")
    cascade = bool(data.get("cascade", False))
    cache_key = data.get("cache_key")

    VALID_TARGETS = {"palette", "materials", "facade", "mep", "layout", "brief"}
    if not edit_request:
        return jsonify({"error": "Missing edit_request"}), 400
    if target not in VALID_TARGETS:
        return jsonify({
            "error": f"Invalid target: {target}. Must be one of {sorted(VALID_TARGETS)}."
        }), 400

    if cache_key and cache_key in _LAST_RESULTS:
        prev = _LAST_RESULTS[cache_key]
    elif _LAST_RESULTS:
        cache_key = next(reversed(_LAST_RESULTS))
        prev = _LAST_RESULTS[cache_key]
    else:
        return jsonify({"error": "No previous generation to edit. Generate a building first."}), 404

    try:
        result = edit_building(prev, edit_request, target=target, cascade=cascade)
        _LAST_RESULTS[cache_key] = result
        spec_dict = result.spec.model_dump()
        spec_dict = ground_spec_with_library(spec_dict)
        return jsonify({
            "status": "done",
            "spec": spec_dict,
            "cache_key": cache_key,
            "target": target,
            "cascade": cascade,
            "edit_request": edit_request,
            "total_duration_s": result.total_duration_s,
            "agents": [
                {"agent": r.agent, "duration_s": r.duration_s,
                 "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}
                for r in result.runs
            ],
        })
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate/ifc", methods=["POST"])
def generate_ifc_endpoint():
    data = request.json or {}
    spec = data.get("spec")
    upload_preview = data.get("upload_preview", True)
    if not spec:
        return jsonify({"error": "No spec provided"}), 400
    try:
        sys.path.insert(0, REPO_DIR)
        from generate import generate_ifc
        spec = ground_spec_with_library(spec)
        safe_name = spec.get("name", "building").replace(" ", "_").replace("/", "-")
        # Sanitize further
        safe_name = "".join(c for c in safe_name if c.isalnum() or c in "_-")
        ts = datetime.now().strftime("%H%M%S")
        output_path = os.path.abspath(os.path.join(GENERATED_FOLDER, f"generated_{safe_name}_{ts}.ifc"))
        path = generate_ifc(spec, output_path)
        print(f"Generated IFC: {path}")
        result = {"status": "done", "output": os.path.basename(path)}
        if upload_preview:
            try:
                aps = upload_to_aps(path)
                result["aps"] = aps
            except Exception as e:
                print(f"APS upload failed: {e}")
                result["aps_error"] = str(e)
        return jsonify(result)
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate/download")
def download_ifc():
    filename = request.args.get("file")
    if not filename:
        return jsonify({"error": "No filename"}), 400
    # Sanitize to prevent path traversal
    basename = secure_filename(os.path.basename(filename))
    if not basename:
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(GENERATED_FOLDER, basename)
    if not os.path.exists(path):
        return jsonify({"error": f"File not found: {basename}"}), 404
    return send_file(path, as_attachment=True, download_name=basename, mimetype="application/octet-stream")


# ── Compose ──────────────────────────────────────────────────────────────────
def get_library_summary():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""SELECT p.id as project_id,p.name as project_name,c.id as component_id,
            c.category,c.family_name,c.type_name,c.width_mm,c.height_mm,c.length_mm,s.level
            FROM components c JOIN projects p ON p.id=c.project_id
            LEFT JOIN spatial_data s ON s.component_id=c.id
            WHERE p.status='done' ORDER BY p.id,c.category""")
        rows = [dict(r) for r in cursor.fetchall()]

    return [{"id": r["component_id"], "project": r["project_name"], "category": r["category"],
             "name": r["family_name"] or r["type_name"] or r["category"], "level": r["level"],
             "dims": {"w": round(r["width_mm"], 1) if r["width_mm"] else None,
                      "h": round(r["height_mm"], 1) if r["height_mm"] else None,
                      "l": round(r["length_mm"], 1) if r["length_mm"] else None}} for r in rows]


COMPOSE_SYSTEM_PROMPT = """You are an AI architect assistant for a BIM system.
You have access to a library of real building components extracted from IFC files.
When referencing components use: [COMPONENT:id:category:name]
Always end with: <selected_components>[1, 2, 3]</selected_components>
If none relevant: <selected_components>[]</selected_components>"""


@app.route("/api/compose", methods=["POST"])
def compose():
    data = request.json or {}
    message = data.get("message", "")
    history = data.get("history", [])

    library = get_library_summary()
    lib_text = f"COMPONENT LIBRARY ({len(library)} components):\n" + json.dumps(library, indent=1)
    messages = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": f"{lib_text}\n\nUser request: {message}"})

    def generate():
        with client.messages.stream(model="claude-sonnet-4-20250514", max_tokens=2000,
                system=COMPOSE_SYSTEM_PROMPT, messages=messages) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'type':'text','text':text})}\n\n"
        yield f"data: {json.dumps({'type':'done'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=" * 40)
    print("BIM STUDIO — http://localhost:5050")
    print("=" * 40)
    app.run(debug=True, port=5050, threaded=True)
