import os, json, subprocess, psycopg2, psycopg2.extras, anthropic
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, send_file, request, Response, stream_with_context
from dotenv import load_dotenv
from aps_upload import upload_to_aps, get_token

load_dotenv()
app = Flask(__name__, static_folder="static")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
REPO_DIR         = os.path.join(BASE_DIR, "..")
UPLOAD_FOLDER    = os.path.join(REPO_DIR, "uploads")
GENERATED_FOLDER = os.path.join(REPO_DIR, "generated")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)

def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST","localhost"), port=os.getenv("DB_PORT",5432),
        dbname=os.getenv("DB_NAME","bim_components"),
        user=os.getenv("DB_USER","postgres"), password=os.getenv("DB_PASSWORD"))

def safe_float(v):
    try: return float(v) if v is not None else None
    except: return None

def run_pipeline(filepath):
    """Run pipeline on a single IFC file. Returns (project_id, stats) or raises."""
    filename = os.path.basename(filepath)

    # Check if this exact filename was already successfully processed
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM projects WHERE filename = %s AND status = 'done' LIMIT 1", (filename,))
    existing = cur.fetchone()
    if existing:
        project_id = existing["id"]
        cur.execute("SELECT COUNT(*) as total FROM components WHERE project_id = %s", (project_id,))
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id = %s", (project_id,))
        rels = cur.fetchone()["total"]
        cur.close(); conn.close()
        print(f"Already processed: {filename} (project_id={project_id})")
        return project_id, {"components": total, "relationships": rels}, True  # True = was cached
    cur.close(); conn.close()
    result = subprocess.run(
        ["python3", "run.py", filepath],
        capture_output=True, text=True, timeout=300, cwd=REPO_DIR)
    print("STDOUT:", result.stdout[-500:])
    if result.returncode != 0:
        raise Exception(f"Pipeline failed: {result.stderr[:300]}")
    project_id = None
    for line in result.stdout.splitlines():
        if "Project id:" in line:
            try: project_id = int(line.split("Project id:")[-1].strip()); break
            except: pass
    if not project_id:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row: project_id = row[0]
        cur.close(); conn.close()
    if not project_id:
        raise Exception("Could not determine project_id")
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM components WHERE project_id=%s", (project_id,))
    total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id=%s", (project_id,))
    rels = cur.fetchone()["total"]
    cur.close(); conn.close()
    return project_id, {"components": total, "relationships": rels}, False  # False = freshly processed

# ── Static ──────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return send_from_directory("static", "index.html")

# ── APS Token ────────────────────────────────────────────────────────────────
@app.route("/api/aps/token")
def aps_token():
    try: return jsonify({"access_token": get_token(), "expires_in": 3600})
    except Exception as e: return jsonify({"error": str(e)}), 500

# ── Single IFC Upload ─────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload_ifc():
    if "file" not in request.files: return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename.endswith(".ifc"): return jsonify({"error": "Only .ifc"}), 400
    filepath = os.path.join(UPLOAD_FOLDER, f.filename)
    f.save(filepath)
    print(f"Saved IFC to {filepath}")
    try:
        project_id, stats, cached = run_pipeline(filepath)
        print(f"Pipeline done. project_id={project_id} cached={cached}")

        # If cached, reuse existing APS URN — no need to re-upload
        if cached:
            conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT aps_urn FROM projects WHERE id=%s", (project_id,))
            row = cur.fetchone(); cur.close(); conn.close()
            if row and row["aps_urn"]:
                return jsonify({"status": "done", "cached": True, "project_id": project_id,
                                "aps": {"urn": row["aps_urn"]}, "stats": stats})

        aps_result = {"urn": None}
        try:
            aps_result = upload_to_aps(filepath)
            conn = get_db(); cur = conn.cursor()
            cur.execute("UPDATE projects SET aps_urn=%s WHERE id=%s", (aps_result["urn"], project_id))
            conn.commit(); cur.close(); conn.close()
        except Exception as aps_err:
            print(f"APS upload failed (non-fatal): {aps_err}")
        return jsonify({"status": "done", "project_id": project_id, "aps": aps_result, "stats": stats})
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

