from flask import Flask, render_template, request, jsonify, send_from_directory, Response
import os
import random
import requests, uuid
from functools import wraps
from datetime import datetime

# Import the pieces from main.py
from main import build_raw_prompt, transform_prompt, update_and_inject
OUTPUT_FOLDER = os.getenv(
    "OUTPUT_FOLDER",
    r"C:\Users\Davis\Documents\Comfy\ComfyUI_windows_portable\ComfyUI\output"
)
ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS = os.getenv("ADMIN_PASS")

DEFAULT_REALISM_LORA   = 0.45
DEFAULT_DETAIL_LORA    = 2.0
DEFAULT_WORKFLOW_TYPE  = "final"      # or "smoke" if you prefer smoke tests by default
DEFAULT_FILENAME_PREFIX= "locked_down"
DEFAULT_EXECUTIONS     = 1

app = Flask(__name__)

# global in-memory history (newest first)
queue_history = []

def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASS

def authenticate():
    """Sends a 401 response that enables basic auth"""
    return Response(
        "Authentication required", 401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# Add this _before_ your other routes:
@app.route('/output_images/<path:filename>')
@requires_auth
def serve_image(filename):
    """Serve a single image file from the output directory."""
    return send_from_directory(OUTPUT_FOLDER, filename)

@app.route("/gallery_images.json")
@requires_auth
def gallery_images_json():
    try:
        imgs = [
            f for f in os.listdir(OUTPUT_FOLDER)
            if f.lower().endswith(('.png','.jpg','jpeg','gif'))
        ]
        # sort descending by modified time
        imgs.sort(
            key=lambda fn: os.path.getmtime(os.path.join(OUTPUT_FOLDER, fn)),
            reverse=True
        )
        return jsonify(imgs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/gallery')
@requires_auth
def gallery():
    images = [
        f for f in os.listdir(OUTPUT_FOLDER)
        if f.lower().endswith(('.png','.jpg','.jpeg','.gif'))
    ]
    images.sort(
      key=lambda fn: os.path.getmtime(os.path.join(OUTPUT_FOLDER, fn)),
      reverse=True
    )
    return render_template('gallery.html', images=images)

@app.route("/queue_status", methods=["GET"])
def queue_status():
    try:
        # Proxy the ComfyUI queue endpoint
        r = requests.get("http://127.0.0.1:8188/queue", timeout=5)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/", methods=["GET"])
@requires_auth
def index():
    return render_template("index.html")

@app.route("/generate_prompt", methods=["POST"])
@requires_auth
def generate_prompt():
    try:
        # Collect the raw pieces
        subject = request.form["subject"]
        pose    = request.form["pose"]
        setting = request.form["setting"]
        other   = request.form["other"]
          # extract seed

        # Build & transform
        raw = build_raw_prompt(subject, pose, setting, other)
        prompt = transform_prompt(raw)

        return jsonify({"prompt": prompt})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate_image", methods=["POST"])
@requires_auth
def generate_image():
    try:
        data   = request.get_json()
        prompt = data["prompt"]

        # 1) Apply your constants
        realism_lora  = DEFAULT_REALISM_LORA
        detail_lora   = DEFAULT_DETAIL_LORA
        workflow_type = DEFAULT_WORKFLOW_TYPE
        prefix        = DEFAULT_FILENAME_PREFIX
        executions    = DEFAULT_EXECUTIONS
        seed          = None  # ensures random per-run seeds below

        # 2) Choose which base workflow file to start from
        master_file = (
            "smoke_test.json" if workflow_type == "smoke"
            else "final_image.json"
        )
        output_file = f"{workflow_type}_updated.json"

        # 3) Inject prompt, LoRA strengths and prefix into the JSON once
        node_map = update_and_inject(
            master_file, output_file,
            prompt, realism_lora, detail_lora,
            seed, prefix
        )

        # 4) Enqueue `executions` jobs, each with its own random seed
        import uuid
        job_id = str(uuid.uuid4())
        for i in range(executions):
            this_seed = random.randint(0, 2**53 - 1)
            payload = {
                "prompt": node_map,
                "client_id": f"{job_id}_{i+1}",
                "filename_prefix": job_id,
            }
            # override the seed in every KSampler node
            for node in payload["prompt"].values():
                if node.get("class_type") == "KSampler":
                    node["inputs"]["seed"] = this_seed

            r = requests.post(
                "http://127.0.0.1:8188/prompt",
                json=payload,
                timeout=5
            )
            r.raise_for_status()

        # 5) Log exactly one history entry for the batch
        queue_history.insert(0, {
            "time":            datetime.utcnow().isoformat(),
            "prompt":          prompt,
            "realism_lora":    realism_lora,
            "detail_lora":     detail_lora,
            "workflow_type":   workflow_type,
            "seed":            None,
            "executions":      executions,
            "filename_prefix": prefix,
            "batch_id":        job_id
        })

        return jsonify({"job_id": r.text})

    except requests.RequestException as req_err:
        return jsonify({"error": f"ComfyUI request failed: {req_err}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# new endpoint to fetch the in-memory history
@app.route("/history", methods=["GET"])
def get_history():
    return jsonify(queue_history)

if __name__ == "__main__":
    app.run(debug=True)
