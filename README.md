# 🌊 Torrent Loader — Setup & Deployment Guide

A self-hosted, browser-based torrent client built with Flask and libtorrent.
Paste magnet links → watch downloads → grab files. No desktop client needed.

---

## 📁 Project Structure

```
torrent-app/
├── app.py              ← Flask backend (all server logic)
├── templates/
│   └── index.html      ← Browser UI
├── requirements.txt    ← Python packages
├── build.sh            ← Render build script
├── render.yaml         ← Render deployment config
└── .gitignore
```

---

## 🚀 Deployment on Render (Free Tier) — Step by Step

### Step 1 — Push to GitHub

1. Create a new repository on [github.com](https://github.com) (call it `torrent-loader`)
2. In your terminal (or GitHub Desktop), initialize and push:

```bash
cd torrent-app
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/torrent-loader.git
git push -u origin main
```

> **Tip:** If you've never used Git from the terminal, GitHub Desktop makes this visual and easy. Download it at desktop.github.com.

---

### Step 2 — Create a Render Account

1. Go to [render.com](https://render.com) and sign up (free)
2. Connect your GitHub account when prompted

---

### Step 3 — Create a New Web Service

1. On the Render dashboard, click **"New +"** → **"Web Service"**
2. Choose **"Build and deploy from a Git repository"**
3. Select your `torrent-loader` repo
4. Fill in the settings:

| Setting | Value |
|---|---|
| **Name** | `torrent-loader` (or anything you like) |
| **Region** | Choose closest to you |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `bash build.sh` |
| **Start Command** | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120` |
| **Instance Type** | Free |

5. Scroll down to **Environment Variables** and add:
   - `SECRET_KEY` → click **"Generate"** button
   - `DOWNLOAD_DIR` → `/downloads`

6. Scroll to **Disks** (under Advanced) and add a disk:
   - **Name:** `downloads`
   - **Mount Path:** `/downloads`
   - **Size:** 10 GB (free tier allows this)

7. Click **"Create Web Service"**

---

### Step 4 — Wait for the Build

Render will:
1. Clone your repo
2. Run `build.sh` which installs libtorrent + Python packages
3. Start the app with gunicorn

This takes **3–5 minutes** the first time. You can watch the build log in real time.

Once you see `==> Your service is live 🎉`, you're done!

---

### Step 5 — Open Your App

Render gives you a URL like:
```
https://torrent-loader.onrender.com
```

Open it in any browser. That's your torrent client! Bookmark it.

---

## 🖥️ Using the App

### Adding a Torrent
1. Find a magnet link (from any torrent site)
2. Copy the full `magnet:?xt=urn:btih:...` link
3. Paste it into the text box and click **Add Torrent** (or press Enter)
4. The torrent appears in the list and starts downloading automatically

### Monitoring Progress
- The progress bar fills as data downloads
- **↓** = download speed, **↑** = upload speed
- State badges: `Downloading` → `Seeding` when complete

### Downloading Files
- When a torrent reaches **100%**, click **"↓ Download"** next to each file
- The file streams directly from the server to your browser

### Pause / Resume / Remove
- **⏸ Pause** — stops the torrent temporarily (resumes later)
- **▶ Resume** — restarts a paused torrent
- **✕ Remove** — removes the torrent from the list (files stay on server disk)

---

## ⚠️ Important Limitations (Free Tier)

| Limitation | Details |
|---|---|
| **Sleep after inactivity** | Free Render services sleep after 15 min of no web requests. Torrents pause when sleeping. Open the page periodically to keep it awake, or upgrade to a paid plan. |
| **Disk persistence** | The 10 GB disk persists between deploys. Files are safe unless you manually delete them. |
| **No auth by default** | The app has no login. Anyone with your URL can use it. See Security section below. |
| **Single worker** | Only one gunicorn worker — fine for personal use, not for multiple simultaneous users. |

---

## 🔒 Adding Basic Authentication (Optional but Recommended)

Since your app is public on the internet, add a simple password. In `app.py`, add this near the top after the imports:

```python
from functools import wraps
from flask import request, Response

USERNAME = os.environ.get("APP_USER", "admin")
PASSWORD = os.environ.get("APP_PASS", "changeme")

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != USERNAME or auth.password != PASSWORD:
            return Response("Unauthorized", 401,
                {"WWW-Authenticate": 'Basic realm="Torrent Loader"'})
        return f(*args, **kwargs)
    return decorated
```

Then decorate all routes:
```python
@app.route("/")
@require_auth
def index():
    ...
```

Add `APP_USER` and `APP_PASS` environment variables in Render dashboard.

---

## 🛠️ Running Locally (for testing)

### Prerequisites
- Python 3.10+
- libtorrent (the hard part — see below)

### Install libtorrent

**Ubuntu/Debian/WSL:**
```bash
sudo apt-get install python3-libtorrent
```

**macOS (Homebrew):**
```bash
brew install libtorrent-rasterbar
pip3 install python-libtorrent
```

**Windows:** Use WSL (Ubuntu), then follow the Ubuntu steps above.

### Run the App

```bash
cd torrent-app
pip install flask gunicorn
python app.py
```

Then open `http://localhost:5000` in your browser.

---

## 🔧 Troubleshooting

### Build fails on Render
- Check the build log for the specific error
- Most common: `apt-get` permission issues — make sure `build.sh` is executable (`git update-index --chmod=+x build.sh`)

### Torrent stuck on "Fetching metadata…"
- This is normal for new magnet links — libtorrent needs to find peers that have the torrent's metadata
- Can take 30 seconds to several minutes depending on the torrent's health (number of seeders)
- If stuck for over 10 minutes, the torrent may be dead (no seeders)

### App sleeping on Render free tier
- The free tier spins down after 15 minutes of inactivity
- Your downloads pause when this happens
- Fix: Use [UptimeRobot](https://uptimerobot.com) (free) to ping your URL every 10 minutes, keeping it awake

### Files not appearing for download
- Files only show the download button when progress = 100%
- The file must exist at the path listed — if the torrent was removed and re-added, it may re-check

---

## 📦 Tech Stack

| Component | Technology |
|---|---|
| Backend | Flask (Python) |
| Torrent engine | libtorrent (python-libtorrent) |
| WSGI server | Gunicorn |
| Frontend | Vanilla HTML/CSS/JS (no frameworks) |
| Hosting | Render.com |
| File storage | Render Persistent Disk |

---

## 🗺️ Possible Future Improvements

- [ ] HTTP Basic Auth (password protect)
- [ ] File browser for managing downloaded files
- [ ] Torrent file upload (`.torrent` files, not just magnet links)
- [ ] RSS feed support for automatic downloads
- [ ] Progress notifications (email/push when download completes)
- [ ] Dark/light theme toggle

---

*Built for personal use. Respect copyright law and your ISP's terms of service.*
