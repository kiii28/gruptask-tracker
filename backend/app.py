"""
GroupTask Tracker - Backend REST API (v2: autentikasi + kolaborasi)
Tugas 9 Komputasi Awan dan Terdistribusi

Sesuai ketentuan soal (poin 2.b): TIDAK memakai database.
Seluruh data (user, proyek, anggota, tugas) disimpan di memori sebagai dict.
Data kembali kosong setiap server di-restart.

Konfigurasi lewat environment variable (semuanya opsional untuk uji lokal):
  SECRET_KEY        kunci penanda token; WAJIB sama di semua instance
  APP_URL           alamat frontend untuk link undangan, mis. http://localhost:8080
  SMTP_HOST/PORT/USER/PASSWORD/FROM   server email untuk OTP dan undangan
  GOOGLE_CLIENT_ID  OAuth Client ID untuk login Google
  SEED_DEMO=1       buat akun demo (demo@example.com / Demo12345) + proyek contoh
Tanpa SMTP_HOST aplikasi berjalan dalam "mode dev": email tidak dikirim, kode OTP
dan link undangan dikembalikan di respons API supaya fitur tetap bisa dicoba.
"""
import hashlib
import json
import os
import re
import secrets
import smtplib
import socket
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from functools import wraps

from flask import Flask, g, jsonify, request
from flask_cors import CORS
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
CORS(app)

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")
APP_URL = os.environ.get("APP_URL", "http://localhost:8080").rstrip("/")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "no-reply@grouptask.local")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
EMAIL_ENABLED = bool(SMTP_HOST)

STATUSES = ["To Do", "In Progress", "Done"]
TOKEN_TTL = 7 * 24 * 3600
INVITE_TTL = 7 * 24 * 3600
OTP_TTL = 600
ONLINE_WINDOW = 45  # detik sejak aktivitas terakhir
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[\w.\- ]{3,30}$")

auth_ser = URLSafeTimedSerializer(SECRET_KEY, salt="auth")
invite_ser = URLSafeTimedSerializer(SECRET_KEY, salt="invite")

# ---------------------------------------------------------------------------
# DATA HARDCODED / IN-MEMORY (pengganti database)
# ---------------------------------------------------------------------------
DB = {"users": {}, "projects": {}, "members": {}, "tasks": {}, "otps": {}}
_counters = {"users": 0, "projects": 0, "members": 0, "tasks": 0}
_prefix = {"users": "U", "projects": "P", "members": "M", "tasks": "T"}
LOGIN_FAILS = {}  # email -> (jumlah gagal, terkunci sampai)


def next_id(entity):
    _counters[entity] += 1
    return f"{_prefix[entity]}{_counters[entity]:02d}"


# ---------------------------------------------------------------------------
# Helper umum
# ---------------------------------------------------------------------------
def error(message, code=400, **extra):
    return jsonify({"error": message, **extra}), code


def body():
    return request.get_json(silent=True) or {}


def clean(value):
    return value.strip() if isinstance(value, str) else value


@app.errorhandler(404)
def not_found(_):
    return error("Endpoint tidak ditemukan.", 404)


@app.errorhandler(405)
def not_allowed(_):
    return error("Method tidak diizinkan.", 405)


@app.errorhandler(500)
def server_error(_):
    return error("Terjadi kesalahan di server.", 500)


def send_mail(to, subject, text):
    """Kirim email via SMTP. Return True jika terkirim, False jika tidak."""
    if not EMAIL_ENABLED:
        print(f"[MODE DEV] Email ke {to}\n  Subjek: {subject}\n  {text}\n", flush=True)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject
    msg.set_content(text)
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=12) as s:
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=12) as s:
                s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[EMAIL GAGAL] {to}: {exc}", flush=True)
        return False


# ---------------------------------------------------------------------------
# User & autentikasi
# ---------------------------------------------------------------------------
def user_by_email(email):
    return next((u for u in DB["users"].values() if u["email"] == email), None)


