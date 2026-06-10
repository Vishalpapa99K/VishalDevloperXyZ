import json
import os
import secrets
import hashlib
import hmac
import base64
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, make_response, send_from_directory
from functools import wraps
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from pymongo import MongoClient

# ══════════════════════════════════════════════════════════════════
# ENV LOADER — supports .env file (no external dep)
# Load order: ENV_FILE env var > alonexraj.env > .env
# Existing os.environ values are NOT overridden (real env wins).
# ══════════════════════════════════════════════════════════════════
def _load_env_file(path):
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        print(f"[✓ ENV] Loaded {path}")
        return True
    except Exception as e:
        print(f"[! ENV] Failed to load {path}: {e}")
        return False

_env_candidates = [os.environ.get('ENV_FILE'), 'alonexraj.env', '.env']
for _p in _env_candidates:
    if _load_env_file(_p):
        break

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════
# CONFIG — values from .env, fallback to empty string
# Set them in alonexraj.env (local) OR Render/Railway env vars (production)
# ══════════════════════════════════════════════════════════════════
app.secret_key = os.getenv('FLASK_SECRET_KEY', '') or secrets.token_hex(16)

# Owner credentials
OWNER_USER = os.getenv('OWNER_USER', '')
OWNER_PASS = os.getenv('OWNER_PASS', '')

# Shared secret keys — must match Android app
HMAC_SECRET = os.getenv('HMAC_SECRET', '')
AES_KEY = os.getenv('AES_KEY', '').encode('utf-8')

# Attack API — INTERNAL ONLY
ATTACK_API_BASE = os.getenv('ATTACK_API_BASE', '')
ATTACK_API_KEY = os.getenv('ATTACK_API_KEY', '')

# External Proxy (proxy.py on VPS)
PROXY_URL = os.getenv('PROXY_URL', '')
PROXY_SECRET = os.getenv('PROXY_SECRET', '')
PROXY_METHOD = os.getenv('PROXY_METHOD', 'STUN')

# MongoDB
MONGO_URI = os.getenv('MONGO_URI', '')
MONGO_DB_NAME = os.getenv('MONGO_DB_NAME', 'alonexraj_panel')

# Warn loudly if critical vars are missing — but don't crash on import
_missing = [k for k, v in {
    'OWNER_USER': OWNER_USER, 'OWNER_PASS': OWNER_PASS,
    'HMAC_SECRET': HMAC_SECRET, 'MONGO_URI': MONGO_URI,
    'PROXY_URL': PROXY_URL,
}.items() if not v]
if _missing:
    print(f"[! WARN] Missing env vars: {', '.join(_missing)}. Configure them in env to enable full functionality.")

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
db = mongo_client[MONGO_DB_NAME] if mongo_client else None

# Collections (None if DB not configured — routes will fail gracefully)
keys_col = db['keys'] if db is not None else None
connections_col = db['connections'] if db is not None else None
resellers_col = db['resellers'] if db is not None else None
history_col = db['key_history'] if db is not None else None
config_col = db['config'] if db is not None else None

# Credit rate: 10 credits = 1 hour
CREDITS_PER_HOUR = int(os.environ.get('CREDITS_PER_HOUR', '10'))


# ══════════════════════════════════════════════════════════════════
# DATA HELPERS — MongoDB
# ══════════════════════════════════════════════════════════════════
# Keep alive thread for Render free tier
import threading
import time
import requests

def keep_alive_ping():
    """Background thread to ping the app every 4 minutes"""
    while True:
        time.sleep(240)  # 4 minutes (Render idle timeout is 15 minutes)
        try:
            # Get the port from environment or use default
            port = int(os.environ.get('PORT', 3000))
            url = f"http://localhost:{port}/health"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print(f"[✓ Keep-Alive] Ping successful at {datetime.utcnow().isoformat()}")
            else:
                print(f"[✗ Keep-Alive] Ping failed with status: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[! Keep-Alive] Error: {e}")
        except Exception as e:
            print(f"[! Keep-Alive] Unexpected error: {e}")

# Health check endpoint for uptime monitoring
@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint for keep-alive services"""
    return jsonify({
        'status': 'alive',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'service': 'ALONExRAJ Panel',
        'version': '2.0'
    })

# Start keep-alive thread (only if not disabled via environment variable)
def start_keep_alive():
    """Initialize the keep-alive background thread"""
    if os.environ.get('DISABLE_KEEP_ALIVE', '').lower() != 'true':
        keep_alive_thread = threading.Thread(target=keep_alive_ping, daemon=True)
        keep_alive_thread.start()
        print("[✓ Keep-Alive] Background thread started successfully")
        print("[✓ Keep-Alive] Will ping every 4 minutes to prevent spin-down")
    else:
        print("[! Keep-Alive] Disabled via DISABLE_KEEP_ALIVE environment variable")
        
def load_keys():
    """Load active keys, auto-remove expired ones."""
    now = datetime.utcnow().isoformat() + 'Z'
    # Remove expired keys that have expires_at set and are past expiry
    all_keys = list(keys_col.find({}, {'_id': 0}))
    active_keys = []
    for k in all_keys:
        if not k.get('expires_at'):
            active_keys.append(k)  # Unredeemed — keep
            continue
        if k['expires_at'] > now:
            active_keys.append(k)  # Not expired — keep
        else:
            keys_col.delete_one({'id': k['id']})  # Expired — remove
    return active_keys

def save_key(record):
    """Insert or update a single key."""
    keys_col.update_one({'id': record['id']}, {'$set': record}, upsert=True)

def delete_key_by_id(key_id):
    keys_col.delete_one({'id': key_id})

def find_key_by_value(key_value):
    return keys_col.find_one({'key': key_value}, {'_id': 0})

def update_key(key_id, updates):
    keys_col.update_one({'id': key_id}, {'$set': updates})

def load_connections():
    doc = connections_col.find_one({'_type': 'connections'}, {'_id': 0})
    return doc.get('data', {}) if doc else {}

def save_connections(connections):
    connections_col.update_one({'_type': 'connections'}, {'$set': {'_type': 'connections', 'data': connections}}, upsert=True)

def load_resellers():
    return list(resellers_col.find({}, {'_id': 0}))

def find_reseller(username):
    return resellers_col.find_one({'username': username}, {'_id': 0})

def update_reseller(username, updates):
    resellers_col.update_one({'username': username}, {'$set': updates})

def delete_reseller_by_username(username):
    resellers_col.delete_one({'username': username})

def add_reseller(reseller):
    resellers_col.insert_one(reseller)

def load_history():
    return list(history_col.find({}, {'_id': 0}).sort('created_at', -1))

def save_history_record(record):
    history_col.insert_one(record)

def load_update_config():
    doc = config_col.find_one({'_type': 'update_config'}, {'_id': 0})
    if doc:
        doc.pop('_type', None)
        return doc
    return {"latest_version_code": 1, "latest_version_name": "1.0", "apk_filename": "", "changelog": ""}

def save_update_config(config):
    config['_type'] = 'update_config'
    config_col.update_one({'_type': 'update_config'}, {'$set': config}, upsert=True)


# ══════════════════════════════════════════════════════════════════
# CRYPTO + PROXY
# ══════════════════════════════════════════════════════════════════

def encrypt_response(data_dict):
    plaintext = json.dumps(data_dict).encode('utf-8')
    iv = os.urandom(16)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(iv + ct).decode('utf-8')

def encrypted_reply(data_dict):
    encrypted = encrypt_response(data_dict)
    resp = make_response(encrypted, 200)
    resp.headers['Content-Type'] = 'application/octet-stream'
    return resp

def proxy_attack(ip, port, time_sec):
    """
    Forward attack request to VPS proxy — single request, STUN method.
    """
    try:
        payload = {
            "secret": PROXY_SECRET,
            "ip": ip,
            "port": port,
            "time": time_sec,
            "method": PROXY_METHOD,
        }
        r = requests.post(PROXY_URL, json=payload, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "queued":
                launched = data.get("launchedCount", 1)
                return {
                    "status": "queued",
                    "message": data.get("message", "⚡ Attack Launched!"),
                    "target": f"{ip}:{port}",
                    "method": PROXY_METHOD,
                    "slots": {"active": launched, "available": max(8 - launched, 0), "max": 8},
                }
            return {"status": "error", "message": data.get("message", "Attack failed")}
        return {"status": "error", "message": f"Proxy returned {r.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def proxy_status():
    """Simple online check"""
    return {"status": "online"}

def sign_response(expires_at, device_id):
    msg = "{}|{}".format(expires_at or "", device_id or "")
    return hmac.HMAC(HMAC_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr


# ══════════════════════════════════════════════════════════════════
# RATE LIMITER (in-memory, per-IP) — login brute-force protection
# ══════════════════════════════════════════════════════════════════
import time as _time_mod
from collections import defaultdict
_login_attempts = defaultdict(list)   # ip -> [timestamp, ...]
_login_lockouts = {}                  # ip -> unlock_timestamp

# Config (override via env)
LOGIN_MAX_ATTEMPTS = int(os.environ.get('LOGIN_MAX_ATTEMPTS', '5'))
LOGIN_WINDOW_SEC = int(os.environ.get('LOGIN_WINDOW_SEC', '300'))    # 5 min
LOGIN_LOCKOUT_SEC = int(os.environ.get('LOGIN_LOCKOUT_SEC', '900'))  # 15 min

def login_check_rate(ip):
    """Returns (allowed: bool, retry_after_sec: int, attempts_remaining: int)."""
    now = _time_mod.time()
    # Currently locked out?
    unlock_at = _login_lockouts.get(ip)
    if unlock_at and now < unlock_at:
        return False, int(unlock_at - now), 0
    if unlock_at and now >= unlock_at:
        _login_lockouts.pop(ip, None)
        _login_attempts.pop(ip, None)
    # Prune old attempts
    cutoff = now - LOGIN_WINDOW_SEC
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    remaining = LOGIN_MAX_ATTEMPTS - len(_login_attempts[ip])
    return True, 0, max(remaining, 0)

def login_record_failure(ip):
    """Record a failed login. Lock out IP if threshold exceeded."""
    now = _time_mod.time()
    _login_attempts[ip].append(now)
    if len(_login_attempts[ip]) >= LOGIN_MAX_ATTEMPTS:
        _login_lockouts[ip] = now + LOGIN_LOCKOUT_SEC
        print(f"[! RATE] Locked out IP {ip} for {LOGIN_LOCKOUT_SEC}s after {len(_login_attempts[ip])} failures")

def login_record_success(ip):
    """Clear the IP's failure counter on successful login."""
    _login_attempts.pop(ip, None)
    _login_lockouts.pop(ip, None)


