import os
import json
import subprocess
import psycopg2
import psycopg2.extras
import anthropic
from flask import Flask, jsonify, send_from_directory, send_file, request, Response, stream_with_context
from dotenv import load_dotenv
from aps_upload import upload_to_aps, get_token

load_dotenv()

app = Flask(__name__, static_folder="static")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
REPO_DIR         = os.path.join(BASE_DIR, '..')
UPLOAD_FOLDER    = os.path.join(REPO_DIR, 'uploads')
GENERATED_FOLDER = os.path.join(REPO_DIR, 'generated')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)

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

# ── Static ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ── APS Token ────────────────────────────────────────────────────────────────

@app.route("/api/aps/token")
def aps_token():
    try:
        token = get_token()
        return jsonify({"access_token": token, "expires_in": 3600})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── IFC Upload (developer pipeline tool) ─────────────────────────────────────

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
        print("Running pipeline...")
        result = subprocess.run(
            ["python3", "run.py", filepath],
            capture_output=True, text=True, timeout=300,
            cwd=REPO_DIR
        )
        print("STDOUT:", result.stdout[-500:] if result.stdout else "")
        if result.returncode != 0:
            return jsonify({"error": "Pipeline failed", "log": result.stderr}), 500

        # Parse project_id from "Pipeline complete. Project id: 16"
        project_id = None
        for line in result.stdout.splitlines():
            if "Project id:" in line:
                try:
                    project_id = int(line.split("Project id:")[-1].strip())
                    break
                except:
                    pass

        if not project_id:
            conn = get_db()
            cur  = conn.cursor()
            cur.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                project_id = row[0]
            cur.close(); conn.close()

        if not project_id:
            return jsonify({"error": "Could not determine project_id"}), 500

        print(f"Pipeline done. project_id={project_id}")
        print("Uploading to APS...")
        aps_result = upload_to_aps(filepath)

        conn = get_db()
        cur  = conn.cursor()
        cur.execute("UPDATE projects SET aps_urn = %s WHERE id = %s", (aps_result["urn"], project_id))
        conn.commit()
        cur.close(); conn.close()

        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT COUNT(*) as total FROM components WHERE project_id = %s", (project_id,))
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id = %s", (project_id,))
        rels  = cur.fetchone()["total"]
        cur.close(); conn.close()

        return jsonify({
            "status": "done", "project_id": project_id,
            "aps": aps_result,
            "stats": {"components": total, "relationships": rels}
        })

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

# ── Projects ─────────────────────────────────────────────────────────────────

@app.route("/api/projects")
def get_projects():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, filename, status, processed_at, aps_urn FROM projects ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route("/api/projects/<int:pid>/components")
def get_components(pid):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
               c.quality_score, c.parameters,
               s.pos_x, s.pos_y, s.pos_z, s.rot_x, s.rot_y, s.rot_z,
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

# ── Component lookup ──────────────────────────────────────────────────────────

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
        WHERE c.revit_id = %s LIMIT 1
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

# ── Library ───────────────────────────────────────────────────────────────────

@app.route("/api/library", methods=["GET"])
def get_library():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.category, c.family_name, c.type_name, c.revit_id,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
               c.quality_score, l.saved_at, l.notes, s.level, p.name as project_name
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
    cur.execute("INSERT INTO library (component_id, notes) VALUES (%s, %s) RETURNING id",
                (component_id, notes))
    lib_id = cur.fetchone()["id"]
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "saved", "library_id": lib_id, "component_id": component_id})

@app.route("/api/library/remove", methods=["POST"])
def remove_from_library():
    data = request.json
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM library WHERE component_id = %s", (data.get("component_id"),))
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

# ── Reconstruct ───────────────────────────────────────────────────────────────

@app.route("/api/reconstruct", methods=["POST"])
def reconstruct():
    data       = request.json
    project_id = data.get("project_id")
    if not project_id:
        return jsonify({"error": "project_id required"}), 400
    try:
        result = subprocess.run(
            ["python3", "reconstruct.py", str(project_id)],
            capture_output=True, text=True, timeout=120, cwd=REPO_DIR
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr}), 500
        output_file = None
        for line in result.stdout.splitlines():
            if "Output:" in line:
                output_file = line.split("Output:")[-1].strip()
        return jsonify({"status": "done", "output": output_file, "log": result.stdout})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Reconstruction timed out"}), 500

# ── AI GENERATE ───────────────────────────────────────────────────────────────