# ── Bulk IFC Upload ───────────────────────────────────────────────────────────
@app.route("/api/upload/bulk", methods=["POST"])
def upload_bulk():
    """
    Accept multiple IFC files, process each one sequentially, stream progress as SSE.
    Each file gets its own pipeline run and APS upload.
    Frontend receives events:
      {type: "start",   file: "name.ifc", index: 0, total: 3}
      {type: "step",    file: "name.ifc", step: "pipeline"|"aps", message: "..."}
      {type: "done",    file: "name.ifc", project_id: 1, stats: {...}, aps: {...}}
      {type: "error",   file: "name.ifc", error: "..."}
      {type: "complete", processed: 3, failed: 0, total_components: 201}
    """
    files = request.files.getlist("files")
    if not files: return jsonify({"error": "No files provided"}), 400
    ifc_files = [f for f in files if f.filename.endswith(".ifc")]
    if not ifc_files: return jsonify({"error": "No .ifc files found"}), 400

    # Save all files first
    saved = []
    for f in ifc_files:
        fp = os.path.join(UPLOAD_FOLDER, f.filename)
        f.save(fp)
        saved.append((f.filename, fp))

    def stream():
        total = len(saved)
        processed = 0
        failed = 0
        total_components = 0

        for idx, (filename, filepath) in enumerate(saved):
            yield f"data: {json.dumps({'type':'start','file':filename,'index':idx,'total':total})}\n\n"
            try:
                yield f"data: {json.dumps({'type':'step','file':filename,'step':'pipeline','message':'Running pipeline…'})}\n\n"
                project_id, stats, cached = run_pipeline(filepath)
                total_components += stats["components"]

                if cached:
                    # Already processed — reuse existing APS URN
                    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                    cur.execute("SELECT aps_urn FROM projects WHERE id=%s", (project_id,))
                    row = cur.fetchone(); cur.close(); conn.close()
                    aps_result = {"urn": row["aps_urn"]} if row and row["aps_urn"] else {}
                    processed += 1
                    yield f"data: {json.dumps({'type':'done','file':filename,'project_id':project_id,'stats':stats,'aps':aps_result,'cached':True})}\n\n"
                else:
                    aps_result = {}
                    try:
                        aps_result = upload_to_aps(filepath)
                        conn = get_db(); cur = conn.cursor()
                        cur.execute("UPDATE projects SET aps_urn=%s WHERE id=%s", (aps_result["urn"], project_id))
                        conn.commit(); cur.close(); conn.close()
                    except Exception as aps_err:
                        print(f"APS upload failed (non-fatal): {aps_err}")
                    processed += 1
                    yield f"data: {json.dumps({'type':'done','file':filename,'project_id':project_id,'stats':stats,'aps':aps_result})}\n\n"

            except Exception as e:
                import traceback; print(traceback.format_exc())
                failed += 1
                yield f"data: {json.dumps({'type':'error','file':filename,'error':str(e)})}\n\n"

        yield f"data: {json.dumps({'type':'complete','processed':processed,'failed':failed,'total_components':total_components})}\n\n"

    return Response(stream_with_context(stream()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Projects ─────────────────────────────────────────────────────────────────
@app.route("/api/projects")
def get_projects():
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id,name,filename,status,processed_at,aps_urn FROM projects ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return jsonify(rows)

@app.route("/api/projects/<int:pid>/components")
def get_components(pid):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT c.id,c.category,c.family_name,c.type_name,c.revit_id,
        c.width_mm,c.height_mm,c.length_mm,c.area_m2,c.volume_m3,c.quality_score,c.parameters,
        s.pos_x,s.pos_y,s.pos_z,s.rot_x,s.rot_y,s.rot_z,s.bounding_box,s.level,s.elevation
        FROM components c LEFT JOIN spatial_data s ON s.component_id=c.id
        WHERE c.project_id=%s ORDER BY COALESCE(s.pos_z,0),c.category""", (pid,))
    rows = cur.fetchall(); cur.close(); conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for k in ["pos_x","pos_y","pos_z","rot_x","rot_y","rot_z","elevation","width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
            d[k] = safe_float(d.get(k))
        out.append(d)
    return jsonify(out)

@app.route("/api/projects/<int:pid>/stats")
def get_stats(pid):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) as total FROM components WHERE project_id=%s", (pid,))
    total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as total FROM relationships WHERE project_id=%s", (pid,))
    rels = cur.fetchone()["total"]
    cur.close(); conn.close()
    return jsonify({"total": total, "relationships": rels})

# ── Component lookup ──────────────────────────────────────────────────────────
@app.route("/api/component/by-revit-id/<revit_id>")
def get_component_by_revit_id(revit_id):
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT c.id,c.category,c.family_name,c.type_name,c.revit_id,
        c.width_mm,c.height_mm,c.length_mm,c.area_m2,c.volume_m3,c.quality_score,c.parameters,
        s.pos_x,s.pos_y,s.pos_z,s.level,s.elevation,p.name as project_name
        FROM components c LEFT JOIN spatial_data s ON s.component_id=c.id
        JOIN projects p ON p.id=c.project_id WHERE c.revit_id=%s LIMIT 1""", (revit_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return jsonify({"error": "Component not found"}), 404
    d = dict(row)
    for k in ["pos_x","pos_y","pos_z","elevation","width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
        d[k] = safe_float(d.get(k))
    return jsonify(d)

# ── Library ───────────────────────────────────────────────────────────────────
@app.route("/api/library", methods=["GET"])
def get_library():
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT c.id,c.category,c.family_name,c.type_name,c.revit_id,
        c.width_mm,c.height_mm,c.length_mm,c.area_m2,c.volume_m3,c.quality_score,
        l.saved_at,l.notes,s.level,p.name as project_name
        FROM library l JOIN components c ON c.id=l.component_id
        LEFT JOIN spatial_data s ON s.component_id=c.id
        JOIN projects p ON p.id=c.project_id ORDER BY l.saved_at DESC""")
    rows = cur.fetchall(); cur.close(); conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for k in ["width_mm","height_mm","length_mm","area_m2","volume_m3","quality_score"]:
            d[k] = safe_float(d.get(k))
        out.append(d)
    return jsonify(out)

@app.route("/api/library/save", methods=["POST"])
def save_to_library():
    data = request.json; component_id = data.get("component_id"); revit_id = data.get("revit_id"); notes = data.get("notes","")
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Resolve component_id from revit_id if needed
    if not component_id and revit_id:
        cur.execute("SELECT id FROM components WHERE revit_id=%s LIMIT 1", (revit_id,))
        row = cur.fetchone()
        if not row: cur.close(); conn.close(); return jsonify({"error": "Not found"}), 404
        component_id = row["id"]

    # Already in library by id?
    cur.execute("SELECT id FROM library WHERE component_id=%s", (component_id,))
    if cur.fetchone(): cur.close(); conn.close(); return jsonify({"status":"already_saved","component_id":component_id})

    # Fingerprint duplicate check:
    # A component is a duplicate if another library entry has the same
    # category + family_name + dimensions (rounded to nearest mm to avoid float noise).
    # If a duplicate fingerprint exists, skip — don't add to library.
    cur.execute("""
        SELECT c.category, c.family_name, c.width_mm, c.height_mm, c.length_mm
        FROM components c WHERE c.id = %s
    """, (component_id,))
    comp = cur.fetchone()
    if comp:
        category    = comp["category"]
        family_name = comp["family_name"] or ""
        w = round(float(comp["width_mm"]))  if comp["width_mm"]  is not None else None
        h = round(float(comp["height_mm"])) if comp["height_mm"] is not None else None
        l = round(float(comp["length_mm"])) if comp["length_mm"] is not None else None

        # Check if library already has a component with matching fingerprint
        cur.execute("""
            SELECT l.id FROM library l
            JOIN components c2 ON c2.id = l.component_id
            WHERE c2.category = %s
              AND COALESCE(c2.family_name, '') = %s
              AND (
                (%s IS NULL AND c2.width_mm  IS NULL) OR ROUND(c2.width_mm::numeric)  = %s
              )
              AND (
                (%s IS NULL AND c2.height_mm IS NULL) OR ROUND(c2.height_mm::numeric) = %s
              )
              AND (
                (%s IS NULL AND c2.length_mm IS NULL) OR ROUND(c2.length_mm::numeric) = %s
              )
            LIMIT 1
        """, (category, family_name, w, w, h, h, l, l))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"status":"duplicate","component_id":component_id,
                            "message":"A component with identical characteristics already exists in the library"})

    cur.execute("INSERT INTO library (component_id,notes) VALUES (%s,%s) RETURNING id", (component_id, notes))
    lib_id = cur.fetchone()["id"]; conn.commit(); cur.close(); conn.close()
    return jsonify({"status":"saved","library_id":lib_id,"component_id":component_id})

@app.route("/api/library/remove", methods=["POST"])
def remove_from_library():
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM library WHERE component_id=%s", (request.json.get("component_id"),))
    conn.commit(); cur.close(); conn.close(); return jsonify({"status":"removed"})

@app.route("/api/library/clear", methods=["POST"])
def clear_library():
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM library"); conn.commit(); cur.close(); conn.close()
    return jsonify({"status":"cleared"})

# ── Reconstruct ───────────────────────────────────────────────────────────────
@app.route("/api/reconstruct", methods=["POST"])
def reconstruct():
    data = request.json; project_id = data.get("project_id")
    if not project_id: return jsonify({"error":"project_id required"}), 400
    try:
        result = subprocess.run(["python3","reconstruct.py",str(project_id)],
            capture_output=True, text=True, timeout=120, cwd=REPO_DIR)
        if result.returncode != 0: return jsonify({"error":result.stderr}), 500
        output_file = None
        for line in result.stdout.splitlines():
            if "Output:" in line: output_file = line.split("Output:")[-1].strip()
        return jsonify({"status":"done","output":output_file,"log":result.stdout})
    except subprocess.TimeoutExpired: return jsonify({"error":"Timed out"}), 500

# ── Library tool functions (called by AI during generation) ───────────────────

def library_get_categories():
    """Return a summary of what categories exist in the library and how many of each."""
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.category, COUNT(*) as count
        FROM library l JOIN components c ON c.id = l.component_id
        GROUP BY c.category ORDER BY count DESC
    """)
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"category": r["category"], "count": r["count"]} for r in rows]

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
    """Search the library by name and/or category. Returns matching components with IDs."""
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cat = f"%{category.lower()}%" if category else "%"

    # Expand query with synonyms
    q_lower = query.lower()
    terms = [q_lower] if q_lower else []
    if q_lower in SEARCH_SYNONYMS:
        terms = SEARCH_SYNONYMS[q_lower]
    # Also check if query matches any synonym key
    for key, syns in SEARCH_SYNONYMS.items():
        if q_lower in syns and key not in terms:
            terms.append(key)
            terms.extend(syns)

    terms = list(set(terms)) if terms else [""]

    # Build OR clause for all terms
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

    cur.execute(f"""
        SELECT c.id, c.category, c.family_name, c.type_name,
               c.width_mm, c.height_mm, c.length_mm,
               c.parameters->>'_material' as material
        FROM library l JOIN components c ON c.id = l.component_id
        WHERE ({where_names})
          AND LOWER(c.category) LIKE %s
        ORDER BY c.category, c.family_name
        LIMIT %s
    """, params)
    rows = cur.fetchall(); cur.close(); conn.close()
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
        "description": "Get a list of all component categories available in the library with counts. Call this first to understand what's available before searching.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "search_library",
        "description": "Search the component library by name and/or category. Returns real IFC components with their IDs. Use library_component_id in the spec to reference them. Call multiple times for different component types.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Name to search for, e.g. 'dining table', 'refrigerator', 'sofa', 'toilet', 'duct', 'light'. Leave empty to browse all in a category."
                },
                "category": {
                    "type": "string",
                    "description": "IFC category to filter by, e.g. 'IfcFurniture', 'IfcElectricAppliance', 'IfcSanitaryTerminal', 'IfcLightFixture'. Leave empty to search all categories."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return. Default 12, max 25.",
                    "default": 12
                }
            },
            "required": []
        }
    }
]