def username_taken(name, exclude_id=None):
    low = name.lower()
    return any(u["username"].lower() == low and u["user_id"] != exclude_id for u in DB["users"].values())


def unique_username(base):
    base = re.sub(r"[^\w.\- ]", "", base).strip()[:24] or "user"
    if len(base) < 3:
        base += "_user"
    name, i = base, 1
    while username_taken(name):
        i += 1
        name = f"{base}{i}"
    return name


def new_user(username, email, password_hash, verified, photo=""):
    uid = next_id("users")
    DB["users"][uid] = {
        "user_id": uid, "username": username, "email": email, "nim": "", "photo": photo,
        "password_hash": password_hash, "verified": verified, "last_seen": 0.0,
    }
    return DB["users"][uid]


def user_public(u):
    return {
        "user_id": u["user_id"], "username": u["username"], "email": u["email"],
        "nim": u["nim"], "photo": u["photo"], "has_password": bool(u["password_hash"]),
    }


def is_online(u):
    return time.time() - u["last_seen"] < ONLINE_WINDOW


def make_token(uid):
    return auth_ser.dumps({"uid": uid})


def auth_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return error("Silakan login terlebih dahulu.", 401)
        try:
            data = auth_ser.loads(header[7:], max_age=TOKEN_TTL)
        except (BadSignature, SignatureExpired):
            return error("Sesi tidak valid atau sudah berakhir. Silakan login lagi.", 401)
        u = DB["users"].get(data.get("uid"))
        if not u:
            return error("Sesi tidak valid. Silakan login lagi.", 401)
        u["last_seen"] = time.time()  # heartbeat untuk status online
        g.user = u
        return fn(*a, **kw)
    return wrapper


def otp_hash(email, otp):
    return hashlib.sha256(f"{SECRET_KEY}:{email}:{otp}".encode()).hexdigest()


def issue_otp(email):
    otp = f"{secrets.randbelow(10 ** 6):06d}"
    DB["otps"][email] = {"hash": otp_hash(email, otp), "expires": time.time() + OTP_TTL, "attempts": 0, "sent_at": time.time()}
    sent = send_mail(
        email, "Kode verifikasi GroupTask Tracker",
        f"Kode verifikasi Anda: {otp}\n\nKode berlaku {OTP_TTL // 60} menit. Abaikan email ini jika Anda tidak merasa mendaftar.",
    )
    return otp, sent


def otp_response(email, otp, sent):
    if EMAIL_ENABLED and not sent:
        return error("Gagal mengirim email OTP. Periksa konfigurasi SMTP lalu coba kirim ulang.", 502)
    resp = {"email": email, "email_sent": sent}
    if not EMAIL_ENABLED:
        resp["dev_otp"] = otp
    return jsonify(resp)


@app.get("/api/health")
def health():
    # "server" menunjukkan instance mana yang menjawab (berguna saat demo Load Balancer)
    return jsonify({"status": "ok", "server": socket.gethostname()})


@app.get("/api/config")
def config():
    return jsonify({"google_client_id": GOOGLE_CLIENT_ID, "email_enabled": EMAIL_ENABLED})


@app.post("/api/auth/register")
def register():
    d = body()
    username, email, password = clean(d.get("username")) or "", (clean(d.get("email")) or "").lower(), d.get("password") or ""
    if not USERNAME_RE.match(username):
        return error("Username 3-30 karakter (huruf, angka, spasi, titik, strip, underscore).")
    if not EMAIL_RE.match(email):
        return error("Format email tidak valid.")
    if len(password) < 8:
        return error("Password minimal 8 karakter.")
    existing = user_by_email(email)
    if existing and existing["verified"]:
        return error("Email sudah terdaftar. Silakan login (atau gunakan Google).", 409)
    if username_taken(username, existing["user_id"] if existing else None):
        return error("Username sudah dipakai.", 409)
    if existing:  # daftar ulang akun yang belum terverifikasi
        existing.update(username=username, password_hash=generate_password_hash(password))
    else:
        new_user(username, email, generate_password_hash(password), verified=False)
    otp, sent = issue_otp(email)
    return otp_response(email, otp, sent)