def get_component_library_for_ai():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.category, c.family_name, c.type_name,
               c.width_mm, c.height_mm, c.length_mm, c.area_m2,
               c.parameters->>'_material' as material,
               p.name as project_name
        FROM components c
        JOIN projects p ON p.id = c.project_id
        WHERE p.status = 'done'
        ORDER BY c.category, c.id
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    by_cat = {}
    for r in rows:
        cat = r["category"]
        if cat not in by_cat:
            by_cat[cat] = []
        by_cat[cat].append({
            "id":       r["id"],
            "name":     r["family_name"] or r["type_name"] or cat,
            "w_mm":     r["width_mm"],
            "h_mm":     r["height_mm"],
            "l_mm":     r["length_mm"],
            "material": r["material"],
            "project":  r["project_name"]
        })

    summary = {}
    for cat, items in by_cat.items():
        seen = set()
        unique = []
        for item in items:
            if item["name"] not in seen:
                seen.add(item["name"])
                unique.append(item)
        summary[cat] = unique[:15]
    return summary


GENERATE_SYSTEM_PROMPT = """You are an expert AI architect and building consultant integrated into BIM Studio.

You have access to a library of real building components extracted from IFC files. These are your parametric templates — you scale and adapt them to every new building you design.

## YOUR BEHAVIOUR

### When the user's request is vague or missing key information:
Ask focused clarifying questions. You need to know:
- Building type (house, office, apartment, warehouse, etc.)
- Location / site (city, country, terrain, climate zone)
- Approximate size (floor count, footprint, or total area)
- Budget (rough range is fine)
- Timeline / start date
- Material preferences or constraints
- Any special requirements (accessibility, sustainability, local codes)

Ask naturally in one message. Do not generate a building spec until you have enough to make informed decisions.

### When you have enough information:
1. Give a concise conversational summary: site analysis, cost estimate, timeline, key material choices, any risks or code concerns for that location.
2. Tell the user you are generating their building plan now.
3. Silently append the machine-readable spec at the very end of your response inside <building_spec> tags. The user will NEVER see this — it is processed automatically. Do NOT mention it, describe it, or reference it in your conversational text.

## REAL-WORLD KNOWLEDGE
Use your knowledge of:
- Local building codes and regulations for the specified location
- Climate and seismic conditions
- Typical construction costs per m2 for the region
- Soil and site considerations
- Material availability and lead times

## COMPONENT TEMPLATE RULES
- Walls: keep thickness from template, scale length to footprint, scale height to floor height
- Slabs: scale to floor footprint, keep thickness from template
- Doors/Windows: use template dimensions unless user specifies
- HVAC: size by floor count x floor area, route vertically through building core
- All positions in mm, elevation in metres

## LIBRARY COMPONENTS — REAL GEOMETRY
The component library includes furniture, fixtures, and fittings with real 3D geometry
extracted from uploaded IFC files. Each has an "id" field.

When placing furniture or fixtures, ALWAYS check the library first and use a matching
component by setting "library_component_id" to its id. This gives the user real geometry
instead of a placeholder box.

Example — placing a dining table from the library:
{
  "category": "IfcFurniture",
  "name": "Dining Table",
  "library_component_id": 70,
  "pos_x": 3000, "pos_y": 4000, "pos_z": 0,
  "rot_z": 0
}

If no library match exists for a requested item, generate it parametrically with
realistic width_mm, height_mm, length_mm dimensions for that object type.

## SPEC FORMAT
The <building_spec> block must be valid JSON and is completely hidden from the user.

<building_spec>
{
  "name": "Building Name",
  "floors": [
    {
      "name": "Ground Floor",
      "elevation": 0.0,
      "height": 3000,
      "components": [
        {
          "category": "IfcWall",
          "name": "South Exterior Wall",
          "material": "Brick",
          "pos_x": 0, "pos_y": 0, "pos_z": 0,
          "rot_z": 0,
          "width_mm": 290,
          "height_mm": 3000,
          "length_mm": 12000,
          "properties": {"Pset_WallCommon": {"IsExternal": "True"}}
        }
      ]
    }
  ],
  "metadata": {
    "location": "City, Country",
    "building_type": "Residential",
    "estimated_cost_usd": 350000,
    "gross_floor_area_m2": 150,
    "floors_above_ground": 2,
    "estimated_duration_months": 8,
    "structural_system": "Timber frame",
    "primary_material": "Brick exterior, timber frame",
    "site_concerns": "...",
    "building_code": "IBC 2021"
  }
}
</building_spec>

Wall coordinate guide for a W x D footprint (all in mm):
- South wall: pos_x=0, pos_y=0, rot_z=0, length=W
- North wall: pos_x=0, pos_y=D, rot_z=0, length=W
- West wall:  pos_x=0, pos_y=0, rot_z=90, length=D
- East wall:  pos_x=W, pos_y=0, rot_z=90, length=D
- Floor slab: pos_x=0, pos_y=0, pos_z=0

Always include at minimum: 4 exterior walls, floor slab, roof slab, at least one door per floor.
"""


