import os
import json
import subprocess
import psycopg2
import psycopg2.extras
import anthropic
from flask import Flask, jsonify, send_from_directory, request, Response, stream_with_context
from dotenv import load_dotenv
from aps_upload import upload_to_aps, get_token

load_dotenv()

app = Flask(__name__, static_folder="static")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '..', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "bim_components"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD")
    )

def safe_float(v):
    try:
        return float(v) if v is not None else None
    except:
        return None

# ── Static ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ── APS Token (for frontend viewer) ───────────────────────────────────────

@app.route("/api/aps/token")
def aps_token():
    """Return a fresh APS access token for the frontend viewer."""
    try:
        token = get_token()
        return jsonify({"access_token": token, "expires_in": 3600})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── IFC Upload ─────────────────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def upload_ifc():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename.endswith(".ifc"):
        return jsonify({"error": "Only .ifc files are supported"}), 400

    filepath = os.path.join(UPLOAD_FOLDER, f.filename)
    f.save(filepath)
    print(f"Saved IFC to {filepath}")

    try:
        # ── Step 1: Run the pipeline ───────────────────────────────────────
        print("Running pipeline...")
        result = subprocess.run(
            ["python3", "run.py", filepath],
            capture_output=True, text=True, timeout=300,
            cwd=os.path.join(os.path.dirname(__file__), '..')
        )

        if result.returncode != 0:
            return jsonify({"error": "Pipeline failed", "log": result.stderr}), 500

        # Extract project_id from output
        project_id = None
        for line in result.stdout.splitlines():
            if line.startswith("project_id:"):
                project_id = int(line.split(":")[1].strip())

        if not project_id:
            conn = get_db()
            cur  = conn.cursor()
            cur.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1")
            project_id = cur.fetchone()[0]
            cur.close(); conn.close()

        print(f"Pipeline done. project_id={project_id}")

        # ── Step 2: Upload to APS ──────────────────────────────────────────
        print("Uploading to APS...")
        aps_result = upload_to_aps(filepath)

        # Save APS URN to DB
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE projects SET aps_urn = %s WHERE id = %s",
            (aps_result["urn"], project_id)
        )
        conn.commit()
        cur.close(); conn.close()

        # Get stats
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT COUNT(*) as total FROM components WHERE project_id = %s", (project_id,))
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id = %s", (project_id,))
        rels  = cur.fetchone()["total"]
        cur.close(); conn.close()

        return jsonify({
            "status":     "done",
            "project_id": project_id,
            "aps":        aps_result,
            "stats":      {"components": total, "relationships": rels}
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Projects ───────────────────────────────────────────────────────────────

@app.route("/api/projects")
def get_projects():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, filename, status, processed_at, aps_urn FROM projects ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

# ── Components ─────────────────────────────────────────────────────────────

@app.route("/api/projects/<int:pid>/components")
def get_components(pid):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
               c.quality_score, c.parameters,
               s.pos_x, s.pos_y, s.pos_z,
               s.rot_x, s.rot_y, s.rot_z,
               s.bounding_box, s.level, s.elevation
        FROM components c
        LEFT JOIN spatial_data s ON s.component_id = c.id
        WHERE c.project_id = %s
        ORDER BY COALESCE(s.pos_z, 0), c.category
    """, (pid,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    out = []
    for r in rows:
        d = dict(r)
        for k in ["pos_x","pos_y","pos_z","rot_x","rot_y","rot_z","elevation",
                  "width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
            d[k] = safe_float(d.get(k))
        out.append(d)
    return jsonify(out)

@app.route("/api/projects/<int:pid>/floors")
def get_floors(pid):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT s.level, s.elevation
        FROM spatial_data s
        JOIN components c ON c.id = s.component_id
        WHERE c.project_id = %s AND s.level IS NOT NULL
        ORDER BY s.elevation
    """, (pid,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route("/api/projects/<int:pid>/stats")
def get_stats(pid):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM components WHERE project_id = %s", (pid,))
    total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id = %s", (pid,))
    rels  = cur.fetchone()["total"]
    cur.close(); conn.close()
    return jsonify({"total": total, "relationships": rels})

# ── Component lookup by IFC GlobalId ──────────────────────────────────────

@app.route("/api/component/by-revit-id/<revit_id>")
def get_component_by_revit_id(revit_id):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
               c.quality_score, c.parameters,
               s.pos_x, s.pos_y, s.pos_z, s.level, s.elevation,
               p.name as project_name
        FROM components c
        LEFT JOIN spatial_data s ON s.component_id = c.id
        JOIN projects p ON p.id = c.project_id
        WHERE c.revit_id = %s
        LIMIT 1
    """, (revit_id,))
    row = cur.fetchone()
    cur.close(); conn.close()

    if not row:
        return jsonify({"error": "Component not found"}), 404

    d = dict(row)
    for k in ["pos_x","pos_y","pos_z","elevation","width_mm","height_mm",
              "length_mm","area_m2","volume_m3","quality_score"]:
        d[k] = safe_float(d.get(k))
    return jsonify(d)

# ── Library ────────────────────────────────────────────────────────────────

@app.route("/api/library", methods=["GET"])
def get_library():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
               c.quality_score, l.saved_at, l.notes,
               s.level, p.name as project_name
        FROM library l
        JOIN components c ON c.id = l.component_id
        LEFT JOIN spatial_data s ON s.component_id = c.id
        JOIN projects p ON p.id = c.project_id
        ORDER BY l.saved_at DESC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    out = []
    for r in rows:
        d = dict(r)
        for k in ["width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
            d[k] = safe_float(d.get(k))
        out.append(d)
    return jsonify(out)

@app.route("/api/library/save", methods=["POST"])
def save_to_library():
    data         = request.json
    component_id = data.get("component_id")
    revit_id     = data.get("revit_id")
    notes        = data.get("notes", "")

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if not component_id and revit_id:
        cur.execute("SELECT id FROM components WHERE revit_id = %s LIMIT 1", (revit_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "Component not found"}), 404
        component_id = row["id"]

    cur.execute("SELECT id FROM library WHERE component_id = %s", (component_id,))
    if cur.fetchone():
        cur.close(); conn.close()
        return jsonify({"status": "already_saved", "component_id": component_id})

    cur.execute(
        "INSERT INTO library (component_id, notes) VALUES (%s, %s) RETURNING id",
        (component_id, notes)
    )
    lib_id = cur.fetchone()["id"]
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "saved", "library_id": lib_id, "component_id": component_id})

@app.route("/api/library/remove", methods=["POST"])
def remove_from_library():
    data         = request.json
    component_id = data.get("component_id")
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM library WHERE component_id = %s", (component_id,))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "removed"})

@app.route("/api/library/clear", methods=["POST"])
def clear_library():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM library")
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "cleared"})

# ── Reconstruct ────────────────────────────────────────────────────────────

@app.route("/api/reconstruct", methods=["POST"])
def reconstruct():
    data       = request.json
    project_id = data.get("project_id")

    if not project_id:
        return jsonify({"error": "project_id required"}), 400

    try:
        result = subprocess.run(
            ["python3", "reconstruct.py", str(project_id)],
            capture_output=True, text=True, timeout=120,
            cwd=os.path.join(os.path.dirname(__file__), '..')
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr}), 500

        output_file = None
        for line in result.stdout.splitlines():
            if line.startswith("Output:"):
                output_file = line.replace("Output:", "").strip()

        return jsonify({"status": "done", "output": output_file, "log": result.stdout})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Reconstruction timed out"}), 500

# ── AI Compose ─────────────────────────────────────────────────────────────

def get_library_summary():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT p.id as project_id, p.name as project_name,
               c.id as component_id, c.category, c.family_name, c.type_name,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
               c.quality_score, s.level
        FROM components c
        JOIN projects p ON p.id = c.project_id
        LEFT JOIN spatial_data s ON s.component_id = c.id
        WHERE p.status = 'done'
        ORDER BY p.id, c.category
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    summary = []
    for r in rows:
        summary.append({
            "id":       r["component_id"],
            "project":  r["project_name"],
            "category": r["category"],
            "name":     r["family_name"] or r["type_name"] or r["category"],
            "level":    r["level"],
            "dims": {
                "w":    round(r["width_mm"],  1) if r["width_mm"]  else None,
                "h":    round(r["height_mm"], 1) if r["height_mm"] else None,
                "l":    round(r["length_mm"], 1) if r["length_mm"] else None,
                "area": round(r["area_m2"],   2) if r["area_m2"]   else None,
            },
            "quality": round(r["quality_score"], 2) if r["quality_score"] else None,
        })
    return summary

def get_component_details(component_ids):
    if not component_ids:
        return []
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
               c.quality_score, p.name as project_name,
               s.pos_x, s.pos_y, s.pos_z, s.level
        FROM components c
        JOIN projects p ON p.id = c.project_id
        LEFT JOIN spatial_data s ON s.component_id = c.id
        WHERE c.id = ANY(%s)
    """, (component_ids,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        for k in ["pos_x","pos_y","pos_z","width_mm","height_mm","length_mm",
                  "area_m2","volume_m3","quality_score"]:
            r[k] = safe_float(r.get(k))
    return rows

SYSTEM_PROMPT = """You are an AI architect assistant for a BIM (Building Information Modelling) system.
You have access to a library of real building components extracted from IFC files.
When referencing components use: [COMPONENT:id:category:name]
Always end with: <selected_components>[1, 2, 3]</selected_components>
If none relevant: <selected_components>[]</selected_components>"""

@app.route("/api/compose", methods=["POST"])
def compose():
    data    = request.json
    message = data.get("message", "")
    history = data.get("history", [])

    library      = get_library_summary()
    library_text = f"COMPONENT LIBRARY ({len(library)} components):\n"
    library_text += json.dumps(library, indent=1)

    messages = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({
        "role":    "user",
        "content": f"{library_text}\n\nUser request: {message}"
    })

    def generate():
        full_text = ""
        with client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=messages
        ) as stream:
            for text in stream.text_stream:
                full_text += text
                yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"

        selected = []
        if "<selected_components>" in full_text:
            try:
                start    = full_text.index("<selected_components>") + len("<selected_components>")
                end      = full_text.index("</selected_components>")
                selected = json.loads(full_text[start:end].strip())
            except:
                pass

        if selected:
            details = get_component_details(selected)
            yield f"data: {json.dumps({'type': 'components', 'components': details})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

if __name__ == "__main__":
    print("=" * 40)
    print("BIM STUDIO")
    print("http://localhost:5050")
    print("=" * 40)
    app.run(debug=True, port=5050, threaded=True)