@app.post("/api/auth/resend-otp")
def resend_otp():
    email = (clean(body().get("email")) or "").lower()
    u = user_by_email(email)
    if not u or u["verified"]:
        return error("Tidak ada pendaftaran yang menunggu verifikasi untuk email ini.", 404)
    rec = DB["otps"].get(email)
    if rec and time.time() - rec["sent_at"] < 30:
        return error("Tunggu 30 detik sebelum meminta kode baru.", 429)
    otp, sent = issue_otp(email)
    return otp_response(email, otp, sent)


@app.post("/api/auth/verify-otp")
def verify_otp():
    d = body()
    email, otp = (clean(d.get("email")) or "").lower(), clean(d.get("otp")) or ""
    u, rec = user_by_email(email), DB["otps"].get(email)
    if not u or not rec:
        return error("Kode tidak ditemukan. Minta kode baru.", 404)
    if time.time() > rec["expires"]:
        return error("Kode sudah kedaluwarsa. Minta kode baru.", 410)
    if rec["attempts"] >= 5:
        return error("Terlalu banyak percobaan. Minta kode baru.", 429)
    rec["attempts"] += 1
    if not secrets.compare_digest(rec["hash"], otp_hash(email, otp)):
        return error("Kode OTP salah.", 400)
    u["verified"] = True
    del DB["otps"][email]
    return jsonify({"verified": True, "email": email})


@app.post("/api/auth/login")
def login():
    d = body()
    email, password = (clean(d.get("email")) or "").lower(), d.get("password") or ""
    fails, until = LOGIN_FAILS.get(email, (0, 0))
    if until > time.time():
        return error("Terlalu banyak percobaan gagal. Coba lagi beberapa menit lagi.", 429)
    u = user_by_email(email)
    if not u or not u["password_hash"] or not check_password_hash(u["password_hash"], password):
        fails += 1
        LOGIN_FAILS[email] = (fails, time.time() + 300 if fails >= 8 else 0)
        return error("Email atau password salah.", 401)
    LOGIN_FAILS.pop(email, None)
    if not u["verified"]:
        return error("Email belum diverifikasi.", 403, needs_verification=True, email=email)
    u["last_seen"] = time.time()
    return jsonify({"token": make_token(u["user_id"]), "user": user_public(u)})


def verify_google(credential):
    url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(credential or "")
    with urllib.request.urlopen(url, timeout=8) as resp:
        info = json.load(resp)
    if info.get("aud") != GOOGLE_CLIENT_ID or str(info.get("email_verified")).lower() != "true":
        raise ValueError("token tidak valid")
    return info