@app.route("/api/generate/stream", methods=["POST"])
def generate_stream():
    data         = request.json
    message      = data.get("message", "")
    history      = data.get("history", [])
    session_spec = data.get("session_spec")

    library  = get_component_library_for_ai()
    lib_text = "COMPONENT TEMPLATE LIBRARY:\n" + json.dumps(library, indent=1)

    session_context = ""
    if session_spec:
        session_context = f"\n\nCURRENT BUILDING SPEC (user is refining this):\n{json.dumps(session_spec, indent=1)}"

    messages = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({
        "role": "user",
        "content": f"{lib_text}{session_context}\n\nUser: {message}"
    })

    def generate():
        full_text    = ""
        visible_buf  = ""
        in_spec      = False

        with client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=GENERATE_SYSTEM_PROMPT,
            messages=messages
        ) as stream:
            for text in stream.text_stream:
                full_text += text

                if not in_spec:
                    visible_buf += text
                    if "<building_spec>" in visible_buf:
                        # Emit everything before the tag
                        before = visible_buf.split("<building_spec>")[0].rstrip()
                        if before:
                            yield f"data: {json.dumps({'type': 'text', 'text': before})}\n\n"
                        in_spec     = True
                        visible_buf = ""
                    else:
                        # Hold back enough to catch a tag split across chunks
                        hold = len("<building_spec>") - 1
                        safe = visible_buf[:-hold] if len(visible_buf) > hold else ""
                        if safe:
                            yield f"data: {json.dumps({'type': 'text', 'text': safe})}\n\n"
                        visible_buf = visible_buf[len(safe):]
                else:
                    if "</building_spec>" in full_text:
                        in_spec     = False
                        visible_buf = ""

        # Emit any remaining visible text (after spec closing tag)
        if visible_buf.strip():
            yield f"data: {json.dumps({'type': 'text', 'text': visible_buf})}\n\n"

        # Extract BuildingSpec
        spec = None
        if "<building_spec>" in full_text and "</building_spec>" in full_text:
            try:
                start = full_text.index("<building_spec>") + len("<building_spec>")
                end   = full_text.index("</building_spec>")
                raw   = full_text[start:end].strip()
                spec  = json.loads(raw)
                print(f"Spec parsed: {spec.get('name')} | {len(spec.get('floors',[]))} floors")
            except Exception as e:
                print(f"Spec parse error: {e}")

        yield f"data: {json.dumps({'type': 'spec', 'spec': spec})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/api/generate/ifc", methods=["POST"])
def generate_ifc_endpoint():
    data           = request.json
    spec           = data.get("spec")
    upload_preview = data.get("upload_preview", True)

    if not spec:
        return jsonify({"error": "No spec provided"}), 400

    try:
        import sys
        sys.path.insert(0, REPO_DIR)
        from generate import generate_ifc

        safe_name   = spec.get("name", "building").replace(" ", "_").replace("/", "-")
        output_path = os.path.join(GENERATED_FOLDER, f"generated_{safe_name}.ifc")
        output_path = os.path.abspath(output_path)

        path = generate_ifc(spec, output_path)
        print(f"Generated IFC: {path}")

        result = {"status": "done", "output": os.path.basename(path)}

        if upload_preview:
            try:
                aps = upload_to_aps(path, model_name=spec.get("name", "Generated Building"))
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
        return jsonify({"error": "No filename provided"}), 400

    # Only allow simple filenames — no path traversal
    basename = os.path.basename(filename)
    if not basename or basename != filename.replace("\\", "/").split("/")[-1]:
        return jsonify({"error": "Invalid filename"}), 400

    path = os.path.join(GENERATED_FOLDER, basename)
    if not os.path.exists(path):
        return jsonify({"error": f"File not found: {basename}"}), 404

    print(f"Serving download: {path}")
    return send_file(
        path,
        as_attachment=True,
        download_name=basename,
        mimetype="application/octet-stream"
    )


# ── AI Compose (disassemble tab) ──────────────────────────────────────────────

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
                "w": round(r["width_mm"], 1)  if r["width_mm"]  else None,
                "h": round(r["height_mm"], 1) if r["height_mm"] else None,
                "l": round(r["length_mm"], 1) if r["length_mm"] else None,
            },
        })
    return summary


COMPOSE_SYSTEM_PROMPT = """You are an AI architect assistant for a BIM system.
You have access to a library of real building components extracted from IFC files.
When referencing components use: [COMPONENT:id:category:name]
Always end with: <selected_components>[1, 2, 3]</selected_components>
If none relevant: <selected_components>[]</selected_components>"""


@app.route("/api/compose", methods=["POST"])
def compose():
    data     = request.json
    message  = data.get("message", "")
    history  = data.get("history", [])
    library  = get_library_summary()
    lib_text = f"COMPONENT LIBRARY ({len(library)} components):\n" + json.dumps(library, indent=1)
    messages = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": f"{lib_text}\n\nUser request: {message}"})

    def generate():
        full_text = ""
        with client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=COMPOSE_SYSTEM_PROMPT,
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
            details = []
            if details:
                yield f"data: {json.dumps({'type': 'components', 'components': details})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    print("=" * 40)
    print("BIM STUDIO — http://localhost:5050")
    print("=" * 40)
    app.run(debug=True, port=5050, threaded=True)
