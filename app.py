import os
import threading
import time
import urllib.parse
import traceback
import re
from flask import Flask, render_template, request, jsonify, send_file, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

torrents = {}
_session = None
_session_ready = threading.Event()
_init_error = None


def lt_thread():
    global _session, _init_error
    try:
        import libtorrent as lt

        ses = lt.session()
        ses.apply_settings({
            # Use port 443 (HTTPS) and 80 (HTTP) — almost never blocked by firewalls
            "listen_interfaces": "0.0.0.0:443,0.0.0.0:80,0.0.0.0:6881",
            "alert_mask": lt.alert.category_t.all_categories,
            "dht_bootstrap_nodes": (
                "router.bittorrent.com:6881,"
                "router.utorrent.com:6881,"
                "dht.transmissionbt.com:6881,"
                "dht.aelitis.com:6881"
            ),
            # Allow connections via HTTP proxy/tracker for better pierce-ability
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            # Use all connection types
            "use_dht_as_fallback": False,
            # Peer limits
            "connections_limit": 200,
            "connection_speed": 20,
        })
        ses.start_dht()
        ses.start_lsd()
        ses.start_upnp()
        _session = ses
        print("[lt] Session started OK")
    except Exception:
        _init_error = traceback.format_exc()
        print("[lt] Session init FAILED:", _init_error)
    finally:
        _session_ready.set()

    if _session is None:
        return

    import libtorrent as lt
    while True:
        alerts = []
        _session.wait_for_alert(500)
        alerts = _session.pop_alerts()
        for a in alerts:
            # Log important alerts for debugging
            if hasattr(a, 'what'):
                w = a.what()
                if 'error' in w or 'metadata' in w or 'torrent_added' in w:
                    tid_str = str(a.handle.info_hash()) if hasattr(a, 'handle') and a.handle.is_valid() else '?'
                    print(f"[lt alert] {w}: {a} (tid={tid_str})")
                    # If metadata received, update name
                    if 'metadata_received' in w and hasattr(a, 'handle') and a.handle.is_valid():
                        h = a.handle
                        tid = str(h.info_hash())
                        if tid in torrents and torrents[tid].get('handle') is None:
                            torrents[tid]['handle'] = h
        time.sleep(0.1)


threading.Thread(target=lt_thread, daemon=True, name="lt-thread").start()


def status_dict(tid, handle):
    try:
        import libtorrent as lt
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
            "id": tid, "name": name,
            "progress": round(s.progress * 100, 1),
            "download_rate": s.download_rate,
            "upload_rate": s.upload_rate,
            "num_peers": s.num_peers,
            "num_seeds": s.num_seeds,
            "state": state_str,
            "total_done": s.total_done,
            "total_wanted": s.total_wanted,
            "paused": paused, "error": error,
            "files": files,
        }
    except Exception as e:
        return {"id": tid, "name": torrents.get(tid, {}).get("name", "Unknown"), "error": str(e), "progress": 0}


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
        return jsonify({"error": "Must start with magnet:"}), 400

    if not _session_ready.wait(timeout=5):
        return jsonify({"error": "Engine still starting, try again"}), 503
    if _session is None:
        return jsonify({"error": "Engine failed", "detail": _init_error}), 503

    qs = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
    dn = urllib.parse.unquote_plus(qs.get("dn", ["Unknown torrent"])[0])

    ih_match = re.search(r'btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})', magnet, re.I)
    if not ih_match:
        return jsonify({"error": "No info hash in magnet link"}), 400
    prov_tid = ih_match.group(1).lower()

    torrents[prov_tid] = {"handle": None, "name": dn, "state": "adding"}

    def bg_add():
        try:
            import libtorrent as lt
            params = lt.parse_magnet_uri(magnet)
            params.save_path = DOWNLOAD_DIR
            params.flags |= lt.torrent_flags.sequential_download
            handle = _session.add_torrent(params)
            real_tid = str(handle.info_hash())
            torrents.pop(prov_tid, None)
            torrents[real_tid] = {"handle": handle, "name": dn}
            print(f"[add] Added torrent {real_tid} ({dn})")
        except Exception as e:
            print(f"[add] Failed: {e}")
            torrents[prov_tid] = {"handle": None, "name": dn, "error": str(e)}

    threading.Thread(target=bg_add, daemon=True).start()
    return jsonify({"id": prov_tid, "name": dn})


@app.route("/api/torrents")
def list_torrents():
    result = []
    for tid, meta in list(torrents.items()):
        h = meta.get("handle")
        if h is None:
            result.append({
                "id": tid, "name": meta.get("name", "Adding…"),
                "progress": 0, "state": "error" if meta.get("error") else "adding",
                "error": meta.get("error"), "paused": False,
                "download_rate": 0, "upload_rate": 0,
                "num_peers": 0, "num_seeds": 0, "files": [],
            })
        elif h.is_valid():
            result.append(status_dict(tid, h))
    return jsonify(result)


@app.route("/api/torrent/<tid>/pause", methods=["POST"])
def pause_torrent(tid):
    meta = torrents.get(tid)
    if not meta or not meta.get("handle"):
        return jsonify({"error": "Not found"}), 404
    meta["handle"].pause()
    return jsonify({"ok": True})


@app.route("/api/torrent/<tid>/resume", methods=["POST"])
def resume_torrent(tid):
    meta = torrents.get(tid)
    if not meta or not meta.get("handle"):
        return jsonify({"error": "Not found"}), 404
    meta["handle"].resume()
    return jsonify({"ok": True})


@app.route("/api/torrent/<tid>/remove", methods=["DELETE"])
def remove_torrent(tid):
    meta = torrents.pop(tid, None)
    if not meta:
        return jsonify({"error": "Not found"}), 404
    h = meta.get("handle")
    if h and _session:
        try:
            _session.remove_torrent(h)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/download/<tid>/<path:filepath>")
def download_file(tid, filepath):
    safe = os.path.realpath(os.path.join(DOWNLOAD_DIR, filepath))
    base = os.path.realpath(DOWNLOAD_DIR)
    if not safe.startswith(base):
        abort(403)
    if not os.path.exists(safe):
        abort(404)
    return send_file(safe, as_attachment=True)


@app.route("/api/debug")
def debug():
    try:
        import libtorrent as lt
        ses_info = {}
        if _session:
            try:
                ss = _session.status()
                ses_info = {
                    "dht_nodes": ss.dht_nodes,
                    "num_peers": ss.num_peers,
                    "download_rate": ss.download_rate,
                }
            except Exception:
                pass
        return jsonify({
            "lt_version": lt.version,
            "session_ready": _session_ready.is_set(),
            "session_ok": _session is not None,
            "session_error": _init_error,
            "download_dir": DOWNLOAD_DIR,
            "download_dir_writable": os.access(DOWNLOAD_DIR, os.W_OK),
            "torrent_count": len(torrents),
            "session_stats": ses_info,
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