@app.post("/api/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID:
        return error("Login Google belum dikonfigurasi di server (GOOGLE_CLIENT_ID).", 503)
    try:
        info = verify_google(body().get("credential"))
    except Exception:  # noqa: BLE001
        return error("Token Google tidak valid.", 401)
    email = info["email"].lower()
    u = user_by_email(email)
    if not u:
        u = new_user(unique_username(info.get("name") or email.split("@")[0]), email, "", True, info.get("picture", ""))
    elif not u["verified"]:
        # email terbukti milik pengguna Google ini; buang password dari pendaftaran yang belum diverifikasi
        u.update(verified=True, password_hash="")
    u["last_seen"] = time.time()
    return jsonify({"token": make_token(u["user_id"]), "user": user_public(u)})


@app.post("/api/auth/logout")
@auth_required
def logout():
    g.user["last_seen"] = 0.0
    return jsonify({"ok": True})


# ---- profil ----
@app.get("/api/me")
@auth_required
def get_me():
    return jsonify(user_public(g.user))


@app.put("/api/me")
@auth_required
def update_me():
    u, d = g.user, body()
    if "username" in d:
        name = clean(d["username"]) or ""
        if not USERNAME_RE.match(name):
            return error("Username 3-30 karakter (huruf, angka, spasi, titik, strip, underscore).")
        if username_taken(name, u["user_id"]):
            return error("Username sudah dipakai.", 409)
        u["username"] = name
    if "nim" in d:
        nim = clean(d["nim"]) or ""
        if len(nim) > 20:
            return error("NIM terlalu panjang.")
        u["nim"] = nim
    if "photo" in d:
        photo = d["photo"] or ""
        ok = photo.startswith("data:image/") and len(photo) < 400_000 or photo.startswith("https://") and len(photo) < 600
        if photo and not ok:
            return error("Foto tidak valid atau terlalu besar.")
        u["photo"] = photo
    return jsonify(user_public(u))


@app.put("/api/me/password")
@auth_required
def change_password():
    u, d = g.user, body()
    new = d.get("new_password") or ""
    if len(new) < 8:
        return error("Password baru minimal 8 karakter.")
    if u["password_hash"] and not check_password_hash(u["password_hash"], d.get("current_password") or ""):
        return error("Password saat ini salah.", 403)
    u["password_hash"] = generate_password_hash(new)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Akses proyek, progres, tampilan data
# ---------------------------------------------------------------------------
def membership(pid, uid):
    return next((m for m in DB["members"].values()
                 if m["project_id"] == pid and m["user_id"] == uid and m["status"] == "active"), None)


def access(pid):
    """Return (project, membership_saya, error_response)."""
    p = DB["projects"].get(pid)
    if not p:
        return None, None, error("Proyek tidak ditemukan.", 404)
    me = membership(pid, g.user["user_id"])
    if not me:
        return None, None, error("Anda bukan anggota proyek ini.", 403)
    return p, me, None


def recalc_progress(project_id):
    """Assignment & Progress Kalkulator: Done / total Task * 100."""
    project = DB["projects"].get(project_id)
    if not project:
        return
    tasks = [t for t in DB["tasks"].values() if t["project_id"] == project_id]
    done = sum(1 for t in tasks if t["status"] == "Done")
    project["progress_percentage"] = round(done / len(tasks) * 100) if tasks else 0


def project_view(p):
    tasks = [t for t in DB["tasks"].values() if t["project_id"] == p["project_id"]]
    return {
        **{k: v for k, v in p.items() if k != "owner_id"},
        "total_tasks": len(tasks),
        "done_tasks": sum(1 for t in tasks if t["status"] == "Done"),
        "is_owner": p["owner_id"] == g.user["user_id"],
    }


def member_view(m):
    view = {
        "member_id": m["member_id"], "project_id": m["project_id"], "status": m["status"],
        "email": m["email"], "role": m["role"], "description": m["description"], "is_owner": m["is_owner"],
    }
    if m["status"] == "active":
        u = DB["users"][m["user_id"]]
        view.update(user_id=u["user_id"], name=u["username"], nim=u["nim"] or "-", photo=u["photo"], online=is_online(u))
    else:
        view.update(user_id=None, name="Pending", nim="-", photo="", online=False,
                    email=m.get("invited_as") or m["email"])
    return view


def project_members(pid):
    ms = [m for m in DB["members"].values() if m["project_id"] == pid]
    ms.sort(key=lambda m: (m["status"] != "active", m["member_id"]))
    return [member_view(m) for m in ms]


# ---------------------------------------------------------------------------
# PROJECT (CRUD) - hanya untuk anggota aktif proyek
# ---------------------------------------------------------------------------
@app.get("/api/projects")
@auth_required
def list_projects():
    uid = g.user["user_id"]
    mine = [DB["projects"][m["project_id"]] for m in DB["members"].values()
            if m["user_id"] == uid and m["status"] == "active"]
    return jsonify([project_view(p) for p in sorted(mine, key=lambda p: p["project_id"])])


@app.get("/api/projects/<pid>")
@auth_required
def get_project(pid):
    p, _, err = access(pid)
    return err or jsonify(project_view(p))


@app.get("/api/projects/<pid>/board")
@auth_required
def get_board(pid):
    """Data satu papan: proyek + anggota (dengan status online) + tugas."""
    p, _, err = access(pid)
    if err:
        return err
    tasks = [t for t in DB["tasks"].values() if t["project_id"] == pid]
    return jsonify({"project": project_view(p), "members": project_members(pid), "tasks": tasks})


@app.post("/api/projects")
@auth_required
def create_project():
    d = body()
    name, course, deadline = clean(d.get("project_name")), clean(d.get("course_name")), clean(d.get("deadline"))
    if not name:
        return error("Nama proyek wajib diisi.")
    if not course:
        return error("Nama mata kuliah wajib diisi.")
    if not deadline:
        return error("Deadline wajib diisi.")
    pid = next_id("projects")
    DB["projects"][pid] = {
        "project_id": pid, "project_name": name, "course_name": course, "deadline": deadline,
        "progress_percentage": 0, "owner_id": g.user["user_id"],
    }
    mid = next_id("members")
    DB["members"][mid] = {
        "member_id": mid, "project_id": pid, "email": g.user["email"], "user_id": g.user["user_id"],
        "status": "active", "role": "-", "description": "-", "is_owner": True, "invited_by": None,
    }
    return jsonify(project_view(DB["projects"][pid])), 201


@app.put("/api/projects/<pid>")
@auth_required
def update_project(pid):
    p, _, err = access(pid)
    if err:
        return err
    d = body()
    for field in ("project_name", "course_name", "deadline"):
        if field in d:
            value = clean(d[field])
            if not value:
                return error(f"Field '{field}' tidak boleh kosong.")
            p[field] = value
    recalc_progress(pid)  # progres selalu dihitung dari Task, bukan diisi manual
    return jsonify(project_view(p))


@app.delete("/api/projects/<pid>")
@auth_required
def delete_project(pid):
    p, _, err = access(pid)
    if err:
        return err
    if p["owner_id"] != g.user["user_id"]:
        return error("Hanya pemilik proyek yang dapat menghapus proyek.", 403)
    del DB["projects"][pid]
    for key in ("tasks", "members"):
        for k in [k for k, v in DB[key].items() if v["project_id"] == pid]:
            del DB[key][k]
    return jsonify({"deleted": pid})


# ---------------------------------------------------------------------------
# MEMBER (CRUD) = keanggotaan di sebuah proyek: undang, lihat, ubah role/deskripsi, keluarkan
# ---------------------------------------------------------------------------
@app.get("/api/projects/<pid>/members")
@auth_required
def list_members(pid):
    _, _, err = access(pid)
    return err or jsonify(project_members(pid))


@app.post("/api/projects/<pid>/members")
@auth_required
def invite_member(pid):
    p, _, err = access(pid)
    if err:
        return err
    d = body()
    target = None
    if d.get("user_id"):
        target = DB["users"].get(d["user_id"])
    elif clean(d.get("username")):
        uname = clean(d["username"]).lower()
        target = next((u for u in DB["users"].values() if u["username"].lower() == uname), None)
    if target and target["verified"]:
        if target["user_id"] == g.user["user_id"]:
            return error("Anda sudah menjadi anggota proyek ini.", 409)
        email, invited_as = target["email"], "@" + target["username"]
    elif d.get("user_id") or clean(d.get("username")):
        return error("Username tidak ditemukan.", 404)
    else:
        email = (clean(d.get("email")) or "").lower()
        if not EMAIL_RE.match(email):
            return error("Format email tidak valid.")
        invited_as = email
    if any(m["project_id"] == pid and m["email"] == email for m in DB["members"].values()):
        return error("Email ini sudah menjadi anggota atau masih menunggu konfirmasi.", 409)
    mid = next_id("members")
    DB["members"][mid] = {
        "member_id": mid, "project_id": pid, "email": email, "user_id": None, "status": "pending",
        "role": "-", "description": "-", "is_owner": False, "invited_by": g.user["user_id"],
        "invited_as": invited_as,
    }
    link = f"{APP_URL}/?invite={invite_ser.dumps({'mid': mid, 'email': email})}"
    sent = send_mail(
        email, f"Undangan proyek {p['project_name']} di GroupTask Tracker",
        f"{g.user['username']} mengundang Anda bergabung di proyek \"{p['project_name']}\" ({p['course_name']}).\n\n"
        f"Klik untuk menerima undangan (login atau daftar dengan email ini):\n{link}\n\nLink berlaku 7 hari.",
    )
    resp = {"member": member_view(DB["members"][mid]), "email_sent": sent}
    if not EMAIL_ENABLED:
        resp["dev_invite_link"] = link
    return jsonify(resp), 201


@app.get("/api/projects/<pid>/users/search")
@auth_required
def search_users(pid):
    """Cari pengguna terdaftar untuk diundang. Maks 5 hasil.
    - Username: cocok sebagian (awalan diprioritaskan), cukup 1 huruf.
    - Email: hanya cocok persis (email penuh), agar daftar email tidak bocor.
    Hasil tidak memuat email; yang sudah jadi anggota/pending disembunyikan."""
    _, _, err = access(pid)
    if err:
        return err
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"results": [], "query_is_email": False, "already_member": False})
    taken = {m["email"] for m in DB["members"].values() if m["project_id"] == pid}
    is_email = bool(EMAIL_RE.match(q))
    ranked = []
    for u in DB["users"].values():
        if not u["verified"] or u["email"] in taken:
            continue
        name = u["username"].lower()
        if "@" in q:
            rank = 0 if u["email"] == q else None
        elif name == q:
            rank = 0
        elif name.startswith(q):
            rank = 1
        elif q in name:
            rank = 2
        else:
            rank = None
        if rank is not None:
            ranked.append((rank, name, u))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return jsonify({
        "results": [{"user_id": u["user_id"], "username": u["username"], "photo": u["photo"]} for _, _, u in ranked[:5]],
        "query_is_email": is_email,
        "already_member": is_email and q in taken,
    })


