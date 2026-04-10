import os
import json
import subprocess
import psycopg2
import psycopg2.extras
import anthropic
from flask import Flask, jsonify, send_from_directory, request, Response, stream_with_context
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SPECKLE_PROJECT_ID = os.getenv("SPECKLE_PROJECT_ID", "8cd76bca8d")
SPECKLE_MODEL_ID   = os.getenv("SPECKLE_MODEL_ID",   "a69f7e2443")

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

# ── Projects ───────────────────────────────────────────────────────────────

@app.route("/api/projects")
def get_projects():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, filename, status, processed_at FROM projects ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

# ── Components ─────────────────────────────────────────────────────────────

@app.route("/api/projects/<int:pid>/components")
def get_components(pid):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT category, COUNT(*) as count
        FROM components WHERE project_id = %s
        GROUP BY category ORDER BY count DESC
    """, (pid,))
    cats = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) as total FROM components WHERE project_id = %s", (pid,))
    total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id = %s", (pid,))
    rels = cur.fetchone()["total"]
    cur.close(); conn.close()
    return jsonify({"total": total, "relationships": rels, "categories": cats})

# ── Speckle / Component Lookup ─────────────────────────────────────────────

@app.route("/api/component/by-revit-id/<revit_id>")
def get_component_by_revit_id(revit_id):
    """Look up a component by its IFC GlobalId (= Speckle applicationId)."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    """Get all components saved to the library."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
    """Save a component to the library by component ID or revit_id."""
    data = request.json
    component_id = data.get("component_id")
    revit_id     = data.get("revit_id")
    notes        = data.get("notes", "")

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Look up by revit_id if component_id not provided
    if not component_id and revit_id:
        cur.execute("SELECT id FROM components WHERE revit_id = %s LIMIT 1", (revit_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "Component not found"}), 404
        component_id = row["id"]

    # Check if already saved
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
    """Remove a component from the library."""
    data = request.json
    component_id = data.get("component_id")
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM library WHERE component_id = %s", (component_id,))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "removed"})

@app.route("/api/library/clear", methods=["POST"])
def clear_library():
    """Clear the entire library."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM library")
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "cleared"})

# ── Reconstruct ────────────────────────────────────────────────────────────

@app.route("/api/reconstruct", methods=["POST"])
def reconstruct():
    """Run reconstruct.py on a given project and return the output file path."""
    data       = request.json
    project_id = data.get("project_id")

    if not project_id:
        return jsonify({"error": "project_id required"}), 400

    try:
        result = subprocess.run(
            ["python3", "reconstruct.py", str(project_id)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr}), 500

        # Find the output file
        output_file = None
        for line in result.stdout.splitlines():
            if line.startswith("Output:"):
                output_file = line.replace("Output:", "").strip()

        return jsonify({
            "status": "done",
            "output": output_file,
            "log": result.stdout
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Reconstruction timed out"}), 500

# ── Speckle Config ─────────────────────────────────────────────────────────

@app.route("/api/speckle/config")
def speckle_config():
    """Return Speckle project/model IDs for the frontend."""
    return jsonify({
        "project_id": SPECKLE_PROJECT_ID,
        "model_id":   SPECKLE_MODEL_ID,
        "viewer_url": f"https://app.speckle.systems/projects/{SPECKLE_PROJECT_ID}/models/{SPECKLE_MODEL_ID}"
    })

# ── AI Compose ─────────────────────────────────────────────────────────────

def get_library_summary():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            p.id as project_id, p.name as project_name,
            c.id as component_id, c.category, c.family_name, c.type_name,
            c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
            c.quality_score,
            s.level, s.elevation,
            s.pos_x, s.pos_y, s.pos_z
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
            "id":      r["component_id"],
            "project": r["project_name"],
            "category": r["category"],
            "name":    r["family_name"] or r["type_name"] or r["category"],
            "level":   r["level"],
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

You have access to a library of real building components extracted from IFC files. Each component has:
- A unique ID, category (IfcWall, IfcSlab, IfcDoor, etc.), name, and dimensions
- Spatial data (position, level/floor)
- A quality score (0-1) indicating data completeness

Your job is to help users understand and compose buildings from these components.

When a user asks you to build or compose something:
1. Select appropriate components from the library that match their requirements
2. Explain your selections clearly — what you chose and why
3. Always reference components by their ID so the UI can highlight them
4. Be honest about limitations — the library only contains what was extracted from real IFC files

When referencing components, use this exact format so the UI can parse them:
[COMPONENT:id:category:name]

For example: [COMPONENT:42:IfcWall:Basic Wall]

Always end your response with a JSON block listing the selected component IDs:
<selected_components>
[1, 2, 3, 42, 55]
</selected_components>

If no components are relevant, use an empty array: <selected_components>[]</selected_components>

Be concise but specific. This is a technical demo for an architecture firm."""

@app.route("/api/compose", methods=["POST"])
def compose():
    data    = request.json
    message = data.get("message", "")
    history = data.get("history", [])

    library      = get_library_summary()
    library_text = f"COMPONENT LIBRARY ({len(library)} components across all projects):\n"
    library_text += json.dumps(library, indent=1)

    messages = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({
        "role": "user",
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
                ids_str  = full_text[start:end].strip()
                selected = json.loads(ids_str)
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
