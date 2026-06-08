# Context

Deploy this Flask/SQLite NFT trading dashboard to PythonAnywhere so 3 teammates can access it over the internet. Auth via Google OAuth (Flask-Dance) so teammates log in with their Google accounts — no passwords to manage, and access is revocable per-email. PythonAnywhere handles the web server infrastructure; Gunicorn is NOT needed (PythonAnywhere uses its own WSGI runner).

**Platform requirement: paid PythonAnywhere plan** (Hacker ~$5/mo minimum) because:
- Free tier blocks outbound HTTP to non-whitelisted domains — OpenSea API won't work
- Scheduled tasks require a paid account (free accounts created after Jan 15, 2026 lost scheduled task access)
- Custom domain support (optional but needed if you want your own URL for the Google OAuth redirect)

---

# Part 1: Code Changes

Already applied to `app.py` and `requirements.txt`. For reference:

- `requirements.txt` — created with `flask`, `requests`, `python-dotenv`, `tabulate`, `flask-dance`, `flask-login`
- `app.py` — Google OAuth added via `oauth_authorized` signal (Flask-Dance); all routes protected with `@login_required`

### `.env` — add new variables

```
OPENSEA_API_KEY=...
SECRET_KEY=<generate: python -c "import secrets; print(secrets.token_hex(32))">
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
ALLOWED_EMAILS=teammate1@gmail.com,teammate2@gmail.com,yourname@gmail.com
OAUTHLIB_RELAX_TOKEN_SCOPE=true
OAUTHLIB_INSECURE_TRANSPORT=1   # local dev only — do NOT set on server
```

---

# Part 2: Google Cloud OAuth App Setup (one-time)

1. Go to https://console.cloud.google.com → New Project
2. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
3. Application type: **Web application**
4. Authorized redirect URIs:
   - `https://yourusername.pythonanywhere.com/login/google/authorized`
   - `http://localhost:5001/login/google/authorized` (local dev)
5. Copy Client ID and Client Secret → paste into `.env`

---

# Part 3: PythonAnywhere Deployment

### 3a. Checkpoint WAL before uploading DB

Run this locally first so the database is in a clean state for upload:
```bash
python -c "import sqlite3; c=sqlite3.connect('collection_trades.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
```

### 3b. Upload code

In PythonAnywhere Bash console:
```bash
git clone https://github.com/yourrepo/nft-dashboard.git ~/nft-dashboard
```
Or upload the files manually via the Files tab if not on GitHub.

Upload/create `.env` at `/home/yourusername/nft-dashboard/.env` with all variables (omit `OAUTHLIB_INSECURE_TRANSPORT` on the server).

Upload the SQLite database: `/home/yourusername/nft-dashboard/collection_trades.db` (225MB — use the Files tab or scp).

### 3c. Create virtualenv and install deps

```bash
mkvirtualenv --python=python3.10 nft-dashboard
cd ~/nft-dashboard
pip install -r requirements.txt
```

Use `workon nft-dashboard` to reactivate later.

### 3d. WSGI file

In PythonAnywhere → Web tab → click your app → WSGI configuration file link.

Replace the entire contents with:
```python
import sys, os
from dotenv import load_dotenv

path = '/home/yourusername/nft-dashboard'
sys.path.insert(0, path)
load_dotenv(os.path.join(path, '.env'))

from app import app as application
```

### 3e. Web app settings (Web tab)

- **Source code**: `/home/yourusername/nft-dashboard`
- **Working directory**: `/home/yourusername/nft-dashboard`
- **Python version**: 3.10 (or match your local version)
- **Virtualenv**: `/home/yourusername/.virtualenvs/nft-dashboard`
- **Do NOT add a static files mapping** — Flask must serve `index.html` through the auth layer
- Hit **Reload** after any code or config change

### 3f. Scheduled task for `update_all.py`

Tasks tab → Add a new scheduled task:
```bash
cd /home/yourusername/nft-dashboard && /home/yourusername/.virtualenvs/nft-dashboard/bin/python update_all.py
```
Frequency: Daily (or set multiple tasks at staggered times for more frequent updates).

---

# Part 4: Security Checklist

| Concern | Solution |
|---------|----------|
| Strangers accessing the app | Google OAuth — only whitelisted emails can log in |
| Data in transit | PythonAnywhere provides HTTPS automatically on *.pythonanywhere.com |
| API keys exposed | `.env` file, not committed to git (already in `.gitignore`) |
| Flask debug mode | `debug=True` is already wrapped in `if __name__ == '__main__':` — PythonAnywhere never runs it |
| Session hijacking | `SESSION_COOKIE_SECURE=True`, `HTTPONLY=True`, `SAMESITE=Lax` |
| Revoking a user | Remove their email from `ALLOWED_EMAILS` in `.env`, reload the app |

---

# Part 5: Verification

1. Visit `https://yourusername.pythonanywhere.com` → should redirect to Google sign-in
2. Sign in with a whitelisted email → should land on the dashboard
3. Sign in with a non-whitelisted email → should get 403 Forbidden
4. `/logout` → session cleared, redirected to login
5. `/api/collections` without being logged in → redirects to login (not raw JSON)
6. `https://yourusername.pythonanywhere.com/static/index.html` → redirects to login (not the raw page)
7. Check PythonAnywhere error log (Web tab → Log files) if anything fails