@app.get("/api/members/<mid>")
@auth_required
def get_member(mid):
    m = DB["members"].get(mid)
    if not m:
        return error("Anggota tidak ditemukan.", 404)
    _, _, err = access(m["project_id"])
    return err or jsonify(member_view(m))


@app.put("/api/members/<mid>")
@auth_required
def update_member(mid):
    """Role & deskripsi bisa diubah oleh anggota aktif mana pun, hanya berlaku di proyek ini."""
    m = DB["members"].get(mid)
    if not m:
        return error("Anggota tidak ditemukan.", 404)
    _, _, err = access(m["project_id"])
    if err:
        return err
    if m["status"] != "active":
        return error("Role dan deskripsi bisa diubah setelah undangan diterima.")
    d = body()
    if "role" in d:
        m["role"] = (clean(d["role"]) or "-")[:60]
    if "description" in d:
        m["description"] = (clean(d["description"]) or "-")[:300]
    return jsonify(member_view(m))


@app.delete("/api/members/<mid>")
@auth_required
def delete_member(mid):
    m = DB["members"].get(mid)
    if not m:
        return error("Anggota tidak ditemukan.", 404)
    _, me, err = access(m["project_id"])
    if err:
        return err
    if m["status"] == "active":
        if m is me and me["is_owner"]:
            return error("Pemilik tidak dapat keluar dari proyek. Hapus proyek jika tidak dipakai lagi.", 403)
        if m is not me and not me["is_owner"]:
            return error("Hanya pemilik proyek yang dapat mengeluarkan anggota.", 403)
    del DB["members"][mid]
    for t in DB["tasks"].values():  # dicabut dari daftar penanggung jawab tugas
        if mid in t["member_ids"]:
            t["member_ids"].remove(mid)
    return jsonify({"deleted": mid})


