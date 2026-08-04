import io
import os
import re
import time
import zipfile

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
THUMB_DIR = os.path.join(UPLOAD_DIR, ".thumbs")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
MAX_CONTENT_MB = int(os.environ.get("MAX_CONTENT_MB", "50"))

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_MB * 1024 * 1024

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


def _list_images():
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
    return render_template("upload.html")


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
    return redirect(url_for("gallery"))


@app.route("/gallery")
def gallery():
    return render_template("gallery.html", images=_list_images())


@app.route("/image/<path:name>")
def image(name):
    name = secure_filename(name)
    if not _allowed(name):
        abort(404)
    return send_from_directory(UPLOAD_DIR, name)


@app.route("/thumb/<path:name>")
def thumb(name):
    name = secure_filename(name)
    if not _allowed(name):
        abort(404)
    src = os.path.join(UPLOAD_DIR, name)
    if not os.path.isfile(src):
        abort(404)
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
