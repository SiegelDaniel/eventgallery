import io
import os
import shutil
import time
import zipfile
from functools import wraps

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
THUMB_DIR = os.path.join(UPLOAD_DIR, ".thumbs")
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".3gp", ".avi", ".mkv"}
ALLOWED_EXT = ALLOWED_IMAGE_EXT | ALLOWED_VIDEO_EXT
# Videos are large — allow a big body by default. nginx client_max_body_size must match.
MAX_CONTENT_MB = int(os.environ.get("MAX_CONTENT_MB", "512"))
GALLERY_PASSWORD = os.environ.get("GALLERY_PASSWORD", "romanraucht")
# Optional cap for the storage bar; falls back to the real filesystem free space.
STORAGE_QUOTA_MB = os.environ.get("STORAGE_QUOTA_MB")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_MB * 1024 * 1024


def _load_secret_key():
    """A stable key shared across gunicorn workers and restarts.

    Prefer $SECRET_KEY; otherwise persist a generated key to disk so every
    worker signs sessions with the same value (random per-worker keys would
    make logins appear to drop at random)."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    key_path = os.path.join(UPLOAD_DIR, ".secret_key")
    try:
        with open(key_path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        key = os.urandom(32)
        with open(key_path, "wb") as fh:
            fh.write(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return key


app.secret_key = _load_secret_key()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            # Only GET targets can be resumed via a redirect after login;
            # POST endpoints (delete/download) would 405, so fall back to gallery.
            nxt = request.path if request.method == "GET" else url_for("gallery")
            return redirect(url_for("login", next=nxt))
        return view(*args, **kwargs)

    return wrapped

# Honor an optional URL prefix (e.g. /eventgallery) set by the reverse proxy,
# so url_for() generates correct links when mounted under a sub-path.
APPLICATION_ROOT = os.environ.get("APPLICATION_ROOT", "")
if APPLICATION_ROOT:
    app.config["APPLICATION_ROOT"] = APPLICATION_ROOT
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    def _not_found(environ, start_response):
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found"]

    app.wsgi_app = DispatcherMiddleware(_not_found, {APPLICATION_ROOT: app.wsgi_app})


def _allowed(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXT


def _is_video(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_VIDEO_EXT


# Expose the video check to templates so the gallery can pick <img> vs <video>.
app.jinja_env.globals["is_video"] = _is_video


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _storage_stats():
    """Bytes used/free/total for the upload area, plus human-readable strings.

    Uses STORAGE_QUOTA_MB as the cap if set, otherwise the real filesystem."""
    used_uploads = _dir_size(UPLOAD_DIR)
    if STORAGE_QUOTA_MB:
        total = int(STORAGE_QUOTA_MB) * 1024 * 1024
        used = used_uploads
        free = max(total - used, 0)
    else:
        du = shutil.disk_usage(UPLOAD_DIR)
        total, used, free = du.total, du.used, du.free
    percent = round(used / total * 100, 1) if total else 0
    return {
        "used": used,
        "free": free,
        "total": total,
        "percent": percent,
        "used_h": _human_bytes(used),
        "free_h": _human_bytes(free),
        "total_h": _human_bytes(total),
    }


def _safe_name(name):
    """Return a stored filename that is safe and unique."""
    base = secure_filename(name) or "image"
    root, ext = os.path.splitext(base)
    ext = ext.lower()
    candidate = base
    counter = 1
    while os.path.exists(os.path.join(UPLOAD_DIR, candidate)):
        candidate = f"{root}_{counter}{ext}"
        counter += 1
    return candidate


def _list_media():
    files = []
    for name in os.listdir(UPLOAD_DIR):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(path) and _allowed(name):
            files.append((name, os.path.getmtime(path)))
    files.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in files]


def _valid_selection(names):
    """Filter posted names down to files that actually exist in UPLOAD_DIR."""
    valid = []
    for name in names:
        safe = secure_filename(name)
        if safe and _allowed(safe):
            path = os.path.join(UPLOAD_DIR, safe)
            if os.path.isfile(path):
                valid.append(safe)
    return valid


@app.route("/")
def index():
    return render_template("upload.html", storage=_storage_stats())


@app.route("/storage")
def storage():
    return jsonify(_storage_stats())


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("photos")
    saved = 0
    for f in files:
        if not f or not f.filename:
            continue
        if not _allowed(f.filename):
            continue
        f.save(os.path.join(UPLOAD_DIR, _safe_name(f.filename)))
        saved += 1
    # Uploads happen via fetch(); stay on the upload page instead of redirecting.
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"saved": saved})
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == GALLERY_PASSWORD:
            session["authed"] = True
            # Only allow safe relative redirects (no open-redirect off-site).
            target = request.args.get("next") or ""
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("gallery")
            return redirect(target)
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("authed", None)
    return redirect(url_for("index"))


@app.route("/gallery")
@login_required
def gallery():
    return render_template("gallery.html", images=_list_media())


@app.route("/image/<path:name>")
@login_required
def image(name):
    name = secure_filename(name)
    if not _allowed(name):
        abort(404)
    return send_from_directory(UPLOAD_DIR, name)


@app.route("/thumb/<path:name>")
@login_required
def thumb(name):
    name = secure_filename(name)
    if not _allowed(name):
        abort(404)
    src = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(src):
        abort(404)
    # No image thumbnail for videos — the gallery renders those with <video>.
    if _is_video(name):
        return send_from_directory(UPLOAD_DIR, name)
    thumb_path = os.path.join(THUMB_DIR, name + ".jpg")
    if not os.path.exists(thumb_path) or os.path.getmtime(thumb_path) < os.path.getmtime(src):
        try:
            from PIL import Image, ImageOps

            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((400, 400))
                im.save(thumb_path, "JPEG", quality=80)
        except Exception:
            # Fall back to serving the original if thumbnailing fails.
            return send_from_directory(UPLOAD_DIR, name)
    return send_file(thumb_path, mimetype="image/jpeg")


@app.route("/download", methods=["POST"])
@login_required
def download():
    names = _valid_selection(request.form.getlist("selected"))
    if not names:
        return redirect(url_for("gallery"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.write(os.path.join(UPLOAD_DIR, name), arcname=name)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"eventgallery_{int(time.time())}.zip",
    )


@app.route("/delete", methods=["POST"])
@login_required
def delete():
    names = _valid_selection(request.form.getlist("selected"))
    for name in names:
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
        except OSError:
            pass
        thumb_path = os.path.join(THUMB_DIR, name + ".jpg")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
    return redirect(url_for("gallery"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True)