# ---- undangan (sisi penerima) ----
@app.get("/api/invitations")
@auth_required
def my_invitations():
    out = []
    for m in DB["members"].values():
        if m["status"] == "pending" and m["email"] == g.user["email"]:
            inviter = DB["users"].get(m["invited_by"])
            out.append({
                "member_id": m["member_id"], "project_name": DB["projects"][m["project_id"]]["project_name"],
                "invited_by": inviter["username"] if inviter else "-",
            })
    return jsonify(out)


@app.post("/api/invitations/accept")
@auth_required
def accept_invitation():
    d = body()
    mid = d.get("member_id")
    if d.get("token"):
        try:
            mid = invite_ser.loads(d["token"], max_age=INVITE_TTL).get("mid")
        except (BadSignature, SignatureExpired):
            return error("Link undangan tidak valid atau sudah kedaluwarsa.", 400)
    m = DB["members"].get(mid)
    if not m or m["status"] != "pending":
        return error("Undangan tidak ditemukan atau sudah diproses.", 404)
    if m["email"] != g.user["email"]:
        return error("Undangan ini ditujukan untuk email lain. Login memakai email yang diundang.", 403)
    m.update(user_id=g.user["user_id"], status="active")
    return jsonify({"member": member_view(m), "project_id": m["project_id"]})