def process_tool_call(tool_name, tool_input):
    """Execute a library tool call and return the result as a string."""
    if tool_name == "get_library_categories":
        cats = library_get_categories()
        if not cats:
            return "Library is empty — no components saved yet. Generate parametrically."
        return json.dumps(cats)
    elif tool_name == "search_library":
        query    = tool_input.get("query", "")
        category = tool_input.get("category", "")
        limit    = min(int(tool_input.get("limit", 12)), 25)
        results  = library_search(query, category, limit)
        if not results:
            return f"No results for query='{query}' category='{category}'. Generate parametrically for this component."
        return json.dumps(results)
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

SPEC FORMAT — room-based (procedural mode). The system will automatically generate all walls, doors, windows, floors, ceilings, and fixtures for each room. You only need to describe rooms.

ROOM LAYOUT RULES:
- x, y are the SW corner of each room's OUTER boundary (including walls) in metres
- width and depth include the wall thickness on all sides
- Rooms that share a wall must be placed so their outer boundaries touch exactly — no gaps
  Example: Living Room x=0 width=6, Kitchen x=6 width=3.5 (they share the wall at x=6)
- Exterior wall thickness is 0.20m, interior wall thickness is 0.12m
- For rooms stacked north-south: room2.y = room1.y + room1.depth (no gap)
- For rooms side by side east-west: room2.x = room1.x + room1.width (no gap)
- Hallways connect rooms — make them 1.5-2.0m wide minimum
- Minimum room sizes: bathroom 2.5×2.0m, bedroom 3.5×3.5m, living 4.5×4.0m, kitchen 3.0×3.5m
- height = floor-to-ceiling in metres (use 2.7 for residential, 3.0 for commercial)
- exterior: true for rooms on the building perimeter (get windows), false for interior rooms
- door_wall: which wall the door is on — "south", "north", "east", "west"
- All rooms on the same floor must form a connected, gap-free layout
- Patio/terrace rooms should be adjacent to living areas on the south or east side

