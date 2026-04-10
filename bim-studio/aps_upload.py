import os
import time
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

APS_CLIENT_ID     = os.getenv("APS_CLIENT_ID")
APS_CLIENT_SECRET = os.getenv("APS_CLIENT_SECRET")
APS_BUCKET        = os.getenv("APS_BUCKET", "bim-studio-models")

BASE_URL  = "https://developer.api.autodesk.com"


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_token():
    """Get a 2-legged OAuth token for server-to-server calls."""
    res = requests.post(
        f"{BASE_URL}/authentication/v2/token",
        data={
            "client_id":     APS_CLIENT_ID,
            "client_secret": APS_CLIENT_SECRET,
            "grant_type":    "client_credentials",
            "scope":         "data:read data:write data:create bucket:read bucket:create"
        }
    )
    res.raise_for_status()
    return res.json()["access_token"]


# ── Bucket ─────────────────────────────────────────────────────────────────────

def ensure_bucket(token):
    """Create the bucket if it doesn't exist."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Check if exists
    res = requests.get(f"{BASE_URL}/oss/v2/buckets/{APS_BUCKET}/details", headers=headers)
    if res.status_code == 200:
        return

    # Create it
    res = requests.post(
        f"{BASE_URL}/oss/v2/buckets",
        json={"bucketKey": APS_BUCKET, "policyKey": "persistent"},
        headers=headers
    )
    if res.status_code not in [200, 201, 409]:  # 409 = already exists
        res.raise_for_status()


# ── Upload ─────────────────────────────────────────────────────────────────────

def upload_file(token, filepath):
    """Upload a file to OSS and return the object URN."""
    filename    = os.path.basename(filepath)
    object_name = filename.replace(" ", "_")
    headers     = {"Authorization": f"Bearer {token}"}

    file_size = os.path.getsize(filepath)

    # APS requires a signed upload URL approach for larger files
    # Step 1: Get signed upload URL
    res = requests.get(
        f"{BASE_URL}/oss/v2/buckets/{APS_BUCKET}/objects/{object_name}/signeds3upload",
        headers=headers,
        params={"minutesExpiration": 60}
    )
    res.raise_for_status()
    data = res.json()
    upload_key = data["uploadKey"]
    upload_url = data["urls"][0]

    # Step 2: Upload directly to S3
    with open(filepath, "rb") as f:
        s3_res = requests.put(upload_url, data=f)
    s3_res.raise_for_status()

    # Step 3: Complete the upload
    complete_res = requests.post(
        f"{BASE_URL}/oss/v2/buckets/{APS_BUCKET}/objects/{object_name}/signeds3upload",
        json={"uploadKey": upload_key},
        headers={**headers, "Content-Type": "application/json"}
    )
    complete_res.raise_for_status()
    data = complete_res.json()

    object_id = data["objectId"]
    urn = base64.b64encode(object_id.encode()).decode().rstrip("=")
    return urn


# ── Translate ──────────────────────────────────────────────────────────────────

def translate_file(token, urn):
    """Submit translation job to convert the file to SVF2 for the viewer."""
    headers = {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "x-ads-force":    "true"
    }
    res = requests.post(
        f"{BASE_URL}/modelderivative/v2/designdata/job",
        json={
            "input":  {"urn": urn},
            "output": {
                "formats": [{
                    "type":  "svf2",
                    "views": ["2d", "3d"]
                }]
            }
        },
        headers=headers
    )
    res.raise_for_status()
    return res.json()


def poll_translation(token, urn, timeout=300):
    """Poll until translation is complete. Returns status."""
    headers = {"Authorization": f"Bearer {token}"}
    start   = time.time()

    while time.time() - start < timeout:
        res  = requests.get(
            f"{BASE_URL}/modelderivative/v2/designdata/{urn}/manifest",
            headers=headers
        )
        if res.status_code == 200:
            data     = res.json()
            status   = data.get("status", "")
            progress = data.get("progress", "")
            print(f"  Translation: {status} {progress}")

            if status == "success":
                return "success"
            elif status == "failed":
                raise Exception(f"Translation failed: {data}")

        time.sleep(8)

    raise TimeoutError("Translation timed out after 300s")


# ── Main entry ─────────────────────────────────────────────────────────────────

def upload_to_aps(filepath):
    """
    Full pipeline: authenticate → ensure bucket → upload → translate.
    Returns dict with urn and viewer_url.
    """
    print("Authenticating with APS...")
    token = get_token()
    print("✓ Authenticated")

    print("Ensuring bucket exists...")
    ensure_bucket(token)
    print(f"✓ Bucket: {APS_BUCKET}")

    print(f"Uploading {os.path.basename(filepath)}...")
    urn = upload_file(token, filepath)
    print(f"✓ Uploaded. URN: {urn[:30]}...")

    print("Starting translation to SVF2...")
    translate_file(token, urn)

    print("Waiting for translation...")
    poll_translation(token, urn)
    print("✓ Translation complete")

    return {
        "urn":        urn,
        "viewer_url": f"https://viewer.autodesk.com/designviewer?urn={urn}"
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 aps_upload.py path/to/file.ifc")
        sys.exit(1)
    result = upload_to_aps(sys.argv[1])
    print(result)
