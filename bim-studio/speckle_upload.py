import os
import requests
import time
from dotenv import load_dotenv

load_dotenv()

SPECKLE_TOKEN      = os.getenv("SPECKLE_TOKEN")
SPECKLE_SERVER     = os.getenv("SPECKLE_SERVER", "https://app.speckle.systems")
SPECKLE_PROJECT_ID = os.getenv("SPECKLE_PROJECT_ID", "8cd76bca8d")


def upload_ifc_to_speckle(ifc_filepath, model_name=None):
    """
    Upload an IFC file to Speckle using their file upload API.
    Returns dict with project_id, model_id, viewer_url.
    """
    if not SPECKLE_TOKEN:
        raise ValueError("SPECKLE_TOKEN not set in .env")

    filename  = os.path.basename(ifc_filepath)
    model_name = model_name or filename.replace(".ifc", "").replace("_", " ")

    headers = {"Authorization": f"Bearer {SPECKLE_TOKEN}"}

    # ── Step 1: Create a new model (branch) in the project ────────────────────
    print(f"Creating Speckle model: {model_name}")
    create_model_query = """
    mutation CreateModel($input: CreateModelInput!) {
      modelMutations {
        create(input: $input) {
          id
          name
        }
      }
    }
    """
    res = requests.post(
        f"{SPECKLE_SERVER}/graphql",
        json={
            "query": create_model_query,
            "variables": {
                "input": {
                    "projectId": SPECKLE_PROJECT_ID,
                    "name": model_name,
                    "description": f"Uploaded from BIM Studio: {filename}"
                }
            }
        },
        headers={**headers, "Content-Type": "application/json"}
    )
    res.raise_for_status()
    data = res.json()

    if "errors" in data:
        # Model might already exist — try to find it
        model_id = find_model_by_name(model_name, headers)
        if not model_id:
            raise Exception(f"Could not create model: {data['errors']}")
    else:
        model_id = data["data"]["modelMutations"]["create"]["id"]

    print(f"✓ Model created/found: {model_id}")

    # ── Step 2: Upload the IFC file ────────────────────────────────────────────
    print(f"Uploading {filename} to Speckle...")
    upload_url = f"{SPECKLE_SERVER}/api/file/ifc/{SPECKLE_PROJECT_ID}/{model_id}"

    with open(ifc_filepath, "rb") as f:
        upload_res = requests.post(
            upload_url,
            files={"file": (filename, f, "application/octet-stream")},
            headers=headers
        )

    if upload_res.status_code not in [200, 201, 202]:
        raise Exception(f"Upload failed: {upload_res.status_code} {upload_res.text}")

    print(f"✓ File uploaded, waiting for Speckle to convert...")

    # ── Step 3: Poll until conversion is done ─────────────────────────────────
    version_id = poll_for_version(model_id, headers, timeout=120)

    viewer_url = f"{SPECKLE_SERVER}/projects/{SPECKLE_PROJECT_ID}/models/{model_id}"
    embed_url  = build_embed_url(SPECKLE_PROJECT_ID, model_id)

    print(f"✓ Done! Viewer: {viewer_url}")

    return {
        "project_id": SPECKLE_PROJECT_ID,
        "model_id":   model_id,
        "version_id": version_id,
        "viewer_url": viewer_url,
        "embed_url":  embed_url,
    }


def find_model_by_name(name, headers):
    """Find an existing model by name in the project."""
    query = """
    query GetModels($projectId: String!) {
      project(id: $projectId) {
        models {
          items {
            id
            name
          }
        }
      }
    }
    """
    res = requests.post(
        f"{SPECKLE_SERVER}/graphql",
        json={"query": query, "variables": {"projectId": SPECKLE_PROJECT_ID}},
        headers={**headers, "Content-Type": "application/json"}
    )
    data = res.json()
    models = data.get("data", {}).get("project", {}).get("models", {}).get("items", [])
    for m in models:
        if m["name"] == name:
            return m["id"]
    return None


def poll_for_version(model_id, headers, timeout=120):
    """Poll Speckle until the uploaded IFC has been converted and a version exists."""
    query = """
    query GetVersions($projectId: String!, $modelId: String!) {
      project(id: $projectId) {
        model(id: $modelId) {
          versions(limit: 1) {
            items {
              id
              createdAt
            }
          }
        }
      }
    }
    """
    start = time.time()
    while time.time() - start < timeout:
        res = requests.post(
            f"{SPECKLE_SERVER}/graphql",
            json={
                "query": query,
                "variables": {
                    "projectId": SPECKLE_PROJECT_ID,
                    "modelId": model_id
                }
            },
            headers={**headers, "Content-Type": "application/json"}
        )
        data = res.json()
        items = (data.get("data", {})
                     .get("project", {})
                     .get("model", {})
                     .get("versions", {})
                     .get("items", []))
        if items:
            print(f"✓ Version ready: {items[0]['id']}")
            return items[0]["id"]

        print("  Still converting... waiting 5s")
        time.sleep(5)

    raise TimeoutError("Speckle conversion timed out after 120s")


def build_embed_url(project_id, model_id):
    """Build the Speckle embed URL for the iframe."""
    import urllib.parse
    embed_params = urllib.parse.quote('{"isEnabled":true,"isTransparent":false,"hideControls":false,"hideSelectionInfo":false}')
    return f"{SPECKLE_SERVER}/projects/{project_id}/models/{model_id}#embed=%7B%22isEnabled%22%3Atrue%7D"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 speckle_upload.py path/to/file.ifc")
        sys.exit(1)
    result = upload_ifc_to_speckle(sys.argv[1])
    print(result)
