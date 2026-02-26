import os
import threading
import time
import urllib.parse
import traceback
from flask import Flask, render_template, request, jsonify, send_file, abort
import libtorrent as lt

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "./downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

torrents: dict = {}
_session = None
_session_lock = threading.Lock()


def get_session():
    global _session
    with _session_lock:
        if _session is None:
            # libtorrent 2.x: apply_settings() takes a plain dict
            _session = lt.session()
            _session.apply_settings({
                "listen_interfaces": "0.0.0.0:6881",
                "alert_mask": lt.alert.category_t.all_categories,
                "dht_bootstrap_nodes": (
                    "router.bittorrent.com:6881,"
                    "router.utorrent.com:6881,"
                    "dht.transmissionbt.com:6881"
                ),
            })
            _session.start_dht()
            _session.start_lsd()
            _session.start_upnp()
    return _session


def torrent_worker():
    ses = get_session()
    while True:
        ses.wait_for_alert(500)
        ses.pop_alerts()
        time.sleep(0.2)


threading.Thread(target=torrent_worker, daemon=True).start()


def handle_id(handle):
    try:
        return str(handle.info_hash())
    except Exception:
        return None


def status_dict(tid, handle):
    try:
        s = handle.status()
        ti = handle.torrent_file()
        name = ti.name() if ti else torrents.get(tid, {}).get("name", "Fetching metadata…")

        files = []
        if ti:
            fs = ti.files()
            for i in range(fs.num_files()):
                files.append({"path": fs.file_path(i), "size": fs.file_size(i)})

        try:
            state_str = s.state.name
        except AttributeError:
            state_str = str(s.state).split(".")[-1]

        try:
            paused = bool(s.flags & lt.torrent_flags.paused)
        except Exception:
            paused = getattr(s, "paused", False)

        try:
            error = s.errc.message() if s.errc.value() else None
        except Exception:
            error = getattr(s, "error", None) or None

        return {
            "id": tid,
            "name": name,
            "progress": round(s.progress * 100, 1),
            "download_rate": s.download_rate,
            "upload_rate": s.upload_rate,
            "num_peers": s.num_peers,
            "state": state_str,
            "total_done": s.total_done,
            "total_wanted": s.total_wanted,
            "paused": paused,
            "error": error,
            "files": files,
        }
    except Exception as e:
        return {
            "id": tid,
            "name": torrents.get(tid, {}).get("name", "Unknown"),
            "error": str(e),
            "progress": 0,
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/add", methods=["POST"])
def add_torrent():
    data = request.get_json(force=True)
    magnet = data.get("magnet", "").strip()
    if not magnet:
        return jsonify({"error": "No magnet link provided"}), 400
    if not magnet.startswith("magnet:"):
        return jsonify({"error": "Invalid magnet link — must start with magnet:"}), 400

    try:
        ses = get_session()

        params = lt.parse_magnet_uri(magnet)
        params.save_path = DOWNLOAD_DIR
        params.flags |= lt.torrent_flags.sequential_download

        handle = ses.add_torrent(params)
        tid = handle_id(handle)
        if not tid:
            return jsonify({"error": "Could not extract info hash"}), 400

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
        dn = urllib.parse.unquote_plus(qs.get("dn", ["Unknown torrent"])[0])

        torrents[tid] = {"handle": handle, "name": dn}
        return jsonify({"id": tid, "name": dn})

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/torrents")
def list_torrents():
    result = []
    for tid, meta in list(torrents.items()):
        h = meta["handle"]
        if h.is_valid():
            result.append(status_dict(tid, h))
    return jsonify(result)


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
    get_session().remove_torrent(meta["handle"])
    return jsonify({"ok": True})


@app.route("/api/download/<tid>/<path:filepath>")
def download_file(tid, filepath):
    safe_path = os.path.realpath(os.path.join(DOWNLOAD_DIR, filepath))
    base = os.path.realpath(DOWNLOAD_DIR)
    if not safe_path.startswith(base):
        abort(403)
    if not os.path.exists(safe_path):
        abort(404)
    return send_file(safe_path, as_attachment=True)


# ── Debug endpoint — shows real errors ───────────────────────────────────────
@app.route("/api/debug")
def debug():
    try:
        ses = get_session()
        return jsonify({
            "lt_version": lt.version,
            "session_ok": ses is not None,
            "download_dir": DOWNLOAD_DIR,
            "download_dir_exists": os.path.exists(DOWNLOAD_DIR),
            "torrent_count": len(torrents),
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