@app.post("/api/invitations/decline")
@auth_required
def decline_invitation():
    m = DB["members"].get(body().get("member_id"))
    if not m or m["status"] != "pending" or m["email"] != g.user["email"]:
        return error("Undangan tidak ditemukan.", 404)
    del DB["members"][m["member_id"]]
    return jsonify({"declined": True})


# ---------------------------------------------------------------------------
# TASK (CRUD) + Assignment & Progress Kalkulator
# ---------------------------------------------------------------------------
def valid_assignee(pid, mid):
    m = DB["members"].get(mid)
    return bool(m and m["project_id"] == pid and m["status"] == "active")


def parse_assignees(d, pid):
    """Kembalikan (daftar member_id, error). Menerima member_ids (daftar) atau member_id (tunggal)."""
    if "member_ids" in d:
        raw = d["member_ids"] or []
    elif d.get("member_id"):
        raw = [d["member_id"]]
    else:
        raw = []
    if not isinstance(raw, list):
        return None, error("member_ids harus berupa daftar.")
    ids = list(dict.fromkeys(x for x in raw if x))
    if any(not valid_assignee(pid, x) for x in ids):
        return None, error("Penanggung jawab harus anggota aktif proyek ini.", 400)
    return ids, None


def task_access(tid):
    t = DB["tasks"].get(tid)
    if not t:
        return None, error("Tugas tidak ditemukan.", 404)
    _, _, err = access(t["project_id"])
    return (None, err) if err else (t, None)


@app.get("/api/tasks")
@auth_required
def list_tasks():
    uid = g.user["user_id"]
    pids = {m["project_id"] for m in DB["members"].values() if m["user_id"] == uid and m["status"] == "active"}
    tasks = [t for t in DB["tasks"].values() if t["project_id"] in pids]
    for key in ("project_id", "status"):
        if request.args.get(key):
            tasks = [t for t in tasks if t[key] == request.args[key]]
    if request.args.get("member_id"):
        tasks = [t for t in tasks if request.args["member_id"] in t["member_ids"]]
    return jsonify(tasks)


@app.get("/api/tasks/<tid>")
@auth_required
def get_task(tid):
    t, err = task_access(tid)
    return err or jsonify(t)