ROOM TYPES (system auto-populates fixtures):
- "Living Room" / "Lounge" — sofa, coffee table, TV stand, light
- "Kitchen" — counter, sink, stove, refrigerator, light
- "Dining Room" — dining table, chairs, light
- "Bedroom" / "Master Bedroom" / "Guest Bedroom" — bed, wardrobe, nightstand, light
- "Bathroom" / "En-suite" — toilet, sink, shower, light
- "Hallway" / "Corridor" / "Foyer" — light
- "Utility" / "Laundry" — water heater, light
- "Office" / "Study" — desk, chair, light
- "Garage" — light

COST ESTIMATION — ALWAYS calculate from first principles, never work backwards from budget.

Step 1: Calculate realistic cost based on building type, size, location, and materials.
  Use these USD/m² benchmarks:
  Basic residential $800-1,400 | Mid residential $1,400-2,200 | High-end $2,200-4,000+
  Commercial office $1,800-3,500 | Retail $1,200-2,500 | Industrial $400-900
  Sports/gym $1,500-3,000 | Hospital $4,000-8,000+
  Adjust for location: Florida +5%, NYC +40%, rural -15%, etc.

Step 2: Break down by trade using these proportions:
  Structure 25-35% | Envelope 20-25% | Fit-out 15-25% | HVAC 8-15% | Plumbing 5-10% | Electrical 8-12% | Fire 2-4% | Site 5-10%

