import os
import queue
import threading
import time
import urllib.parse
import traceback
from flask import Flask, render_template, request, jsonify, send_file, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

torrents: dict = {}
_session = None
_session_ready = threading.Event()   # set once session is fully up
_add_queue = queue.Queue()
_init_error = None


# ── Single libtorrent thread — all lt calls live here ────────────────────────
def lt_thread():
    global _session, _init_error
    try:
        import libtorrent as lt
        ses = lt.session()
        ses.apply_settings({
            "listen_interfaces": "0.0.0.0:6881",
            "alert_mask": 0,
            "dht_bootstrap_nodes": (
                "router.bittorrent.com:6881,"
                "router.utorrent.com:6881,"
                "dht.transmissionbt.com:6881"
            ),
        })
        ses.start_dht()
        ses.start_lsd()
        ses.start_upnp()
        _session = ses
        _session_ready.set()          # signal: ready for requests
    except Exception:
        _init_error = traceback.format_exc()
        _session_ready.set()          # unblock waiters even on error
        return

    import libtorrent as lt
    while True:
        _session.wait_for_alert(100)
        _session.pop_alerts()

        try:
            while True:
                magnet, save_path, result_q = _add_queue.get_nowait()
                try:
                    params = lt.parse_magnet_uri(magnet)
                    params.save_path = save_path
                    params.flags |= lt.torrent_flags.sequential_download
                    handle = _session.add_torrent(params)
                    tid = str(handle.info_hash())
                    result_q.put({"ok": True, "tid": tid, "handle": handle})
                except Exception as e:
                    result_q.put({"ok": False, "error": str(e),
                                  "trace": traceback.format_exc()})
        except queue.Empty:
            pass

        time.sleep(0.1)


threading.Thread(target=lt_thread, daemon=True, name="lt-thread").start()


# ── Helpers ───────────────────────────────────────────────────────────────────
def status_dict(tid, handle):
    try:
        import libtorrent as lt
        s = handle.status()
        ti = handle.torrent_file()
        name = (ti.name() if ti
                else torrents.get(tid, {}).get("name", "Fetching metadata…"))

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
    if not magnet.startswith("magnet:"):
        return jsonify({"error": "Invalid magnet link — must start with magnet:"}), 400

    # Wait up to 30s for session to be ready (covers cold-start delay)
    ready = _session_ready.wait(timeout=30)
    if not ready or _session is None:
        return jsonify({"error": "Torrent engine failed to start",
                        "detail": _init_error}), 503

    result_q = queue.Queue()
    _add_queue.put((magnet, DOWNLOAD_DIR, result_q))

    try:
        result = result_q.get(timeout=30)
    except queue.Empty:
        return jsonify({"error": "Timed out — engine busy, try again"}), 504

    if not result["ok"]:
        return jsonify({"error": result["error"],
                        "trace": result.get("trace")}), 500

    tid = result["tid"]
    handle = result["handle"]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
    dn = urllib.parse.unquote_plus(qs.get("dn", ["Unknown torrent"])[0])
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
    if _session:
        _session.remove_torrent(meta["handle"])
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


@app.route("/api/debug")
def debug():
    try:
        import libtorrent as lt
        return jsonify({
            "lt_version": lt.version,
            "session_ready": _session_ready.is_set(),
            "session_ok": _session is not None,
            "session_error": _init_error,
            "download_dir": DOWNLOAD_DIR,
            "download_dir_writable": os.access(DOWNLOAD_DIR, os.W_OK),
            "torrent_count": len(torrents),
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