@app.post("/api/tasks")
@auth_required
def create_task():
    d = body()
    title, pid = clean(d.get("title")), d.get("project_id")
    status = d.get("status") or "To Do"
    if not title:
        return error("Judul tugas wajib diisi.")
    if not pid:
        return error("project_id wajib diisi.")
    if status not in STATUSES:
        return error(f"Status harus salah satu dari: {', '.join(STATUSES)}.")
    _, _, err = access(pid)
    if err:
        return err
    member_ids, err = parse_assignees(d, pid)
    if err:
        return err
    tid = next_id("tasks")
    DB["tasks"][tid] = {"task_id": tid, "project_id": pid, "member_ids": member_ids, "title": title, "status": status}
    recalc_progress(pid)
    return jsonify(DB["tasks"][tid]), 201


@app.put("/api/tasks/<tid>")
@auth_required
def update_task(tid):
    t, err = task_access(tid)
    if err:
        return err
    d = body()
    if "title" in d:
        if not clean(d["title"]):
            return error("Judul tugas tidak boleh kosong.")
        t["title"] = clean(d["title"])
    if "status" in d:
        if d["status"] not in STATUSES:
            return error(f"Status harus salah satu dari: {', '.join(STATUSES)}.")
        t["status"] = d["status"]
    if "member_ids" in d or "member_id" in d:
        ids, err2 = parse_assignees(d, t["project_id"])
        if err2:
            return err2
        t["member_ids"] = ids
    recalc_progress(t["project_id"])
    return jsonify(t)


@app.patch("/api/tasks/<tid>/assign")
@auth_required
def assign_task(tid):
    t, err = task_access(tid)
    if err:
        return err
    ids, err2 = parse_assignees(body(), t["project_id"])
    if err2:
        return err2
    t["member_ids"] = ids
    return jsonify(t)


@app.patch("/api/tasks/<tid>/status")
@auth_required
def change_status(tid):
    t, err = task_access(tid)
    if err:
        return err
    status = body().get("status")
    if status not in STATUSES:
        return error(f"Status harus salah satu dari: {', '.join(STATUSES)}.")
    t["status"] = status
    recalc_progress(t["project_id"])
    return jsonify({"task": t, "project": project_view(DB["projects"][t["project_id"]])})


@app.delete("/api/tasks/<tid>")
@auth_required
def delete_task(tid):
    t, err = task_access(tid)
    if err:
        return err
    del DB["tasks"][tid]
    recalc_progress(t["project_id"])
    return jsonify({"deleted": tid})


# ---------------------------------------------------------------------------
# Data contoh opsional (SEED_DEMO=1)
# ---------------------------------------------------------------------------
def seed_demo():
    u = new_user("demo", "demo@example.com", generate_password_hash("Demo12345"), True)
    pid = next_id("projects")
    DB["projects"][pid] = {"project_id": pid, "project_name": "Tugas Besar Cloud", "course_name": "Komputasi Awan dan Terdistribusi",
                           "deadline": "2026-12-10", "progress_percentage": 0, "owner_id": u["user_id"]}
    mid = next_id("members")
    DB["members"][mid] = {"member_id": mid, "project_id": pid, "email": u["email"], "user_id": u["user_id"], "status": "active",
                          "role": "Backend Dev", "description": "-", "is_owner": True, "invited_by": None}
    for title, status in [("Inisiasi Repositori Git", "Done"), ("Implementasi CRUD Fitur Proyek", "In Progress"), ("Slicing UI Homepage", "To Do")]:
        tid = next_id("tasks")
        DB["tasks"][tid] = {"task_id": tid, "project_id": pid, "member_ids": [mid], "title": title, "status": status}
    recalc_progress(pid)


if os.environ.get("SEED_DEMO") == "1":
    seed_demo()

if __name__ == "__main__":
    # 0.0.0.0 agar bisa diakses dari luar instance EC2
    app.run(host="0.0.0.0", port=5000, debug=False)