Step 3: Compare to the user's budget:
  - If budget >= realistic cost: confirm it's achievable, note contingency available
  - If budget is 10-30% short: warn it's tight, suggest value engineering options
  - If budget is >30% short: clearly state the budget is insufficient, give the realistic cost, explain what WOULD be achievable at their budget (smaller, simpler, different location)

NEVER adjust your cost estimate to match the user's budget. Always estimate honestly first, then address the budget gap.

TRADE BREAKDOWN
Structure 25-35% | Envelope 20-25% | Fit-out 15-25% | HVAC 8-15% | Plumbing 5-10% | Electrical 8-12% | Fire 2-4% | Site 5-10%

TIMELINE
House <200m²: 6-12mo | House 200-500m²: 12-18mo | Commercial <2000m²: 12-24mo

<building_spec>
{
  "name": "Building Name",
  "floors": [
    {
      "name": "Ground Floor",
      "elevation": 0.0,
      "height": 2.7,
      "rooms": [
        {"name":"Living Room",  "x":0.0, "y":0.0, "width":5.5, "depth":4.5, "exterior":true,  "door_wall":"north"},
        {"name":"Kitchen",      "x":5.5, "y":0.0, "width":3.5, "depth":4.5, "exterior":true,  "door_wall":"north"},
        {"name":"Hallway",      "x":0.0, "y":4.5, "width":9.0, "depth":1.5, "exterior":false, "door_wall":"east"},
        {"name":"Bedroom",      "x":0.0, "y":6.0, "width":4.5, "depth":3.5, "exterior":true,  "door_wall":"south"},
        {"name":"Bathroom",     "x":4.5, "y":6.0, "width":2.5, "depth":2.0, "exterior":false, "door_wall":"south"},
        {"name":"Patio",        "x":7.0, "y":6.0, "width":2.0, "depth":3.5, "exterior":true,  "door_wall":"south"}
      ]
    }
  ],
  "metadata": {
    "location": "City, Country",
    "building_type": "Residential",
    "estimated_cost_usd": 300000,
    "gross_floor_area_m2": 75,
    "floors_above_ground": 1,
    "estimated_duration_months": 7,
    "structural_system": "CMU load-bearing walls",
    "primary_material": "8-inch CMU with stucco finish",
    "site_concerns": "Hurricane zone, sandy soils",
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
"""

@app.route("/api/generate/stream", methods=["POST"])
def generate_stream():
    data         = request.json
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
            # Use streaming so text appears in real time even during tool-use turns
            tool_uses    = {}   # id -> {name, input_str}
            tool_results = []
            has_tool_use = False
            response_content_blocks = []  # accumulate for next turn

            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=16000,
                system=GENERATE_SYSTEM_PROMPT,
                tools=LIBRARY_TOOLS,
                messages=msgs
            ) as stream:
                for event in stream:
                    etype = event.type

                    # ── Text delta — stream immediately ──────────────────────
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
                            # Tool input streaming — accumulate
                            bid = event.index
                            if bid in tool_uses:
                                tool_uses[bid]["input_str"] += delta.partial_json

                    # ── Block start ──────────────────────────────────────────
                    elif etype == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            has_tool_use = True
                            tool_uses[event.index] = {
                                "id":        block.id,
                                "name":      block.name,
                                "input_str": ""
                            }

                    # ── Block stop ───────────────────────────────────────────
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

                # Get the final message for conversation history
                final_msg = stream.get_final_message()
                response_content_blocks = final_msg.content

            if has_tool_use:
                msgs.append({"role": "assistant", "content": response_content_blocks})
                msgs.append({"role": "user",      "content": tool_results})
                continue

            break  # No tool use — done

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
                print(f"Spec parsed: {spec.get('name')} | {len(spec.get('floors',[]))} floors")
            except Exception as e:
                print(f"Spec parse error: {e}")

        yield f"data: {json.dumps({'type':'spec','spec':spec})}\n\n"
        yield f"data: {json.dumps({'type':'done'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/generate/ifc", methods=["POST"])
def generate_ifc_endpoint():
    data = request.json; spec = data.get("spec"); upload_preview = data.get("upload_preview", True)
    if not spec: return jsonify({"error":"No spec provided"}), 400
    try:
        import sys; sys.path.insert(0, REPO_DIR)
        from generate import generate_ifc
        safe_name = spec.get("name","building").replace(" ","_").replace("/","-")
        ts = datetime.now().strftime("%H%M%S")
        output_path = os.path.abspath(os.path.join(GENERATED_FOLDER, f"generated_{safe_name}_{ts}.ifc"))
        path = generate_ifc(spec, output_path)
        print(f"Generated IFC: {path}")
        result = {"status":"done","output":os.path.basename(path)}
        if upload_preview:
            try:
                aps = upload_to_aps(path)
                result["aps"] = aps
            except Exception as e: print(f"APS upload failed: {e}"); result["aps_error"] = str(e)
        return jsonify(result)
    except Exception as e:
        import traceback; print(traceback.format_exc()); return jsonify({"error":str(e)}), 500

@app.route("/api/generate/download")
def download_ifc():
    filename = request.args.get("file")
    if not filename: return jsonify({"error":"No filename"}), 400
    basename = os.path.basename(filename)
    path = os.path.join(GENERATED_FOLDER, basename)
    if not os.path.exists(path): return jsonify({"error":f"File not found: {basename}"}), 404
    return send_file(path, as_attachment=True, download_name=basename, mimetype="application/octet-stream")

# ── Compose ───────────────────────────────────────────────────────────────────
def get_library_summary():
    conn = get_db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT p.id as project_id,p.name as project_name,c.id as component_id,
        c.category,c.family_name,c.type_name,c.width_mm,c.height_mm,c.length_mm,s.level
        FROM components c JOIN projects p ON p.id=c.project_id
        LEFT JOIN spatial_data s ON s.component_id=c.id
        WHERE p.status='done' ORDER BY p.id,c.category""")
    rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return [{"id":r["component_id"],"project":r["project_name"],"category":r["category"],
        "name":r["family_name"] or r["type_name"] or r["category"],"level":r["level"],
        "dims":{"w":round(r["width_mm"],1) if r["width_mm"] else None,
                "h":round(r["height_mm"],1) if r["height_mm"] else None,
                "l":round(r["length_mm"],1) if r["length_mm"] else None}} for r in rows]

COMPOSE_SYSTEM_PROMPT = """You are an AI architect assistant for a BIM system.
You have access to a library of real building components extracted from IFC files.
When referencing components use: [COMPONENT:id:category:name]
Always end with: <selected_components>[1, 2, 3]</selected_components>
If none relevant: <selected_components>[]</selected_components>"""

@app.route("/api/compose", methods=["POST"])
def compose():
    data = request.json; message = data.get("message",""); history = data.get("history",[])
    library = get_library_summary()
    lib_text = f"COMPONENT LIBRARY ({len(library)} components):\n" + json.dumps(library, indent=1)
    messages = []
    for h in history: messages.append({"role":h["role"],"content":h["content"]})
    messages.append({"role":"user","content":f"{lib_text}\n\nUser request: {message}"})
    def generate():
        full_text = ""
        with client.messages.stream(model="claude-sonnet-4-20250514", max_tokens=2000,
                system=COMPOSE_SYSTEM_PROMPT, messages=messages) as stream:
            for text in stream.text_stream:
                full_text += text; yield f"data: {json.dumps({'type':'text','text':text})}\n\n"
        yield f"data: {json.dumps({'type':'done'})}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__ == "__main__":
    print("="*40); print("BIM STUDIO — http://localhost:5050"); print("="*40)
    app.run(debug=True, port=5050, threaded=True)
