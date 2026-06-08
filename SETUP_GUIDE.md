# Deployment Setup Guide

Follow these steps in order. Each section builds on the previous one.

---

## Step 1 — Generate a secret key (local, right now)

Run this in your terminal:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output. You'll paste it as `SECRET_KEY` in Step 3.

---

## Step 2 — Update the code (local)

### 2a. Create `requirements.txt` in the project root

Create the file with these contents:

```
flask
requests
python-dotenv
tabulate
flask-dance
flask-login
```

### 2b. Replace `app.py` with the auth-enabled version

Full replacement — see `DEPLOYMENT.md` Part 1b for the exact code blocks to add.
The changes are:
- New imports at the top
- Auth setup after `app = Flask(...)` 
- Three new routes: `/login`, `/login/callback`, `/logout`
- Add `@login_required` to the three existing routes (`/`, `/api/collections`, `/api/chart-data`)

### 2c. Add a logout link to `static/index.html`

Find the bottom of the `<div class="sidebar">` and add before the closing `</div>`:

```html
<a href="/logout" style="color:#64748b;font-size:12px;text-decoration:none;padding:8px 0;display:block;">Sign out</a>
```

### 2d. Test locally that the app still imports cleanly

```bash
python -c "from app import app; print('OK')"
```

You'll get an error about missing env vars — that's expected at this stage. What you should NOT see is a syntax error.

---

## Step 3 — Set up Google OAuth (Google Cloud Console)

1. Go to **https://console.cloud.google.com**
2. Click the project dropdown (top left) → **New Project** → name it anything → **Create**
3. In the left menu: **APIs & Services** → **OAuth consent screen**
   - User type: **External** → **Create**
   - App name: anything (e.g. "NFT Dashboard")
   - User support email: your email
   - Developer contact: your email
   - Click **Save and Continue** through all screens (no need to add scopes manually)
   - On the last screen, click **Back to Dashboard**
