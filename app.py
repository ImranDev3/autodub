"""
Auto Dub — web server
---------------------
Upload a video, everything else happens automatically.

Run:      python3 app.py
Then go:  http://localhost:8000
"""

import os
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

from dubber import LANGUAGES, VOICES, DEFAULT_VOICE, dub_video, DubError

# ---------------------------------------------------------------- setup

BASE = Path(__file__).parent
UPLOADS = BASE / "uploads"
OUTPUTS = BASE / "outputs"
for folder in (UPLOADS, OUTPUTS):
    folder.mkdir(exist_ok=True)

ALLOWED = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a"}
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

# In-memory job store. Jobs are lost on restart — swap in Redis or a
# database before running this in production.
JOBS = {}
JOBS_LOCK = threading.Lock()

# How many videos may be processed at the same time
WORKER_LIMIT = threading.Semaphore(int(os.environ.get("CONCURRENCY", "2")))


def set_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


# ---------------------------------------------------------------- worker

def process(job_id, video_path, lang, voice, keep_bg):
    """Background worker: run the pipeline and record progress on the job."""
    with WORKER_LIMIT:
        set_job(job_id, status="processing", percent=3, message="Starting...")
        out_path = OUTPUTS / f"{job_id}.mp4"

        def progress(pct, msg):
            set_job(job_id, percent=pct, message=msg)

        try:
            result = dub_video(video_path, out_path, target_lang=lang,
                               voice=voice, keep_background=keep_bg,
                               progress=progress)
            set_job(job_id,
                    status="done", percent=100, message="Dubbing complete",
                    output=out_path.name,
                    srt=Path(result["srt"]).name,
                    segment_count=len(result["segments"]),
                    preview=[{"start": s["start"], "text": s["text"],
                              "original": s["original"]}
                             for s in result["segments"][:8]],
                    finished_at=datetime.now().isoformat())

        except DubError as exc:
            set_job(job_id, status="error", message=str(exc))
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            set_job(job_id, status="error", message=f"Unexpected error: {exc}")
        finally:
            # Drop the source upload once we're done — saves disk
            try:
                Path(video_path).unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("index.html", languages=LANGUAGES,
                           voices=VOICES, default_voice=DEFAULT_VOICE,
                           max_mb=MAX_MB)


@app.post("/upload")
def upload():
    """Accept a file, queue a job, return its id for polling."""
    file = request.files.get("video")
    if not file or not file.filename:
        return jsonify(error="No file was selected."), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        return jsonify(error=f"Unsupported file type ({ext})."), 400

    job_id = uuid.uuid4().hex[:12]
    safe_name = secure_filename(file.filename) or f"video{ext}"
    saved = UPLOADS / f"{job_id}_{safe_name}"
    file.save(saved)

    lang = request.form.get("lang", "bn")
    voice = request.form.get("voice", DEFAULT_VOICE)
    keep_bg = request.form.get("keep_bg", "1") == "1"

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "status": "queued", "percent": 0,
            "message": "Waiting in queue...",
            "filename": safe_name, "lang": lang, "voice": voice,
            "created_at": datetime.now().isoformat(),
        }

    threading.Thread(target=process, daemon=True,
                     args=(job_id, saved, lang, voice, keep_bg)).start()

    return jsonify(job_id=job_id)


@app.get("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found."), 404
    return jsonify(job)


@app.get("/jobs")
def jobs():
    """Twenty most recent jobs, newest first."""
    with JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j["created_at"],
                       reverse=True)[:20]
    return jsonify(items)


@app.get("/watch/<job_id>")
def watch(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return redirect(url_for("index"))
    return render_template("watch.html", job=job)


@app.get("/file/<path:name>")
def file(name):
    return send_from_directory(OUTPUTS, name)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"File is too large. Maximum is {MAX_MB} MB."), 413


if __name__ == "__main__":
    if not os.environ.get("GEMINI_API_KEY"):
        print("\n⚠️  GEMINI_API_KEY is not set!")
        print("   Get one: https://aistudio.google.com/apikey")
        print("   Then:    export GEMINI_API_KEY=your_key\n")

    port = int(os.environ.get("PORT", "8000"))
    print(f"\n🎬 Auto Dub running at  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