# ══════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ══════════════════════════════════════════════════════════════════

def is_owner():
    return session.get('role') == 'owner'

def is_reseller():
    return session.get('role') == 'reseller'

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_owner():
            return jsonify({'error': 'Owner access required'}), 403
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════
# HTML TEMPLATES
# ══════════════════════════════════════════════════════════════════

LOGIN_TEMPLATE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ALONExRAJ Panel — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Plus Jakarta Sans',sans-serif}
:root{
  --card:#fff; --card-text:#1a1a2e; --card-muted:#9ca3af;
  --input-bg:#f9fafb; --input-border:#e5e7eb; --input-text:#1f2937;
  --input-placeholder:#d1d5db; --label:#4b5563;
}
[data-theme="dark"]{
  --card:#1a1d2e; --card-text:#f3f4f6; --card-muted:#9ca3af;
  --input-bg:#252938; --input-border:#3a3f54; --input-text:#f3f4f6;
  --input-placeholder:#6b7280; --label:#cbd5e1;
}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
background:linear-gradient(135deg,#667eea 0%,#764ba2 50%,#f093fb 100%);
background-size:300% 300%;animation:bgShift 15s ease infinite;position:relative;overflow:hidden}
@keyframes bgShift{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}
body::before,body::after{content:'';position:absolute;border-radius:50%;filter:blur(80px);opacity:.4;pointer-events:none}
body::before{width:400px;height:400px;background:#a78bfa;top:-100px;left:-100px;animation:float 8s ease-in-out infinite}
body::after{width:500px;height:500px;background:#f0abfc;bottom:-150px;right:-150px;animation:float 10s ease-in-out infinite reverse}
@keyframes float{0%,100%{transform:translate(0,0)}50%{transform:translate(30px,-30px)}}
.card{position:relative;z-index:2;display:flex;background:var(--card);backdrop-filter:blur(20px);
border-radius:28px;overflow:hidden;box-shadow:0 30px 80px rgba(80,50,180,.3),0 0 0 1px rgba(255,255,255,.1) inset;
max-width:920px;width:100%;min-height:500px}
.left{flex:1;background:linear-gradient(160deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
display:flex;flex-direction:column;align-items:center;justify-content:center;padding:50px 40px;color:#fff;position:relative;overflow:hidden}
.left::before{content:'';position:absolute;width:280px;height:280px;border-radius:50%;background:rgba(255,255,255,.08);top:-80px;right:-80px}
.left::after{content:'';position:absolute;width:200px;height:200px;border-radius:50%;background:rgba(255,255,255,.06);bottom:-60px;left:-60px}
.left .logo-circle{width:96px;height:96px;border-radius:24px;background:rgba(255,255,255,.2);backdrop-filter:blur(10px);
display:flex;align-items:center;justify-content:center;margin-bottom:24px;box-shadow:0 12px 32px rgba(0,0,0,.15);position:relative;z-index:2}
.left h1{font-size:30px;font-weight:800;margin-bottom:10px;letter-spacing:-.5px;position:relative;z-index:2}
.left .tagline{font-size:14px;font-weight:400;opacity:.85;text-align:center;line-height:1.6;max-width:280px;position:relative;z-index:2}
.left .feats{margin-top:32px;display:flex;flex-direction:column;gap:12px;position:relative;z-index:2;width:100%;max-width:260px}
.left .feat{display:flex;align-items:center;gap:10px;font-size:13px;background:rgba(255,255,255,.12);padding:10px 14px;border-radius:12px;backdrop-filter:blur(10px)}
.left .feat .dot{width:8px;height:8px;border-radius:50%;background:#4ade80;box-shadow:0 0 8px #4ade80}
.right{flex:1;padding:60px 50px;display:flex;flex-direction:column;justify-content:center}
.right h2{font-size:28px;font-weight:800;color:var(--card-text);margin-bottom:8px;letter-spacing:-.5px}
.right .sub{font-size:14px;color:var(--card-muted);margin-bottom:32px}
.ig{margin-bottom:18px}
.ig label{display:block;font-size:12px;font-weight:600;color:var(--label);margin-bottom:8px;text-transform:uppercase;letter-spacing:.6px}
.ig input{width:100%;padding:15px 18px;background:var(--input-bg);border:2px solid var(--input-border);border-radius:14px;font-size:15px;color:var(--input-text);transition:.2s;font-weight:500}
.ig input:focus{outline:none;border-color:#8b5cf6;background:var(--card);box-shadow:0 0 0 4px rgba(139,92,246,.12)}
.ig input::placeholder{color:var(--input-placeholder);font-weight:400}
.btn-submit{width:100%;padding:16px;background:linear-gradient(135deg,#6366f1,#8b5cf6,#ec4899);background-size:200% 200%;
color:#fff;border:none;border-radius:14px;font-size:15px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
cursor:pointer;transition:.25s;margin-top:8px;box-shadow:0 8px 24px rgba(139,92,246,.35)}
.btn-submit:hover{background-position:100% 50%;transform:translateY(-2px);box-shadow:0 12px 32px rgba(139,92,246,.45)}
.btn-submit:active{transform:translateY(0)}
.err{color:#dc2626;font-size:13px;margin-bottom:16px;padding:12px 14px;background:#fef2f2;border-left:3px solid #dc2626;border-radius:8px;font-weight:500}
.ft{margin-top:24px;font-size:11px;color:#9ca3af;text-align:center;font-weight:500;letter-spacing:.4px}
.ft span{color:#8b5cf6;font-weight:700}
@media(max-width:760px){.card{flex-direction:column;min-height:auto}.left{padding:40px 30px}.left .feats{display:none}.right{padding:40px 28px}}
</style>
</head>
<body>
<div class="card">
<div class="left">
<div class="logo-circle">
<svg width="48" height="48" viewBox="0 0 24 24" fill="none">
<path d="M12 2L4 7v6c0 5 3.5 9 8 10 4.5-1 8-5 8-10V7l-8-5z" stroke="#fff" stroke-width="2" fill="rgba(255,255,255,.15)"/>
<path d="M9 12l2 2 4-4" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
</div>
<h1>ALONExRAJ</h1>
<div class="tagline">Premium Key Management & Reseller Panel</div>
<div class="feats">
<div class="feat"><div class="dot"></div>Secure Key Generation</div>
<div class="feat"><div class="dot"></div>Reseller Credit System</div>
<div class="feat"><div class="dot"></div>Real-time Device Tracking</div>
</div>
</div>
<div class="right">
<h2>Welcome back 👋</h2>
<p class="sub">Sign in to your dashboard to continue</p>
{% if error %}<div class="err">⚠️ {{ error }}</div>{% endif %}
<form method="post">
<div class="ig"><label>Username</label><input name="username" placeholder="Enter your username" required autofocus></div>
<div class="ig"><label>Password</label><input name="password" type="password" placeholder="••••••••••" required></div>
<button type="submit" class="btn-submit">Sign In</button>
</form>
<div class="ft">© 2025 <span>ALONExRAJ</span> Premium Panel</div>
</div>
</div>
<script>
// Apply saved theme on login page too
(function(){
  let saved=null;
  try{saved=localStorage.getItem('theme');}catch(e){}
  const t = saved || (window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
  document.documentElement.setAttribute('data-theme', t);
})();
</script>
</body>
</html>'''

DASHBOARD_TEMPLATE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Plus Jakarta Sans',sans-serif}

/* ═══════════════════════════════════════════════════════════
   THEME VARIABLES — light (default) + dark (data-theme="dark")
   ═══════════════════════════════════════════════════════════ */
:root{
  --bg-grad:linear-gradient(180deg,#f8f9ff 0%,#eef0fc 100%);
  --surface:#ffffff;
  --surface-2:#f9fafb;
  --surface-3:#f3f4f6;
  --border:#eef0f7;
  --border-2:#e5e7eb;
  --text:#1a1a2e;
  --text-muted:#6b7280;
  --text-soft:#9ca3af;
  --primary:#6366f1;
  --primary-hover:#8b5cf6;
  --shadow-sm:0 2px 12px rgba(80,50,180,.04);
  --shadow:0 4px 16px rgba(80,50,180,.06);
  --shadow-lg:0 8px 24px rgba(0,0,0,.08);
  --modal-bg:rgba(20,15,40,.5);
  --row-hover:#fafbff;
  --table-head:#f9fafb;
  --logout-bg:#fef2f2;
  --logout-color:#dc2626;
  --logout-hover:#fee2e2;
  --link:#3b82f6;
  --countdown-active:#10b981;
  --countdown-warn:#f59e0b;
  --countdown-danger:#dc2626;
  --empty-icon-bg:#f3f4f6;
  --empty-icon-color:#d1d5db;
}
[data-theme="dark"]{
  --bg-grad:linear-gradient(180deg,#0f1117 0%,#1a1d2e 100%);
  --surface:#1a1d2e;
  --surface-2:#252938;
  --surface-3:#2d3142;
  --border:#2a2e3f;
  --border-2:#3a3f54;
  --text:#f3f4f6;
  --text-muted:#9ca3af;
  --text-soft:#6b7280;
  --primary:#818cf8;
  --primary-hover:#a78bfa;
  --shadow-sm:0 2px 12px rgba(0,0,0,.3);
  --shadow:0 4px 16px rgba(0,0,0,.4);
  --shadow-lg:0 8px 24px rgba(0,0,0,.5);
  --modal-bg:rgba(0,0,0,.7);
  --row-hover:#252938;
  --table-head:#252938;
  --logout-bg:#3f1d1d;
  --logout-color:#f87171;
  --logout-hover:#5b2424;
  --link:#60a5fa;
  --countdown-active:#34d399;
  --countdown-warn:#fbbf24;
  --countdown-danger:#f87171;
  --empty-icon-bg:#2a2e3f;
  --empty-icon-color:#4b5563;
}

body{background:var(--bg-grad);color:var(--text);min-height:100vh;transition:background .3s,color .3s}
.topbar{background:var(--surface);padding:14px 24px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;box-shadow:var(--shadow-sm)}
.topbar .brand-wrap{display:flex;align-items:center;gap:10px}
.topbar .brand-icon{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:14px;box-shadow:0 4px 12px rgba(99,102,241,.3)}
.topbar .brand{font-size:18px;font-weight:800;color:var(--text);letter-spacing:-.3px}
.topbar .user-info{display:flex;align-items:center;gap:14px;font-size:13px;color:var(--text-muted);font-weight:500}
.topbar .user-info span{color:var(--text);font-weight:600}
.topbar a.logout-link{color:var(--logout-color);text-decoration:none;font-size:13px;font-weight:600;padding:7px 14px;border-radius:8px;background:var(--logout-bg);transition:.2s}
.topbar a.logout-link:hover{background:var(--logout-hover)}
/* Theme toggle */
.theme-toggle{background:var(--surface-3);border:none;color:var(--text);width:36px;height:36px;border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:16px;transition:.2s}
.theme-toggle:hover{background:var(--border-2);transform:scale(1.05)}
.theme-toggle .sun{display:none}
.theme-toggle .moon{display:block}
[data-theme="dark"] .theme-toggle .sun{display:block}
[data-theme="dark"] .theme-toggle .moon{display:none}
.container{max-width:1100px;margin:0 auto;padding:24px 20px 60px}

/* Hero greeting card */
.hero{background:var(--surface);border-radius:20px;padding:24px 26px;margin-bottom:20px;display:flex;align-items:center;gap:18px;box-shadow:var(--shadow);position:relative;overflow:hidden;border:1px solid var(--border)}
.hero::before{content:'';position:absolute;width:200px;height:200px;border-radius:50%;background:linear-gradient(135deg,#a78bfa20,#f0abfc20);top:-80px;right:-60px}
.hero .hero-icon{width:54px;height:54px;border-radius:16px;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 8px 20px rgba(99,102,241,.35);position:relative;z-index:1}
.hero .hero-icon svg{width:26px;height:26px;color:#fff}
.hero .hero-text{position:relative;z-index:1;flex:1}
.hero h1{font-size:22px;font-weight:800;color:var(--text);margin-bottom:4px;letter-spacing:-.4px}
.hero p{font-size:13px;color:var(--text-muted);font-weight:500}
.hero .hero-actions{display:flex;gap:10px;position:relative;z-index:1;flex-wrap:wrap}
.hero-btn{padding:10px 18px;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:.2s;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.hero-btn.primary{background:linear-gradient(135deg,#3b82f6,#6366f1);color:#fff;box-shadow:0 4px 12px rgba(59,130,246,.3)}
.hero-btn.primary:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(59,130,246,.4)}
.hero-btn.outline{background:var(--surface);color:var(--text-muted);border:1.5px solid var(--border-2)}
.hero-btn.outline:hover{border-color:#8b5cf6;color:#8b5cf6}

/* Gradient stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:24px}
.stat-card{border-radius:18px;padding:20px;color:#fff;position:relative;overflow:hidden;transition:.25s;box-shadow:0 8px 24px rgba(0,0,0,.08);cursor:default}
.stat-card:hover{transform:translateY(-3px);box-shadow:0 14px 32px rgba(0,0,0,.12)}
.stat-card::before{content:'';position:absolute;width:140px;height:140px;border-radius:50%;background:rgba(255,255,255,.12);top:-50px;right:-50px}
.stat-card::after{content:'';position:absolute;width:80px;height:80px;border-radius:50%;background:rgba(255,255,255,.08);bottom:-30px;right:30px}
.stat-card .icon{width:40px;height:40px;border-radius:11px;background:rgba(255,255,255,.22);display:flex;align-items:center;justify-content:center;margin-bottom:14px;backdrop-filter:blur(10px);position:relative;z-index:1}
.stat-card .icon svg{width:20px;height:20px;color:#fff}
.stat-card .label{font-size:12px;opacity:.92;font-weight:500;margin-bottom:6px;position:relative;z-index:1;letter-spacing:.2px}
.stat-card .value{font-size:30px;font-weight:800;position:relative;z-index:1;letter-spacing:-.5px}
.sc-blue{background:linear-gradient(135deg,#3b82f6,#1d4ed8)}
.sc-green{background:linear-gradient(135deg,#10b981,#059669)}
.sc-orange{background:linear-gradient(135deg,#f59e0b,#ea580c)}
.sc-purple{background:linear-gradient(135deg,#a855f7,#7c3aed)}
.sc-pink{background:linear-gradient(135deg,#ec4899,#be185d)}
.sc-cyan{background:linear-gradient(135deg,#06b6d4,#0891b2)}

/* Section panels */
.section{background:var(--surface);border-radius:18px;padding:22px;margin-bottom:16px;box-shadow:var(--shadow);border:1px solid var(--border)}
.section-head{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.section-head .se-icon{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,#a855f7,#7c3aed);display:flex;align-items:center;justify-content:center;color:#fff;flex-shrink:0;box-shadow:0 4px 12px rgba(168,85,247,.25)}
.section-head .se-icon svg{width:20px;height:20px}
.section-head .se-text h3{font-size:16px;font-weight:700;color:var(--text);margin-bottom:2px}
.section-head .se-text p{font-size:12px;color:var(--text-soft);font-weight:500}
.section-head .se-spacer{flex:1}
.section-head .view-all{font-size:13px;font-weight:600;color:var(--link);text-decoration:none;cursor:pointer;display:flex;align-items:center;gap:4px}
.section-head .view-all:hover{color:var(--primary-hover)}

/* Buttons */
.btn{padding:10px 18px;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;transition:.2s}
.btn:hover{transform:translateY(-1px)}
.btn-blue{background:linear-gradient(135deg,#3b82f6,#6366f1);color:#fff;box-shadow:0 4px 12px rgba(59,130,246,.25)}
.btn-blue:hover{box-shadow:0 6px 18px rgba(59,130,246,.35)}
.btn-green{background:linear-gradient(135deg,#10b981,#059669);color:#fff;box-shadow:0 4px 12px rgba(16,185,129,.25)}
.btn-green:hover{box-shadow:0 6px 18px rgba(16,185,129,.35)}
.btn-purple{background:linear-gradient(135deg,#a855f7,#7c3aed);color:#fff;box-shadow:0 4px 12px rgba(168,85,247,.25)}
.btn-purple:hover{box-shadow:0 6px 18px rgba(168,85,247,.35)}
.btn-red{background:#fef2f2;color:#dc2626;border:1.5px solid #fecaca}
.btn-red:hover{background:#fee2e2}

/* Forms */
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.form-group label{display:block;font-size:11px;color:var(--text-muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.form-group input,.form-group select{width:100%;padding:11px 14px;background:var(--surface-2);border:1.5px solid var(--border-2);border-radius:10px;color:var(--text);font-size:14px;transition:.2s;font-weight:500;font-family:inherit}
.form-group input:focus,.form-group select:focus{outline:none;border-color:#8b5cf6;background:var(--surface);box-shadow:0 0 0 3px rgba(139,92,246,.15)}

/* Tables */
.table-wrap{overflow-x:auto;border-radius:12px;border:1px solid var(--border)}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:12px 14px;font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;background:var(--table-head);font-weight:700;border-bottom:1px solid var(--border)}
td{padding:12px 14px;font-size:13px;color:var(--text);border-bottom:1px solid var(--border);font-weight:500}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--row-hover)}

/* Badges */
.badge{padding:4px 10px;border-radius:20px;font-size:11px;font-weight:700;display:inline-block}
.badge-active{background:#d1fae5;color:#059669}
.badge-expired{background:#fee2e2;color:#dc2626}
.badge-unredeemed{background:#fef3c7;color:#d97706}
.mono{font-family:'JetBrains Mono',monospace;font-size:12px;color:#6366f1;font-weight:600}

/* Empty state */
.empty{padding:40px 20px;text-align:center;color:var(--text-soft)}
.empty .empty-icon{width:64px;height:64px;border-radius:18px;background:var(--empty-icon-bg);display:inline-flex;align-items:center;justify-content:center;margin-bottom:14px}
.empty .empty-icon svg{width:32px;height:32px;color:var(--empty-icon-color)}
.empty p{font-size:14px;font-weight:600;color:var(--text-muted);margin-bottom:4px}
.empty span{font-size:12px;color:var(--text-soft)}

/* Modal */
.modal-bg{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:var(--modal-bg);backdrop-filter:blur(6px);z-index:200;align-items:center;justify-content:center;padding:20px}
.modal-bg.active{display:flex}
.modal{background:var(--surface);border-radius:20px;padding:28px;width:100%;max-width:520px;max-height:90vh;overflow-y:auto;box-shadow:0 30px 60px rgba(0,0,0,.3);border:1px solid var(--border)}
.modal h3{color:var(--text);margin-bottom:18px;font-size:19px;font-weight:800;letter-spacing:-.3px}
.modal .close-btn{float:right;background:var(--surface-3);border:none;color:var(--text-muted);font-size:18px;cursor:pointer;width:32px;height:32px;border-radius:10px;display:flex;align-items:center;justify-content:center;transition:.2s}
.modal .close-btn:hover{background:var(--logout-hover);color:var(--logout-color)}

/* Credit pill in topbar */
.credit-badge{background:linear-gradient(135deg,#fbbf24,#f59e0b);color:#fff;padding:6px 14px;border-radius:20px;font-size:12px;font-weight:700;box-shadow:0 4px 10px rgba(245,158,11,.3);display:inline-flex;align-items:center;gap:6px}
.credit-badge::before{content:'⚡'}

/* Toolbar (action buttons row) */
.toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:18px}
.toolbar h2{font-size:20px;font-weight:800;color:#1a1a2e;letter-spacing:-.3px}
.toolbar .actions{display:flex;gap:10px;flex-wrap:wrap}

@media(max-width:700px){.cards{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}.hero{flex-direction:column;align-items:flex-start;text-align:left}.hero .hero-actions{width:100%}.hero-btn{flex:1;justify-content:center}}
@keyframes fadeOut{0%,70%{opacity:1}100%{opacity:0;transform:translateY(-10px)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.countdown{font-family:'JetBrains Mono',monospace;font-size:12px;font-variant-numeric:tabular-nums}
</style>
</head>
<body>
<div class="topbar">
<div class="brand-wrap">
<div class="brand-icon">A</div>
<div class="brand">ALONExRAJ</div>
</div>
<div class="user-info">
<span>{{ display_name }}</span>
{% if role == 'reseller' %}<span class="credit-badge">{{ credits }} Credits</span>{% endif %}
<button class="theme-toggle" onclick="toggleTheme()" title="Toggle theme"><span class="sun">☀️</span><span class="moon">🌙</span></button>
<a class="logout-link" href="/logout">Sign out</a>
</div>
</div>
<div class="container" id="app"></div>

<div class="modal-bg" id="modalBg">
<div class="modal" id="modalContent"></div>
</div>

<script>
const ROLE = '{{ role }}';
const USERNAME = '{{ username }}';
const DISPLAY_NAME = '{{ display_name }}';
const container = document.getElementById('app');
const modalBg = document.getElementById('modalBg');
const modalContent = document.getElementById('modalContent');

function closeModal(){modalBg.classList.remove('active')}
function showModal(html){modalContent.innerHTML=html;modalBg.classList.add('active')}
modalBg.addEventListener('click',e=>{if(e.target===modalBg)closeModal()});

async function api(url,opts){const r=await fetch(url,opts);return r.json();}

// ═══════════════════════════════════════════
// THEME (light/dark) — persisted in localStorage
// ═══════════════════════════════════════════
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  try{localStorage.setItem('theme', t);}catch(e){}
}
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme')||'light';
  applyTheme(cur==='dark'?'light':'dark');
}
// Init on load — saved choice OR system preference
(function(){
  let saved=null;
  try{saved=localStorage.getItem('theme');}catch(e){}
  if(saved){applyTheme(saved);return}
  const prefersDark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(prefersDark?'dark':'light');
})();

function greeting(){
  const h=new Date().getHours();
  if(h<12)return'Good morning';
  if(h<17)return'Good afternoon';
  return'Good evening';
}

// SVG icon helpers
const ICONS={
  spark:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>',
  key:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="14" r="4"/><path d="M11 11l8-8 3 3M16 6l3 3"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>',
  users:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="4"/><path d="M2 21c0-3.9 3.1-7 7-7s7 3.1 7 7"/><circle cx="17" cy="6" r="3"/><path d="M22 19c0-2.8-2.2-5-5-5"/></svg>',
  device:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="2" width="12" height="20" rx="2"/><path d="M11 18h2"/></svg>',
  coin:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v10M9 9h4.5a1.5 1.5 0 010 3H9M9 12h5a1.5 1.5 0 010 3H9"/></svg>',
  rate:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-5 4 4 8-8M14 8h6v6"/></svg>',
  plus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  arrow:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="width:14px;height:14px"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  empty:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 12l9 4 9-4M3 17l9 4 9-4"/></svg>',
};

// ═══════════════════════════════════════════
// OWNER DASHBOARD
// ═══════════════════════════════════════════
async function renderOwnerDashboard(){
const allKeys=await api('/api/keys');
const resellers=await api('/api/resellers');
const history=await api('/api/history');
const now=new Date();
const nowMs=Date.now();
const LIVE_WINDOW=120000; // 2 minutes
const keys=allKeys;
let activeKeys=0,expiredKeys=0,totalDevices=0,liveDevices=0;
allKeys.forEach(k=>{
  k.expires_at&&new Date(k.expires_at)>now?activeKeys++:(!k.expires_at?activeKeys++:expiredKeys++);
  totalDevices+=(k.locked_device_ids||[]).length;
  Object.values(k.devices_info||{}).forEach(info=>{
    if(info.last_seen&&(nowMs-new Date(info.last_seen).getTime())<LIVE_WINDOW)liveDevices++;
  });
});

container.innerHTML=`
<div class="hero">
<div class="hero-icon">${ICONS.spark}</div>
<div class="hero-text">
<h1>${greeting()}, ${DISPLAY_NAME}!</h1>
<p>Here's an overview of your panel today.</p>
</div>
<div class="hero-actions">
<button class="hero-btn primary" onclick="showAddReseller()">${ICONS.plus} Add Reseller</button>
<button class="hero-btn outline" onclick="showResellerList()">View Resellers</button>
</div>
</div>

<div class="cards">
<div class="stat-card sc-blue"><div class="icon">${ICONS.key}</div><div class="label">Total Keys</div><div class="value">${keys.length}</div></div>
<div class="stat-card sc-green"><div class="icon">${ICONS.check}</div><div class="label">Active Keys</div><div class="value">${activeKeys}</div></div>
<div class="stat-card sc-orange"><div class="icon">${ICONS.users}</div><div class="label">Resellers</div><div class="value">${resellers.length}</div></div>
<div class="stat-card sc-purple"><div class="icon">${ICONS.device}</div><div class="label">Total Devices</div><div class="value">${totalDevices}</div></div>
<div class="stat-card sc-pink"><div class="icon">${ICONS.spark}</div><div class="label">Live Now</div><div class="value">${liveDevices}<span style="font-size:13px;font-weight:500;opacity:.85;margin-left:5px">🟢</span></div></div>
</div>

<div class="section">
<div class="section-head">
<div class="se-icon" style="background:linear-gradient(135deg,#10b981,#059669);box-shadow:0 4px 12px rgba(16,185,129,.25)">${ICONS.plus}</div>
<div class="se-text"><h3>Generate New Key</h3><p>Owner — unlimited generation, full custom control</p></div>
</div>
<div class="form-grid">
<div class="form-group"><label>Prefix</label><input id="kName" placeholder="e.g. VIP"></div>
<div class="form-group"><label>Duration</label><input id="kDur" type="number" min="1" value="60"></div>
<div class="form-group"><label>Unit</label><select id="kUnit"><option value="minutes">Minutes</option><option value="hours">Hours</option><option value="days">Days</option></select></div>
<div class="form-group"><label>Devices</label><input id="kDev" type="number" min="1" value="1"></div>
</div>
<button class="btn btn-green" style="margin-top:14px" onclick="generateKey()">⚡ Generate Key</button>
<div id="genResult" style="margin-top:12px;font-size:13px;font-family:monospace"></div>
</div>

<div class="section">
<div class="section-head">
<div class="se-icon">${ICONS.key}</div>
<div class="se-text"><h3>My Keys</h3><p>All keys generated by you — live timers</p></div>
<div class="se-spacer"></div>
<a class="view-all" onclick="showHistory()">History ${ICONS.arrow}</a>
</div>
<div class="table-wrap"><table><thead><tr><th>Name</th><th>Key</th><th>Status</th><th>Time Left</th><th>Devices</th><th>By</th><th></th><th></th></tr></thead>
<tbody>${keys.map(k=>{const x=k.expires_at?new Date(k.expires_at)<now:false;const unredeemed=!k.redeemed;const statusBadge=x?'<span class="badge badge-expired">Expired</span>':(unredeemed?'<span class="badge badge-unredeemed">Pending</span>':'<span class="badge badge-active">Active</span>');const timeCell=k.expires_at?`<span class="countdown" data-exp="${k.expires_at}">…</span>`:'<span style="color:#9ca3af;font-size:11px">awaits redeem</span>';const liveCount=Object.values(k.devices_info||{}).filter(d=>d.last_seen&&(nowMs-new Date(d.last_seen).getTime())<LIVE_WINDOW).length;const liveDot=liveCount>0?`<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 6px #10b981;animation:pulse 1.5s infinite;margin-right:5px" title="${liveCount} live"></span>`:'';return`<tr><td><strong>${k.name}</strong></td><td class="mono" style="cursor:pointer" onclick="copyKey('${k.key}')" title="Click to copy">${k.key}</td><td>${statusBadge}</td><td>${timeCell}</td><td>${liveDot}${(k.locked_device_ids||[]).length}/${k.device_limit}${liveCount>0?`<span style="color:#10b981;font-size:10px;font-weight:600;margin-left:4px">(${liveCount} live)</span>`:''}</td><td>${k.generated_by||'owner'}</td><td><button class="btn btn-blue" style="padding:6px 12px;font-size:11px" onclick="showDevices('${k.id}',this)">📱 Devices</button></td><td><button class="btn btn-red" style="padding:6px 12px;font-size:11px" onclick="deleteKey('${k.id}')">Delete</button></td></tr>`}).join('')||`<tr><td colspan="8"><div class="empty"><div class="empty-icon">${ICONS.empty}</div><p>No keys yet</p><span>Generate your first key above</span></div></td></tr>`}</tbody></table></div>
</div>

<div class="section">
<div class="section-head">
<div class="se-icon" style="background:linear-gradient(135deg,#3b82f6,#1d4ed8);box-shadow:0 4px 12px rgba(59,130,246,.25)">${ICONS.rate}</div>
<div class="se-text"><h3>Quick Actions</h3><p>Manage app & history</p></div>
</div>
<div style="display:flex;gap:10px;flex-wrap:wrap">
<button class="btn btn-purple" onclick="showHistory()">📜 Key History</button>
<button class="btn btn-blue" onclick="showUpdateConfig()">⬆️ App Update Settings</button>
</div>
</div>`;
}

// ═══════════════════════════════════════════
// RESELLER DASHBOARD
// ═══════════════════════════════════════════
async function renderResellerDashboard(){
const data=await api('/api/my-dashboard');
const keys=data.keys||[];
const credits=data.credits||0;
const now=new Date();
const nowMs=Date.now();
const LIVE_WINDOW=120000; // 2 minutes
let active=0,liveDevices=0;
keys.forEach(k=>{
  if(new Date(k.expires_at)>now)active++;
  Object.values(k.devices_info||{}).forEach(info=>{
    if(info.last_seen&&(nowMs-new Date(info.last_seen).getTime())<LIVE_WINDOW)liveDevices++;
  });
});

container.innerHTML=`
<div class="hero">
<div class="hero-icon" style="background:linear-gradient(135deg,#f59e0b,#ea580c);box-shadow:0 8px 20px rgba(245,158,11,.35)">${ICONS.spark}</div>
<div class="hero-text">
<h1>${greeting()}, ${DISPLAY_NAME}!</h1>
<p>You have <strong>${credits}</strong> credits available — let's create some keys.</p>
</div>
<div class="hero-actions">
<button class="hero-btn primary" onclick="document.getElementById('kName').focus()">${ICONS.plus} Generate Key</button>
<button class="hero-btn outline" onclick="showHistory()">View History</button>
</div>
</div>

<div class="cards">
<div class="stat-card sc-blue"><div class="icon">${ICONS.key}</div><div class="label">My Keys</div><div class="value">${keys.length}</div></div>
<div class="stat-card sc-green"><div class="icon">${ICONS.check}</div><div class="label">Active</div><div class="value">${active}</div></div>
<div class="stat-card sc-orange"><div class="icon">${ICONS.coin}</div><div class="label">Credits</div><div class="value">${credits}</div></div>
<div class="stat-card sc-pink"><div class="icon">${ICONS.spark}</div><div class="label">Live Now</div><div class="value">${liveDevices}<span style="font-size:13px;font-weight:500;opacity:.85;margin-left:5px">🟢</span></div></div>
<div class="stat-card sc-purple"><div class="icon">${ICONS.rate}</div><div class="label">Rate</div><div class="value">10/hr</div></div>
</div>

<div class="section">
<div class="section-head">
<div class="se-icon" style="background:linear-gradient(135deg,#10b981,#059669);box-shadow:0 4px 12px rgba(16,185,129,.25)">${ICONS.plus}</div>
<div class="se-text"><h3>Generate New Key</h3><p>10 credits = 1 hour</p></div>
</div>
<div class="form-grid">
<div class="form-group"><label>Prefix</label><input id="kName" placeholder="e.g. Client"></div>
<div class="form-group"><label>Duration</label><input id="kDur" type="number" min="1" value="1"></div>
<div class="form-group"><label>Unit</label><select id="kUnit"><option value="minutes">Minutes</option><option value="hours" selected>Hours</option><option value="days">Days</option></select></div>
<div class="form-group"><label>Devices</label><input id="kDev" type="number" min="1" value="1"></div>
</div>
<button class="btn btn-green" style="margin-top:14px" onclick="resellerGenerate()">⚡ Generate (uses credits)</button>
<div id="genResult" style="margin-top:12px;font-size:13px;font-family:monospace"></div>
</div>

<div class="section">
<div class="section-head">
<div class="se-icon">${ICONS.key}</div>
<div class="se-text"><h3>My Keys</h3><p>Your keys with live timers</p></div>
<div class="se-spacer"></div>
<a class="view-all" onclick="showHistory()">History ${ICONS.arrow}</a>
</div>
<div class="table-wrap"><table><thead><tr><th>Name</th><th>Key</th><th>Status</th><th>Time Left</th><th>Devices</th><th></th><th></th></tr></thead>
<tbody>${keys.map(k=>{const x=k.expires_at?new Date(k.expires_at)<now:false;const unredeemed=!k.redeemed;const statusBadge=x?'<span class="badge badge-expired">Expired</span>':(unredeemed?'<span class="badge badge-unredeemed">Pending</span>':'<span class="badge badge-active">Active</span>');const timeCell=k.expires_at?`<span class="countdown" data-exp="${k.expires_at}">…</span>`:'<span style="color:#9ca3af;font-size:11px">awaits redeem</span>';const liveCount=Object.values(k.devices_info||{}).filter(d=>d.last_seen&&(nowMs-new Date(d.last_seen).getTime())<LIVE_WINDOW).length;const liveDot=liveCount>0?`<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 6px #10b981;animation:pulse 1.5s infinite;margin-right:5px" title="${liveCount} live"></span>`:'';return`<tr><td><strong>${k.name}</strong></td><td class="mono" style="cursor:pointer" onclick="copyKey('${k.key}')" title="Click to copy">${k.key}</td><td>${statusBadge}</td><td>${timeCell}</td><td>${liveDot}${(k.locked_device_ids||[]).length}/${k.device_limit}${liveCount>0?`<span style="color:#10b981;font-size:10px;font-weight:600;margin-left:4px">(${liveCount} live)</span>`:''}</td><td><button class="btn btn-blue" style="padding:6px 12px;font-size:11px" onclick="showDevices('${k.id}',this)">📱 Devices</button></td><td><button class="btn btn-red" style="padding:6px 12px;font-size:11px" onclick="deleteKey('${k.id}')">Delete</button></td></tr>`}).join('')||`<tr><td colspan="7"><div class="empty"><div class="empty-icon">${ICONS.empty}</div><p>No keys yet</p><span>Create your first key above</span></div></td></tr>`}</tbody></table></div>
</div>`;
}

// ═══════════════════════════════════════════
// ACTIONS
// ═══════════════════════════════════════════
async function generateKey(){
const r=await api('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('kName').value,duration_value:document.getElementById('kDur').value,duration_unit:document.getElementById('kUnit').value,device_limit:document.getElementById('kDev').value})});
document.getElementById('genResult').innerHTML=r.error?`<span style="color:#dc2626">⚠️ ${r.error}</span>`:`<div style="padding:12px 14px;background:#f0fdf4;border-left:3px solid #10b981;border-radius:8px;color:#065f46">✅ Generated: <strong style="color:#10b981;cursor:pointer" onclick="copyKey('${r.key}')">${r.key}</strong></div>`;
render();
}
async function resellerGenerate(){
const dur=parseInt(document.getElementById('kDur').value)||1;
const unit=document.getElementById('kUnit').value;
const r=await api('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('kName').value,duration_value:dur,duration_unit:unit,device_limit:document.getElementById('kDev').value})});
document.getElementById('genResult').innerHTML=r.error?`<span style="color:#dc2626">⚠️ ${r.error}</span>`:`<div style="padding:12px 14px;background:#f0fdf4;border-left:3px solid #10b981;border-radius:8px;color:#065f46">✅ Generated: <strong style="color:#10b981;cursor:pointer" onclick="copyKey('${r.key}')">${r.key}</strong></div>`;
render();
}
async function deleteKey(id){if(!confirm('Delete this key permanently?'))return;const r=await api('/api/delete-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});if(r.error){alert(r.error);return}render();}

async function removeDevice(keyId,deviceId){
  if(!confirm('Remove this device? User will be logged out and can re-login from another device.'))return;
  const r=await api('/api/remove-device',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:keyId,device_id:deviceId})});
  if(r.error){alert(r.error);return}
  // refresh the device dropdown for this key
  const row=document.getElementById('dev_'+keyId);
  if(row)row.remove();
  render();
}

function showAddReseller(){
showModal(`<button class="close-btn" onclick="closeModal()">&times;</button>
<h3>👤 Add New Reseller</h3>
<div class="form-group" style="margin-bottom:12px"><label>Username</label><input id="rUser"></div>
<div class="form-group" style="margin-bottom:12px"><label>Password</label><input id="rPass" type="password"></div>
<div class="form-group" style="margin-bottom:12px"><label>Display Name</label><input id="rName"></div>
<div class="form-group" style="margin-bottom:14px"><label>Initial Credits</label><input id="rCredits" type="number" value="100"></div>
<button class="btn btn-green" style="width:100%" onclick="addReseller()">Create Reseller</button>
<div id="rResult" style="margin-top:12px;font-size:13px"></div>`);
}
async function addReseller(){
const r=await api('/api/add-reseller',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('rUser').value,password:document.getElementById('rPass').value,display_name:document.getElementById('rName').value,credits:parseInt(document.getElementById('rCredits').value)||0})});
document.getElementById('rResult').innerHTML=r.error?`<span style="color:#dc2626">⚠️ ${r.error}</span>`:`<span style="color:#10b981;font-weight:600">✅ Reseller added successfully!</span>`;
render();
}

async function showResellerList(){
const resellers=await api('/api/resellers');
let html=`<button class="close-btn" onclick="closeModal()">&times;</button><h3>👥 Resellers</h3><div class="table-wrap"><table><thead><tr><th>Name</th><th>Credits</th><th>Add</th><th></th></tr></thead><tbody>`;
resellers.forEach(r=>{html+=`<tr><td><a href="#" onclick="viewResellerDash('${r.username}');closeModal()" style="color:#6366f1;text-decoration:none;font-weight:600">${r.display_name}</a></td><td><span class="credit-badge">${r.credits}</span></td><td><input id="cr_${r.username}" type="number" value="100" style="width:75px;padding:6px 8px;background:#f9fafb;border:1.5px solid #e5e7eb;border-radius:8px;color:#1a1a2e;font-size:12px;font-family:inherit"><button class="btn btn-blue" style="padding:5px 10px;margin-left:6px;font-size:11px" onclick="addCredits('${r.username}')">+</button></td><td><button class="btn btn-red" style="padding:5px 10px;font-size:11px" onclick="deleteReseller('${r.username}')">Del</button></td></tr>`;});
html+=`</tbody></table></div>`;
showModal(html);
}
async function addCredits(username){
const credits=parseInt(document.getElementById('cr_'+username).value)||0;
await api('/api/add-credits',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,credits})});
showResellerList();
}
async function deleteReseller(username){if(!confirm('Delete reseller '+username+'?'))return;await api('/api/delete-reseller',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username})});closeModal();render();}
async function viewResellerDash(username){
const data=await api('/api/reseller-dashboard?username='+username);
const keys=data.keys||[];const now=new Date();
const history=await api('/api/history?by='+username);
container.innerHTML=`
<div class="hero">
<div class="hero-icon" style="background:linear-gradient(135deg,#a855f7,#7c3aed);box-shadow:0 8px 20px rgba(168,85,247,.35)">${ICONS.users}</div>
<div class="hero-text"><h1>${data.display_name}</h1><p>Reseller dashboard overview</p></div>
<div class="hero-actions"><button class="hero-btn outline" onclick="render()">← Back</button></div>
</div>
<div class="cards">
<div class="stat-card sc-blue"><div class="icon">${ICONS.key}</div><div class="label">Active Keys</div><div class="value">${keys.length}</div></div>
<div class="stat-card sc-orange"><div class="icon">${ICONS.coin}</div><div class="label">Credits</div><div class="value">${data.credits}</div></div>
<div class="stat-card sc-purple"><div class="icon">${ICONS.rate}</div><div class="label">All Time Keys</div><div class="value">${history.length}</div></div>
</div>
<div class="section"><div class="section-head"><div class="se-icon">${ICONS.key}</div><div class="se-text"><h3>Active Keys</h3><p>Currently usable</p></div></div><div class="table-wrap"><table><thead><tr><th>Name</th><th>Key</th><th>Expires</th><th>Devices</th></tr></thead><tbody>${keys.map(k=>`<tr><td><strong>${k.name}</strong></td><td class="mono">${k.key}</td><td>${k.expires_at?new Date(k.expires_at).toLocaleString():'Not Redeemed'}</td><td>${(k.locked_device_ids||[]).length}/${k.device_limit}</td></tr>`).join('')||`<tr><td colspan="4"><div class="empty"><div class="empty-icon">${ICONS.empty}</div><p>No active keys</p></div></td></tr>`}</tbody></table></div></div>
<div class="section"><div class="section-head"><div class="se-icon" style="background:linear-gradient(135deg,#3b82f6,#1d4ed8);box-shadow:0 4px 12px rgba(59,130,246,.25)">${ICONS.rate}</div><div class="se-text"><h3>Key History</h3><p>All-time generated keys</p></div></div><div class="table-wrap" style="max-height:340px;overflow-y:auto"><table><thead><tr><th>Key</th><th>Created</th><th>Duration</th></tr></thead><tbody>${history.map(h=>`<tr><td class="mono">${h.key}</td><td>${new Date(h.created_at).toLocaleString()}</td><td>${h.duration_value} ${h.duration_unit}</td></tr>`).join('')||`<tr><td colspan="3"><div class="empty"><div class="empty-icon">${ICONS.empty}</div><p>No history</p></div></td></tr>`}</tbody></table></div></div>`;
}

async function showHistory(){
const history=await api('/api/history');
let html=`<button class="close-btn" onclick="closeModal()">&times;</button><h3>📜 Key History</h3><div class="table-wrap" style="max-height:420px;overflow-y:auto"><table><thead><tr><th>Key</th><th>By</th><th>Created</th><th>Duration</th></tr></thead><tbody>`;
history.forEach(h=>{html+=`<tr><td class="mono">${h.key}</td><td>${h.generated_by||'owner'}</td><td>${new Date(h.created_at).toLocaleString()}</td><td>${h.duration_value} ${h.duration_unit}</td></tr>`});
if(history.length===0)html+=`<tr><td colspan="4"><div class="empty"><div class="empty-icon">${ICONS.empty}</div><p>No history yet</p></div></td></tr>`;
html+=`</tbody></table></div>`;
showModal(html);
}

async function showDevices(keyId,btn){
const row=document.getElementById('dev_'+keyId);
if(row){row.remove();return}// toggle off if already open
const keys=await api('/api/keys');
const key=keys.find(k=>k.id===keyId);
if(!key)return;
const devInfo=key.devices_info||{};
const devices=Object.entries(devInfo);
const colspan=ROLE==='owner'?8:7;
const now=Date.now();
const LIVE_WINDOW=120000; // 2 minutes
function statusFor(info){
  if(!info.last_seen)return '<span style="display:inline-flex;align-items:center;gap:5px;color:#9ca3af;font-size:11px;font-weight:600">⚪ Never</span>';
  const ago=now-new Date(info.last_seen).getTime();
  if(ago<LIVE_WINDOW){
    return '<span style="display:inline-flex;align-items:center;gap:5px;color:#10b981;font-size:11px;font-weight:700"><span style="width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 8px #10b981;animation:pulse 1.5s infinite"></span>LIVE</span>';
  }
  return '<span style="display:inline-flex;align-items:center;gap:5px;color:#9ca3af;font-size:11px;font-weight:600"><span style="width:8px;height:8px;border-radius:50%;background:#9ca3af"></span>Offline</span>';
}
function lastSeenStr(info){
  if(!info.last_seen)return '—';
  const ago=Math.floor((now-new Date(info.last_seen).getTime())/1000);
  if(ago<60)return ago+'s ago';
  if(ago<3600)return Math.floor(ago/60)+'m ago';
  if(ago<86400)return Math.floor(ago/3600)+'h ago';
  return Math.floor(ago/86400)+'d ago';
}
let html='';
if(devices.length===0){html=`<td colspan="${colspan}" style="padding:18px;background:#fafbff;color:#9ca3af;font-size:13px;text-align:center">📱 No devices connected yet.</td>`}
else{html=`<td colspan="${colspan}" style="padding:0;background:#fafbff"><table style="width:100%;margin:0"><thead><tr style="background:#f3f4f6"><th style="font-size:10px;padding:8px">#</th><th style="font-size:10px;padding:8px">Status</th><th style="font-size:10px;padding:8px">Model</th><th style="font-size:10px;padding:8px">Android</th><th style="font-size:10px;padding:8px">First Seen</th><th style="font-size:10px;padding:8px">Last Seen</th><th style="font-size:10px;padding:8px">Device ID</th><th style="font-size:10px;padding:8px">Action</th></tr></thead><tbody>${devices.map(([id,info],i)=>`<tr><td style="font-size:12px;padding:8px;color:#6366f1;font-weight:700">${i+1}</td><td style="padding:8px">${statusFor(info)}</td><td style="font-size:12px;padding:8px"><strong>${info.model||'Unknown'}</strong></td><td style="font-size:12px;padding:8px">${info.android_version||'—'}</td><td style="font-size:11px;padding:8px;color:#6b7280">${info.first_seen?new Date(info.first_seen).toLocaleString():'—'}</td><td style="font-size:11px;padding:8px;color:#6b7280" title="${info.last_seen||''}">${lastSeenStr(info)}</td><td class="mono" style="font-size:10px;padding:8px">${id.substring(0,18)}…</td><td style="padding:8px"><button class="btn btn-red" style="padding:5px 10px;font-size:10px" onclick="removeDevice('${keyId}','${id}')">🚫 Remove</button></td></tr>`).join('')}</tbody></table></td>`}
const tr=document.createElement('tr');
tr.id='dev_'+keyId;
tr.innerHTML=html;
const parentRow=btn.closest('tr');
parentRow.parentNode.insertBefore(tr,parentRow.nextSibling);
}

function copyKey(key){navigator.clipboard.writeText(key).then(()=>{const t=document.createElement('div');t.textContent='✅ Key Copied!';t.style.cssText='position:fixed;top:20px;right:20px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;padding:12px 22px;border-radius:12px;font-size:13px;font-weight:600;z-index:9999;box-shadow:0 8px 24px rgba(16,185,129,.4);animation:fadeOut 2s forwards';document.body.appendChild(t);setTimeout(()=>t.remove(),2000)}).catch(()=>prompt('Copy this key:',key))}

async function showUpdateConfig(){
const config=await api('/api/update-config');
showModal(`<button class="close-btn" onclick="closeModal()">&times;</button>
<h3>⬆️ App Update Settings</h3>
<p style="font-size:13px;color:#6b7280;margin-bottom:18px">Upload new APK here. Users will see update popup in app.</p>
<form id="updateForm" enctype="multipart/form-data">
<div class="form-group" style="margin-bottom:14px"><label>Version Code (next: ${config.latest_version_code} → ${config.latest_version_code+1})</label><input id="uVerCode" type="number" value="${config.latest_version_code+1}"></div>
<div class="form-group" style="margin-bottom:14px"><label>Version Name</label><input id="uVerName" value="${config.latest_version_name}"></div>
<div class="form-group" style="margin-bottom:14px"><label>Changelog</label><input id="uChangelog" value="${config.changelog||''}" placeholder="e.g. Bug fixes, new UI"></div>
<div class="form-group" style="margin-bottom:14px"><label>APK File</label><input id="uApkFile" type="file" accept=".apk" style="padding:10px"></div>
${config.has_apk?'<p style="font-size:12px;color:#10b981;margin-bottom:14px;background:#f0fdf4;padding:8px 12px;border-radius:8px;font-weight:600">✅ Current APK: '+config.apk_filename+'</p>':''}
<button type="button" class="btn btn-green" style="width:100%" onclick="uploadUpdate()">⬆️ Upload & Publish Update</button>
</form>
<div id="uResult" style="margin-top:12px;font-size:13px"></div>`);
}
async function uploadUpdate(){
const form=new FormData();
const file=document.getElementById('uApkFile').files[0];
if(!file){document.getElementById('uResult').innerHTML='<span style="color:#dc2626">⚠️ Select APK file</span>';return}
form.append('apk_file',file);
form.append('version_code',document.getElementById('uVerCode').value);
form.append('version_name',document.getElementById('uVerName').value);
form.append('changelog',document.getElementById('uChangelog').value);
document.getElementById('uResult').innerHTML='<span style="color:#3b82f6">⏳ Uploading...</span>';
const r=await fetch('/api/upload-apk',{method:'POST',body:form});
const data=await r.json();
document.getElementById('uResult').innerHTML=data.error?'<span style="color:#dc2626">⚠️ '+data.error+'</span>':'<div style="padding:10px 14px;background:#f0fdf4;border-left:3px solid #10b981;border-radius:8px;color:#065f46;font-weight:600">✅ Update published! Users will see update popup now.</div>';
}

// ═══════════════════════════════════════════
// LIVE COUNTDOWN — updates every second
// ═══════════════════════════════════════════
function fmtCountdown(ms){
  if(ms<=0)return '<span style="color:#dc2626;font-weight:700">EXPIRED</span>';
  const s=Math.floor(ms/1000);
  const d=Math.floor(s/86400);
  const h=Math.floor((s%86400)/3600);
  const m=Math.floor((s%3600)/60);
  const sec=s%60;
  if(d>0)return `<span style="color:#10b981;font-weight:700">${d}d ${h}h ${m}m</span>`;
  if(h>0)return `<span style="color:#10b981;font-weight:700">${h}h ${m}m ${sec}s</span>`;
  if(m>0)return `<span style="color:#f59e0b;font-weight:700">${m}m ${sec}s</span>`;
  return `<span style="color:#dc2626;font-weight:700;animation:pulse 1s infinite">${sec}s</span>`;
}
setInterval(()=>{
  const now=Date.now();
  document.querySelectorAll('.countdown').forEach(el=>{
    const exp=el.dataset.exp;if(!exp)return;
    const ts=new Date(exp).getTime();
    el.innerHTML=fmtCountdown(ts-now);
  });
},1000);

function render(){if(ROLE==='owner')renderOwnerDashboard();else renderResellerDashboard();}
render();
// Auto-refresh dashboard every 30s to keep live device status fresh
setInterval(()=>{
  // only refresh if no modal is open and no device dropdown is expanded
  if(!modalBg.classList.contains('active')&&!document.querySelector('[id^="dev_"]')){
    render();
  }
},30000);
</script>
</body>
</html>'''


# ══════════════════════════════════════════════════════════════════
# WEB ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route('/login', methods=['GET', 'POST'])
def login():
    ip = get_client_ip() or 'unknown'
    allowed, retry_after, remaining = login_check_rate(ip)

    if request.method == 'POST':
        if not allowed:
            mins = retry_after // 60 + (1 if retry_after % 60 else 0)
            return render_template_string(
                LOGIN_TEMPLATE,
                error=f'Too many failed attempts. Try again in {mins} min.'
            ), 429

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        # Check owner
        if username == OWNER_USER and password == OWNER_PASS:
            login_record_success(ip)
            session['logged_in'] = True
            session['role'] = 'owner'
            session['username'] = username
            session['display_name'] = 'Owner'
            return redirect(url_for('dashboard'))
        # Check resellers
        reseller = find_reseller(username)
        if reseller and reseller.get('password') == password:
            login_record_success(ip)
            session['logged_in'] = True
            session['role'] = 'reseller'
            session['username'] = username
            session['display_name'] = reseller['display_name']
            return redirect(url_for('dashboard'))

        # Failure — record and respond
        login_record_failure(ip)
        _, _, remaining = login_check_rate(ip)
        if remaining <= 0:
            return render_template_string(
                LOGIN_TEMPLATE,
                error=f'Too many failed attempts. IP locked for {LOGIN_LOCKOUT_SEC // 60} min.'
            ), 429
        return render_template_string(
            LOGIN_TEMPLATE,
            error=f'Invalid credentials. {remaining} attempt(s) left.'
        )

    # GET — show lockout banner if currently locked
    if not allowed:
        mins = retry_after // 60 + (1 if retry_after % 60 else 0)
        return render_template_string(
            LOGIN_TEMPLATE,
            error=f'IP temporarily locked. Try again in {mins} min.'
        ), 429
    return render_template_string(LOGIN_TEMPLATE, error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    credits = 0
    if is_reseller():
        r = find_reseller(session['username'])
        credits = r['credits'] if r else 0
    return render_template_string(DASHBOARD_TEMPLATE,
        title='ALONExRAJ Panel',
        role=session['role'],
        username=session['username'],
        display_name=session['display_name'],
        credits=credits)


# ══════════════════════════════════════════════════════════════════
# API ROUTES — Dashboard data
# ══════════════════════════════════════════════════════════════════

@app.route('/api/keys')
@login_required
def api_keys():
    keys = load_keys()
    if is_reseller():
        keys = [k for k in keys if k.get('generated_by') == session['username']]
    elif is_owner():
        # Owner sees only their own keys (not reseller-generated)
        keys = [k for k in keys if k.get('generated_by') in (session['username'], 'owner')]
    return jsonify(keys)

@app.route('/api/resellers')
@login_required
@owner_required
def api_resellers():
    return jsonify(load_resellers())

@app.route('/api/history')
@login_required
def api_history():
    history = load_history()
    if is_reseller():
        history = [h for h in history if h.get('generated_by') == session['username']]
    else:
        # Owner can filter by reseller username via query param
        filter_by = request.args.get('by', '')
        if filter_by:
            history = [h for h in history if h.get('generated_by') == filter_by]
    return jsonify(history)

@app.route('/api/my-dashboard')
@login_required
def api_my_dashboard():
    keys = load_keys()
    keys = [k for k in keys if k.get('generated_by') == session['username']]
    r = find_reseller(session['username'])
    return jsonify({'keys': keys, 'credits': r['credits'] if r else 0})

@app.route('/api/reseller-dashboard')
@login_required
@owner_required
def api_reseller_dashboard():
    username = request.args.get('username', '')
    r = find_reseller(username)
    if not r:
        return jsonify({'error': 'Not found'}), 404
    keys = [k for k in load_keys() if k.get('generated_by') == username]
    return jsonify({'keys': keys, 'credits': r['credits'], 'display_name': r['display_name']})

@app.route('/api/add-reseller', methods=['POST'])
@login_required
@owner_required
def api_add_reseller():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    display_name = data.get('display_name', '').strip()
    credits = int(data.get('credits', 0))
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if find_reseller(username):
        return jsonify({'error': 'Username already exists'}), 400
    add_reseller({'username': username, 'password': password, 'display_name': display_name or username, 'credits': credits, 'created_at': datetime.utcnow().isoformat()+'Z'})
    return jsonify({'status': 'success'})

@app.route('/api/add-credits', methods=['POST'])
@login_required
@owner_required
def api_add_credits():
    data = request.json or {}
    username = data.get('username', '')
    credits = int(data.get('credits', 0))
    r = find_reseller(username)
    if not r:
        return jsonify({'error': 'Reseller not found'}), 404
    new_credits = r.get('credits', 0) + credits
    update_reseller(username, {'credits': new_credits})
    return jsonify({'status': 'success', 'new_credits': new_credits})

@app.route('/api/delete-reseller', methods=['POST'])
@login_required
@owner_required
def api_delete_reseller():
    data = request.json or {}
    username = data.get('username', '')
    delete_reseller_by_username(username)
    return jsonify({'status': 'success'})

@app.route('/api/generate', methods=['POST'])
@login_required
def api_generate():
    data = request.json or {}
    key_name = data.get('name', '')[:64].strip() or 'key'
    duration_value = int(data.get('duration_value', 1))
    duration_unit = data.get('duration_unit', 'hours')
    device_limit = int(data.get('device_limit', 1))

    if duration_value <= 0 or device_limit <= 0:
        return jsonify({'error': 'Invalid values'}), 400

    # Credit check for resellers
    if is_reseller():
        # Convert to hours for credit calc
        if duration_unit == 'minutes':
            hours = max(1, duration_value // 60) if duration_value >= 60 else 1
        elif duration_unit == 'days':
            hours = duration_value * 24
        else:
            hours = duration_value
        cost = hours * CREDITS_PER_HOUR * device_limit  # charge per device
        r = find_reseller(session['username'])
        if not r:
            return jsonify({'error': 'Reseller not found'}), 400
        if r['credits'] < cost:
            return jsonify({'error': f'Not enough credits. Need {cost}, have {r["credits"]}'}), 400
        update_reseller(session['username'], {'credits': r['credits'] - cost})

    prefix = key_name.replace(' ', '_')
    new_key = f"{prefix}-{secrets.token_urlsafe(10)}"
    created_at = datetime.utcnow()

    # Single device key: expiry starts on redeem
    # Multi device key: expiry starts immediately (like before)
    if device_limit == 1:
        expires_at_val = None
        redeemed = False
    else:
        if duration_unit == 'minutes':
            expires_at_val = (created_at + timedelta(minutes=duration_value)).isoformat() + 'Z'
        elif duration_unit == 'hours':
            expires_at_val = (created_at + timedelta(hours=duration_value)).isoformat() + 'Z'
        else:
            expires_at_val = (created_at + timedelta(days=duration_value)).isoformat() + 'Z'
        redeemed = True

    record = {
        'id': secrets.token_hex(8),
        'name': key_name,
        'key': new_key,
        'created_at': created_at.isoformat() + 'Z',
        'expires_at': expires_at_val,
        'duration_value': duration_value,
        'duration_unit': duration_unit,
        'device_limit': device_limit,
        'plan': 'Premium',
        'locked_device_ids': [],
        'devices_info': {},
        'generated_by': session.get('username', 'owner'),
        'redeemed': redeemed
    }

    save_key(record.copy())

    # Save to history
    save_history_record(record.copy())

    return jsonify(record)

@app.route('/api/delete-key', methods=['POST'])
@login_required
def api_delete_key():
    data = request.json or {}
    key_id = data.get('id', '')
    # Permission check — resellers can delete only their own keys
    if is_reseller():
        key = keys_col.find_one({'id': key_id}, {'_id': 0})
        if not key or key.get('generated_by') != session.get('username'):
            return jsonify({'error': 'Permission denied'}), 403
    delete_key_by_id(key_id)
    connections = load_connections()
    connections.pop(key_id, None)
    save_connections(connections)
    return jsonify({'status': 'success'})


@app.route('/api/remove-device', methods=['POST'])
@login_required
def api_remove_device():
    """Remove (unlock) a device from a key so the user can re-login from a different device."""
    data = request.json or {}
    key_id = data.get('id', '')
    device_id = data.get('device_id', '')
    if not key_id or not device_id:
        return jsonify({'error': 'Missing id or device_id'}), 400

    key = keys_col.find_one({'id': key_id}, {'_id': 0})
    if not key:
        return jsonify({'error': 'Key not found'}), 404

    # Permission — owners can do anything; resellers only on their own keys
    if is_reseller() and key.get('generated_by') != session.get('username'):
        return jsonify({'error': 'Permission denied'}), 403

    locked = key.get('locked_device_ids') or []
    devices_info = key.get('devices_info') or {}
    if device_id in locked:
        locked.remove(device_id)
    devices_info.pop(device_id, None)
    update_key(key_id, {'locked_device_ids': locked, 'devices_info': devices_info})

    # Also clean from connections
    connections = load_connections()
    if key_id in connections:
        connections[key_id] = [c for c in connections[key_id] if c.get('device_id') != device_id]
        save_connections(connections)

    return jsonify({'status': 'success', 'remaining_devices': len(locked)})


# ══════════════════════════════════════════════════════════════════
# /connect — APP ENDPOINT (unchanged)
# ══════════════════════════════════════════════════════════════════

@app.route('/connect', methods=['POST'])
def connect_device():
    data = request.json or {}
    key = data.get('key', '').strip()
    device_id = data.get('device_id', '').strip()
    device_name = data.get('device_name', 'Unknown')[:100].strip()
    device_model = data.get('device_model', '')[:100].strip()
    android_version = data.get('android_version', '')[:50].strip()
    action = data.get('action', '').strip()

    if not key:
        return encrypted_reply({'valid': False, 'message': 'Key is required'})
    if not device_id:
        return encrypted_reply({'valid': False, 'message': 'Device ID is required'})

    found_key = find_key_by_value(key)
    if not found_key:
        return encrypted_reply({'valid': False, 'message': 'Invalid key'})

    # --- Expiry on first redeem (only for single device keys) ---
    if found_key.get('device_limit', 1) == 1 and (not found_key.get('redeemed') or not found_key.get('expires_at')):
        # Single device key — start expiry timer NOW on first redeem
        now = datetime.utcnow()
        duration_value = found_key.get('duration_value', 1)
        duration_unit = found_key.get('duration_unit', 'hours')
        if duration_unit == 'minutes':
            expires_at = now + timedelta(minutes=duration_value)
        elif duration_unit == 'hours':
            expires_at = now + timedelta(hours=duration_value)
        else:
            expires_at = now + timedelta(days=duration_value)
        found_key['expires_at'] = expires_at.isoformat() + 'Z'
        found_key['redeemed'] = True
        found_key['redeemed_at'] = now.isoformat() + 'Z'
        update_key(found_key['id'], {'expires_at': found_key['expires_at'], 'redeemed': True, 'redeemed_at': found_key['redeemed_at']})

    expires_at = datetime.fromisoformat(found_key['expires_at'].replace('Z', '+00:00'))
    if datetime.utcnow().replace(tzinfo=None) > expires_at.replace(tzinfo=None):
        return encrypted_reply({'valid': False, 'message': 'Key has expired'})

    device_limit = found_key.get('device_limit', 1)
    locked_devices = found_key.get('locked_device_ids') or []
    plan = found_key.get('plan', 'Premium')

    # --- Store device info (model, android version, last_seen) ---
    devices_info = found_key.get('devices_info', {})
    now_iso = datetime.utcnow().isoformat() + 'Z'
    if device_id not in devices_info:
        devices_info[device_id] = {
            'model': device_model or device_name,
            'android_version': android_version,
            'first_seen': now_iso,
            'last_seen': now_iso,
            'ip_address': get_client_ip(),
        }
    else:
        # Update model/version if provided
        if device_model:
            devices_info[device_id]['model'] = device_model
        if android_version:
            devices_info[device_id]['android_version'] = android_version
        # Always update last_seen + IP
        devices_info[device_id]['last_seen'] = now_iso
        devices_info[device_id]['ip_address'] = get_client_ip()

    if device_id not in locked_devices:
        if len(locked_devices) >= device_limit:
            return encrypted_reply({'valid': False, 'message': f'Device limit reached ({len(locked_devices)}/{device_limit})'})
        locked_devices.append(device_id)
        update_key(found_key['id'], {'locked_device_ids': locked_devices, 'devices_info': devices_info})
        connections = load_connections()
        key_id = found_key.get('id')
        connections.setdefault(key_id, [])
        connections[key_id].append({'connection_id': secrets.token_urlsafe(16), 'device_id': device_id, 'device_name': device_name, 'device_model': device_model, 'android_version': android_version, 'ip_address': get_client_ip(), 'connected_at': datetime.utcnow().isoformat()+'Z', 'status': 'approved'})
        save_connections(connections)
    else:
        update_key(found_key['id'], {'devices_info': devices_info})

    if action == 'status':
        return encrypted_reply({'valid': True, 'action': 'status', 'data': proxy_status()})

    if action == 'attack':
        target_ip = data.get('ip', '').strip()
        target_port = data.get('port', '').strip()
        attack_time = data.get('time', '').strip()
        if not target_ip or not target_port or not attack_time:
            return encrypted_reply({'valid': False, 'message': 'Missing attack params'})
        return encrypted_reply({'valid': True, 'action': 'attack', 'data': proxy_attack(target_ip, target_port, attack_time)})

    # Default: key verify
    connections = load_connections()
    key_id = found_key.get('id')
    connections.setdefault(key_id, [])
    existing = next((d for d in connections[key_id] if d.get('device_id') == device_id), None)
    if not existing:
        connections[key_id].append({'connection_id': secrets.token_urlsafe(16), 'device_id': device_id, 'device_name': device_name, 'device_model': device_model, 'android_version': android_version, 'ip_address': get_client_ip(), 'connected_at': datetime.utcnow().isoformat()+'Z', 'status': 'approved'})
        save_connections(connections)

    return encrypted_reply({'valid': True, 'message': 'Access granted', 'expires_at': found_key.get('expires_at'), 'plan': plan, 'max_devices': device_limit, 'sig': sign_response(found_key.get('expires_at'), device_id)})


# ══════════════════════════════════════════════════════════════════
# APP UPDATE SYSTEM — APK upload + in-app update
# ══════════════════════════════════════════════════════════════════

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/api/check-update', methods=['POST'])
def check_update():
    """App calls this with its current versionCode. Returns update info if newer version available."""
    data = request.json or {}
    current_version = int(data.get('version_code', 0))
    config = load_update_config()
    latest_version = config.get('latest_version_code', 1)
    if current_version < latest_version and config.get('apk_filename'):
        return jsonify({
            'update_available': True,
            'latest_version_code': latest_version,
            'latest_version_name': config.get('latest_version_name', '1.0'),
            'download_url': f"/download-apk/{config['apk_filename']}",
            'changelog': config.get('changelog', '')
        })
    return jsonify({'update_available': False})

@app.route('/download-apk/<filename>')
def download_apk(filename):
    """Serve the uploaded APK file for in-app download."""
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(filepath):
        return "File not found", 404
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True, mimetype='application/vnd.android.package-archive')

@app.route('/api/update-config', methods=['GET'])
@login_required
@owner_required
def get_update_config():
    config = load_update_config()
    config['has_apk'] = bool(config.get('apk_filename') and os.path.exists(os.path.join(UPLOAD_FOLDER, config.get('apk_filename', ''))))
    return jsonify(config)

@app.route('/api/upload-apk', methods=['POST'])
@login_required
@owner_required
def upload_apk():
    """Upload new APK + set version info."""
    if 'apk_file' not in request.files:
        return jsonify({'error': 'No APK file provided'}), 400
    file = request.files['apk_file']
    if not file.filename.endswith('.apk'):
        return jsonify({'error': 'File must be .apk'}), 400

    version_code = int(request.form.get('version_code', 1))
    version_name = request.form.get('version_name', '1.0')
    changelog = request.form.get('changelog', '')

    # Save APK
    filename = f"ALONExRAJ_v{version_code}.apk"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    # Delete old APK if different
    config = load_update_config()
    old_file = config.get('apk_filename', '')
    if old_file and old_file != filename:
        old_path = os.path.join(UPLOAD_FOLDER, old_file)
        if os.path.exists(old_path):
            os.remove(old_path)

    # Update config
    config = {
        'latest_version_code': version_code,
        'latest_version_name': version_name,
        'apk_filename': filename,
        'changelog': changelog
    }
    save_update_config(config)
    return jsonify({'status': 'success', 'filename': filename})


start_keep_alive()

if __name__ == '__main__':
    # Get port from environment (Render sets this automatically)
    port = int(os.environ.get('PORT', 3000))
    
    # Disable debug in production for better performance
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    print(f"[✓] Starting GODxPAWAN Panel on port {port}")
    print(f"[✓] Debug mode: {debug_mode}")
    print(f"[✓] Health check available at: http://localhost:{port}/health")
    print(f"[✓] Keep-alive active - will ping every 4 minutes")
    
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
