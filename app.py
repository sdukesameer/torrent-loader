import os
import threading
import time
import json
import hashlib
from flask import Flask, render_template, request, jsonify, send_file, abort
import libtorrent as lt

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

# ── Config ────────────────────────────────────────────────────────────────────
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "./downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory store: { handle_id: { meta... } }
torrents: dict = {}
session: lt.session = None


# ── libtorrent session ────────────────────────────────────────────────────────
def get_session():
    global session
    if session is None:
        settings = {
            "listen_interfaces": "0.0.0.0:6881",
            "alert_mask": lt.alert.category_t.all_categories,
        }
        session = lt.session(settings)
    return session


def torrent_worker():
    """Background thread that pumps libtorrent alerts."""
    ses = get_session()
    while True:
        ses.wait_for_alert(1000)
        alerts = ses.pop_alerts()
        for alert in alerts:
            pass  # libtorrent handles state internally
        time.sleep(0.5)


worker_thread = threading.Thread(target=torrent_worker, daemon=True)
worker_thread.start()


# ── Helpers ───────────────────────────────────────────────────────────────────
def handle_id(handle):
    """Stable string key for a torrent handle."""
    try:
        info_hash = str(handle.info_hash())
        return info_hash
    except Exception:
        return None


def status_dict(tid, handle):
    try:
        s = handle.status()
        ti = handle.torrent_file()
        name = ti.name() if ti else (torrents.get(tid, {}).get("name", "Fetching metadata…"))
        files = []
        if ti:
            fs = ti.files()
            for i in range(fs.num_files()):
                fp = fs.file_path(i)
                sz = fs.file_size(i)
                files.append({"path": fp, "size": sz})

        return {
            "id": tid,
            "name": name,
            "progress": round(s.progress * 100, 1),
            "download_rate": s.download_rate,
            "upload_rate": s.upload_rate,
            "num_peers": s.num_peers,
            "state": str(s.state),
            "total_done": s.total_done,
            "total_wanted": s.total_wanted,
            "paused": s.paused,
            "error": s.error if s.error else None,
            "files": files,
        }
    except Exception as e:
        return {"id": tid, "name": torrents.get(tid, {}).get("name", "Unknown"), "error": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/add", methods=["POST"])
def add_torrent():
    data = request.get_json(force=True)
    magnet = data.get("magnet", "").strip()
    if not magnet:
        return jsonify({"error": "No magnet link provided"}), 400

    ses = get_session()
    params = lt.parse_magnet_uri(magnet)
    params.save_path = DOWNLOAD_DIR

    handle = ses.add_torrent(params)
    handle.set_flags(lt.torrent_flags.sequential_download)

    tid = handle_id(handle)
    if tid is None:
        return jsonify({"error": "Could not parse magnet link"}), 400

    # Extract display name from magnet dn= param
    import urllib.parse
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
    dn = qs.get("dn", ["Unknown"])[0]

    torrents[tid] = {"handle": handle, "name": dn}
    return jsonify({"id": tid, "name": dn})


@app.route("/api/torrents")
def list_torrents():
    result = []
    for tid, meta in list(torrents.items()):
        h = meta["handle"]
        if h.is_valid():
            result.append(status_dict(tid, h))
    return jsonify(result)


@app.route("/api/torrent/<tid>")
def torrent_status(tid):
    meta = torrents.get(tid)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    return jsonify(status_dict(tid, meta["handle"]))


@app.route("/api/torrent/<tid>/pause", methods=["POST"])
def pause_torrent(tid):
    meta = torrents.get(tid)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    meta["handle"].pause()
    return jsonify({"ok": True})


@app.route("/api/torrent/<tid>/resume", methods=["POST"])
def resume_torrent(tid):
    meta = torrents.get(tid)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    meta["handle"].resume()
    return jsonify({"ok": True})


@app.route("/api/torrent/<tid>/remove", methods=["DELETE"])
def remove_torrent(tid):
    meta = torrents.pop(tid, None)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    ses = get_session()
    ses.remove_torrent(meta["handle"])
    return jsonify({"ok": True})


@app.route("/api/download/<tid>/<path:filepath>")
def download_file(tid, filepath):
    """Serve a completed file for download."""
    safe_path = os.path.realpath(os.path.join(DOWNLOAD_DIR, filepath))
    base = os.path.realpath(DOWNLOAD_DIR)
    if not safe_path.startswith(base):
        abort(403)
    if not os.path.exists(safe_path):
        abort(404)
    return send_file(safe_path, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