4. Left menu: **APIs & Services** → **Credentials** → **+ Create Credentials** → **OAuth 2.0 Client ID**
   - Application type: **Web application**
   - Name: anything
   - Under **Authorized redirect URIs**, click **+ Add URI** and add:
     ```
     http://localhost:5001/login/google/authorized
     ```
     (You'll add the PythonAnywhere URI here in Step 7 after you know your username)
   - Click **Create**
5. A popup shows your **Client ID** and **Client Secret** — copy both

---

## Step 4 — Update your `.env` file

Add these lines to your existing `.env`:

```
SECRET_KEY=<paste from Step 1>
GOOGLE_CLIENT_ID=<paste from Step 3>
GOOGLE_CLIENT_SECRET=<paste from Step 3>
ALLOWED_EMAILS=you@gmail.com,teammate2@gmail.com,teammate3@gmail.com
OAUTHLIB_RELAX_TOKEN_SCOPE=true
```

Replace the email addresses with the actual Google accounts your team uses.

---

## Step 5 — Test Google login locally

```bash
python app.py
```

Visit `http://localhost:5001` — it should redirect you to Google's sign-in page.
After signing in with a whitelisted email, you should land on the dashboard.
Visit `http://localhost:5001/logout` — should redirect back to login.

If you get an error, check the terminal output for the exact message.

---

## Step 6 — Sign up for PythonAnywhere

1. Go to **https://www.pythonanywhere.com** → **Start running Python online**
2. Create an account — note your **username** (you'll use it as `yourusername` everywhere below)
3. Upgrade to the **Hacker plan** (~$5/mo) — required for:
   - Unrestricted outbound HTTP (OpenSea API)
   - Scheduled tasks (auto-updating data)

---

## Step 7 — Add your PythonAnywhere URL to Google OAuth

1. Go back to **https://console.cloud.google.com** → APIs & Services → Credentials → click your OAuth client
2. Under **Authorized redirect URIs**, add:
   ```
   https://yourusername.pythonanywhere.com/login/google/authorized
   ```
   (replace `yourusername` with your actual PythonAnywhere username)
3. Click **Save**

---

## Step 8 — Push code to GitHub (or upload manually)

**Option A — GitHub (recommended):**

If this repo isn't on GitHub yet:
```bash
git remote add origin https://github.com/yourgithubname/nft-dashboard.git
git push -u origin main
```

Make sure `.env` and `*.db` are in `.gitignore` — they already are, so the API keys and database won't be pushed.

**Option B — Manual upload:**

Skip to Step 9 and use the PythonAnywhere Files tab to upload files one by one. The database (216MB) will need to be uploaded separately — use the Files tab uploader or `scp` if you have SSH access.

---

## Step 9 — Upload code to PythonAnywhere

In the PythonAnywhere **Bash console** (Dashboard → New console → Bash):

**If using GitHub:**
```bash
git clone https://github.com/yourgithubname/nft-dashboard.git ~/nft-dashboard
cd ~/nft-dashboard
pip install -r requirements.txt --user
```

**If uploading manually:**
```bash
mkdir ~/nft-dashboard
# Then upload files via Files tab to /home/yourusername/nft-dashboard/
cd ~/nft-dashboard
pip install -r requirements.txt --user
```

---

## Step 10 — Upload the database

The `collection_trades.db` file is ~216MB and not in git. Upload it separately:

- **Files tab**: Navigate to `/home/yourusername/nft-dashboard/` → Upload file → select `collection_trades.db`
- (If the Files tab upload times out on large files, use the Bash console to download it from a file share, or use `scp` if you have SSH access on the paid plan)

---

## Step 11 — Create the `.env` file on PythonAnywhere

In the Bash console:

```bash
nano ~/nft-dashboard/.env
```

Paste in your full `.env` contents (same as your local one, but `OAUTHLIB_INSECURE_TRANSPORT` should NOT be set — you only need it locally if testing over plain http).

Save: `Ctrl+O` → Enter → `Ctrl+X`

Set permissions so only your user can read it:
```bash
chmod 600 ~/nft-dashboard/.env
```

---

## Step 12 — Create the web app on PythonAnywhere

1. Dashboard → **Web** tab → **Add a new web app**
2. Click through until you choose a framework → select **Manual configuration**
3. Choose **Python 3.10** (or match your local version)
4. Click **Next** — the web app is created

---

## Step 13 — Configure the WSGI file

1. Web tab → click the link under **WSGI configuration file** (something like `/var/www/yourusername_pythonanywhere_com_wsgi.py`)
2. Delete everything in the file and replace with:

```python
import sys, os
from dotenv import load_dotenv

path = '/home/yourusername/nft-dashboard'
sys.path.insert(0, path)
load_dotenv(os.path.join(path, '.env'))

from app import app as application
```

(Replace `yourusername` with your actual username)

3. **Save** the file

---

## Step 14 — Configure web app settings

Still on the **Web** tab:

- **Source code**: `/home/yourusername/nft-dashboard`
- **Working directory**: `/home/yourusername/nft-dashboard`

Scroll down to **Static files**:
- URL: `/static/`
- Directory: `/home/yourusername/nft-dashboard/static`
- Click the checkmark to save

---

## Step 15 — Reload and test

1. Web tab → click the green **Reload** button at the top
2. Visit `https://yourusername.pythonanywhere.com`
   - Should redirect to Google sign-in
   - After signing in with a whitelisted email → dashboard loads
   - After signing in with a non-whitelisted email → 403 page
3. If anything goes wrong, Web tab → **Error log** shows the Python traceback

---

## Step 16 — Set up the scheduled data update

1. Dashboard → **Tasks** tab
2. **Add a new scheduled task**:
   - Command: `cd /home/yourusername/nft-dashboard && python update_all.py`
   - Choose a time (e.g. 06:00 UTC for a daily morning refresh)
3. Click the checkmark to save

To run it immediately and check it works:
```bash
cd ~/nft-dashboard && python update_all.py
```

---

## You're done

Your team can now access the dashboard at:
```
https://yourusername.pythonanywhere.com
```

They sign in with their Google account. To add or remove a user, edit `ALLOWED_EMAILS` in `~/nft-dashboard/.env` on PythonAnywhere and click **Reload** on the Web tab.
