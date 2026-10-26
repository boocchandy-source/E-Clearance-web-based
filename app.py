"""
TCC eClearance System
Torres Capitol College - Web-Based Student Clearance System
Stack: HTML + CSS + JavaScript (frontend) | Python Flask + SQLite (backend)
"""
import base64
import binascii
import hmac
import logging
import os
import secrets
import sqlite3
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = logging.getLogger("tcc_eclearance")


def env_flag(name, default=False):
    """Read a boolean environment variable (1/true/yes/on)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Render sets RENDER=true (and RENDER_EXTERNAL_HOSTNAME) on every service.
IS_RENDER = env_flag("RENDER") or bool(os.environ.get("RENDER_EXTERNAL_HOSTNAME"))
IS_PRODUCTION = IS_RENDER or os.environ.get("FLASK_ENV", "").lower() == "production"

# On Render, the persistent disk is mounted at /var/data (see render.yaml).
# Locally the SQLite file stays next to app.py, so `python app.py` keeps working.
DB_PATH = os.path.abspath(os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "eclearance.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def load_secret_key():
    """Return a stable session-signing key.

    render.yaml generates SECRET_KEY in production. When it is missing (for
    example on a manually-created Render service), persist a generated key
    beside the SQLite database so a restart does not log everyone out. On the
    paid disk this survives redeploys; on the free plan it survives restarts
    but not redeploys, so setting SECRET_KEY in the dashboard is still best.
    """
    configured = os.environ.get("SECRET_KEY", "").strip()
    if configured:
        return configured

    key_path = os.path.join(os.path.dirname(DB_PATH), ".secret_key")
    try:
        with open(key_path, "r", encoding="utf-8") as fh:
            saved = fh.read().strip()
        if saved:
            return saved
    except FileNotFoundError:
        pass
    except OSError:
        LOG.warning("Could not read %s; using an ephemeral SECRET_KEY.", key_path)
        return secrets.token_hex(32)

    key = secrets.token_hex(32)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # A concurrent worker won the race; use the key it wrote.
        try:
            with open(key_path, "r", encoding="utf-8") as fh:
                saved = fh.read().strip()
            if saved:
                return saved
        except OSError:
            pass
        return key
    except OSError as exc:
        LOG.warning("Could not persist SECRET_KEY at %s (%s); using an ephemeral key.", key_path, exc)
        return key

    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    LOG.warning(
        "SECRET_KEY is not set; generated one at %s. Set SECRET_KEY in the "
        "Render dashboard (or use render.yaml) for sessions that survive redeploys.",
        key_path,
    )
    return key


app = Flask(__name__)
# Render terminates TLS at its proxy; trust exactly one forwarded hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# A stable SECRET_KEY keeps sessions and CSRF tokens valid across restarts.
# render.yaml generates one automatically.
app.secret_key = load_secret_key()

# Signatures are posted as base64 data URLs (~2.7 MB for a 2 MB image) and the
# default 500 kB form-field cap in Flask 3.1 would reject them.
app.config.update(
    MAX_FORM_MEMORY_SIZE=4 * 1024 * 1024,
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_flag("SESSION_COOKIE_SECURE", default=IS_PRODUCTION),
)

csrf = CSRFProtect(app)

# The school is in the Philippines; keep signature timestamps in local time.
try:
    APP_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Manila"))
except (ZoneInfoNotFoundError, ValueError):
    APP_TZ = None


def now_str():
    current = datetime.now(APP_TZ) if APP_TZ else datetime.now()
    return current.strftime("%Y-%m-%d %H:%M:%S")


# Demo accounts are useful for evaluation but must be replaced for real use.
SEED_DEMO_ACCOUNTS = env_flag("SEED_DEMO_ACCOUNTS", default=True)
if IS_PRODUCTION and SEED_DEMO_ACCOUNTS:
    LOG.warning(
        "SEED_DEMO_ACCOUNTS is enabled. On an empty database this creates demo "
        "accounts with known passwords; set SEED_DEMO_ACCOUNTS=0 for a real install."
    )

# Set this to let faculty self-register; its value becomes the required access
# code. On Render, faculty signup stays closed while it is unset.
FACULTY_SIGNUP_CODE = os.environ.get("FACULTY_SIGNUP_CODE", "").strip()


def faculty_signup_enabled():
    return bool(FACULTY_SIGNUP_CODE) or not IS_PRODUCTION


def faculty_code_required():
    return bool(FACULTY_SIGNUP_CODE)


# E-signature images are stored as base64 data URLs. Keep them small and raster-only.
SIGNATURE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")
SIGNATURE_DATA_PREFIXES = tuple(f"data:{mime};base64," for mime in SIGNATURE_MIME_TYPES)
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024  # 2 MB decoded image

STATUS_LABELS = {
    "pending": "Pending",
    "in_review": "In review",
    "needs_revision": "Needs revision",
    "completed": "Cleared",
}

@app.template_filter("status_label")
def status_label(status):
    """Friendly status text — completed clearances read as 'Cleared'."""
    return STATUS_LABELS.get(status, (status or "").replace("_", " ").title())

DEPARTMENTS = [
    "Dept. Treasurer",
    "CSC Treasurer",
    "GPTCA",
    "Yearbook Coordinator",
    "Program Adviser",
    "School Dean",
    "Librarian",
    "Publication Dep't",
    "Guidance Office",
    "VP for Student Affairs",
    "Registrar",
    "VP for Academic Affairs",
    "Cashier",
    "Account Officer",
    "VP for Finance & Marketing",
    "GMT Scholarship Coordinator",
]

# 2nd Year BSIT subject instructors — 9 subjects, 7 instructors.
SECOND_YEAR_SUBJECTS = [
    ("ITAPDC 1", "DAVE MARK YBANEZ"),
    ("ITPC 2", "DAVE MARK YBANEZ"),
    ("GE 5", "GELLIE BABE ROMOLO"),
    ("GE 6", "GELLIE BABE ROMOLO"),
    ("GEE 3", "JOYLEN C. CIFRA"),
    ("PE 3", "KHUN KHURT SERRA"),
    ("ITCC 5", "APRIL JEAN VILLAS"),
    ("ITAPDC 2", "JESSIE MAE JUSAYAN"),
    ("ITPE 1", "WILFREDO VILLAS"),
]

# One account per instructor; instructors who handle two subjects sign both.
INSTRUCTOR_ACCOUNTS = [
    {
        "full_name": "DAVE MARK YBANEZ",
        "school_id": "FAC-INST-001",
        "email": "dave.ybanez@tcc.edu",
        "subjects": ["ITAPDC 1", "ITPC 2"],
    },
    {
        "full_name": "GELLIE BABE ROMOLO",
        "school_id": "FAC-INST-002",
        "email": "gellie.romolo@tcc.edu",
        "subjects": ["GE 5", "GE 6"],
    },
    {
        "full_name": "JOYLEN C. CIFRA",
        "school_id": "FAC-INST-003",
        "email": "joylen.cifra@tcc.edu",
        "subjects": ["GEE 3"],
    },
    {
        "full_name": "KHUN KHURT SERRA",
        "school_id": "FAC-INST-004",
        "email": "khun.serra@tcc.edu",
        "subjects": ["PE 3"],
    },
    {
        "full_name": "APRIL JEAN VILLAS",
        "school_id": "FAC-INST-005",
        "email": "april.villas@tcc.edu",
        "subjects": ["ITCC 5"],
    },
    {
        "full_name": "JESSIE MAE JUSAYAN",
        "school_id": "FAC-INST-006",
        "email": "jessie.jusayan@tcc.edu",
        "subjects": ["ITAPDC 2"],
    },
    {
        "full_name": "WILFREDO VILLAS",
        "school_id": "FAC-INST-007",
        "email": "wilfredo.villas@tcc.edu",
        "subjects": ["ITPE 1"],
    },
]

# ---------- DB helpers ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 30000")
    return g.db

@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def is_second_year(year_level):
    """Only 2nd Year students have the 9 subject-instructor signatures."""
    level = (year_level or "").strip().lower()
    return "2nd" in level or "second" in level

def subject_requirements(year_level):
    return SECOND_YEAR_SUBJECTS if is_second_year(year_level) else []

def instructor_subjects_for_user(user_id):
    """Subjects assigned to an instructor account (empty for office accounts)."""
    db = get_db()
    rows = db.execute(
        "SELECT subject FROM instructor_subjects WHERE user_id=? ORDER BY id", (user_id,)
    ).fetchall()
    return [r["subject"] for r in rows]

def ensure_instructor_approvals(clearance_id, year_level):
    """Make sure a 2nd Year clearance has all 9 instructor signature rows."""
    if not is_second_year(year_level):
        return
    db = get_db()
    for subject, instructor in SECOND_YEAR_SUBJECTS:
        db.execute(
            "INSERT OR IGNORE INTO instructor_approvals (clearance_id, subject, instructor_name) VALUES (?,?,?)",
            (clearance_id, subject, instructor),
        )
    db.commit()

def init_db():
    from werkzeug.security import generate_password_hash
    db = sqlite3.connect(DB_PATH, timeout=30)
    cur = db.cursor()
    cur.execute("PRAGMA busy_timeout = 30000")
    cur.execute("PRAGMA foreign_keys = ON")
    # WAL lets web threads read while an approval writes, and survives process
    # restarts on the Render persistent disk. If a deploy briefly overlaps an
    # old instance, keep the existing journal mode instead of failing to boot.
    try:
        cur.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        LOG.warning("Could not enable SQLite WAL mode; using the existing journal mode.")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL CHECK(role IN ('student','faculty')),
            full_name TEXT NOT NULL,
            school_id TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            department TEXT DEFAULT '',
            course TEXT DEFAULT '',
            year_level TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clearances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES users(id),
            school_year TEXT NOT NULL,
            semester TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clearance_id INTEGER NOT NULL REFERENCES clearances(id) ON DELETE CASCADE,
            department TEXT NOT NULL,
            faculty_id INTEGER REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','declined')),
            remarks TEXT DEFAULT '',
            signature_data TEXT DEFAULT '',
            signed_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(clearance_id, department)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS instructor_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subject TEXT NOT NULL,
            UNIQUE(user_id, subject)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS instructor_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clearance_id INTEGER NOT NULL REFERENCES clearances(id) ON DELETE CASCADE,
            subject TEXT NOT NULL,
            instructor_name TEXT NOT NULL,
            faculty_id INTEGER REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','declined')),
            remarks TEXT DEFAULT '',
            signature_data TEXT DEFAULT '',
            signed_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(clearance_id, subject)
        )
    """)
    db.commit()

    # seed demo accounts if empty (disable with SEED_DEMO_ACCOUNTS=0)
    count = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if SEED_DEMO_ACCOUNTS and count == 0:
        seeds = [
            ("student", "Juan Dela Cruz", "STU-2021-0001", "student@tcc.edu", "student123", "", "BSIT", "3rd Year"),
            ("faculty", "Carlos Dela Pena", "FAC-DT-001", "dept.treasurer@tcc.edu", "faculty123", "Dept. Treasurer", "", ""),
            ("faculty", "Jenny Lim", "FAC-CSC-001", "csc.treasurer@tcc.edu", "faculty123", "CSC Treasurer", "", ""),
            ("faculty", "Liza Ramos", "FAC-GPT-001", "gptca@tcc.edu", "faculty123", "GPTCA", "", ""),
            ("faculty", "Karen Cruz", "FAC-YB-001", "yearbook@tcc.edu", "faculty123", "Yearbook Coordinator", "", ""),
            ("faculty", "Paulo Santos", "FAC-ADV-001", "adviser@tcc.edu", "faculty123", "Program Adviser", "", ""),
            ("faculty", "Dr. Elena Torres", "FAC-DEAN-001", "dean@tcc.edu", "faculty123", "School Dean", "", ""),
            ("faculty", "Maria Santos", "FAC-LIB-001", "librarian@tcc.edu", "faculty123", "Librarian", "", ""),
            ("faculty", "Adrian Flores", "FAC-PUB-001", "publication@tcc.edu", "faculty123", "Publication Dep't", "", ""),
            ("faculty", "Ana Mendoza", "FAC-GUI-001", "guidance@tcc.edu", "faculty123", "Guidance Office", "", ""),
            ("faculty", "Dr. Mark Villanueva", "FAC-VPSA-001", "vpsa@tcc.edu", "faculty123", "VP for Student Affairs", "", ""),
            ("faculty", "Rosa Aquino", "FAC-REG-001", "registrar@tcc.edu", "faculty123", "Registrar", "", ""),
            ("faculty", "Dr. Sofia Navarro", "FAC-VPAA-001", "vpaa@tcc.edu", "faculty123", "VP for Academic Affairs", "", ""),
            ("faculty", "Jose Reyes", "FAC-CAS-001", "cashier@tcc.edu", "faculty123", "Cashier", "", ""),
            ("faculty", "Divina Gracia", "FAC-ACC-001", "account.officer@tcc.edu", "faculty123", "Account Officer", "", ""),
            ("faculty", "Roberto Chan", "FAC-VPFM-001", "vpfm@tcc.edu", "faculty123", "VP for Finance & Marketing", "", ""),
            ("faculty", "Grace Torres", "FAC-GMT-001", "gmt@tcc.edu", "faculty123", "GMT Scholarship Coordinator", "", ""),
        ]
        for role, name, sid, email, pw, dept, course, yl in seeds:
            cur.execute(
                "INSERT INTO users (role, full_name, school_id, email, password_hash, department, course, year_level) VALUES (?,?,?,?,?,?,?,?)",
                (role, name, sid, email, generate_password_hash(pw), dept, course, yl),
            )
        db.commit()
        LOG.info("Seeded demo accounts.")

    # Seed the 2nd Year subject-instructor accounts even on an existing database.
    if SEED_DEMO_ACCOUNTS:
        for inst in INSTRUCTOR_ACCOUNTS:
            existing = cur.execute(
                "SELECT id FROM users WHERE email=? OR school_id=?",
                (inst["email"], inst["school_id"]),
            ).fetchone()
            if existing:
                user_id = existing[0]
            else:
                cur.execute(
                    "INSERT INTO users (role, full_name, school_id, email, password_hash, department, course, year_level) VALUES (?,?,?,?,?,?,?,?)",
                    ("faculty", inst["full_name"], inst["school_id"], inst["email"],
                     generate_password_hash("faculty123"), "Subject Instructor", "", ""),
                )
                user_id = cur.lastrowid
            for subject in inst["subjects"]:
                cur.execute(
                    "INSERT OR IGNORE INTO instructor_subjects (user_id, subject) VALUES (?,?)",
                    (user_id, subject),
                )
        db.commit()

    # Backfill the 9 instructor rows for 2nd Year clearances that existed before this feature.
    existing_clearances = cur.execute("""
        SELECT c.id, u.year_level FROM clearances c
        JOIN users u ON c.student_id=u.id
    """).fetchall()
    for clearance_id, year_level in existing_clearances:
        if not is_second_year(year_level):
            continue
        for subject, instructor in SECOND_YEAR_SUBJECTS:
            cur.execute(
                "INSERT OR IGNORE INTO instructor_approvals (clearance_id, subject, instructor_name) VALUES (?,?,?)",
                (clearance_id, subject, instructor),
            )
    db.commit()
    db.close()


# ---------- Account management (Render Shell) ----------
@app.cli.command("create-user")
@click.option("--role", type=click.Choice(["student", "faculty"]), required=True)
@click.option("--name", "full_name", prompt="Full name")
@click.option("--school-id", prompt="School ID")
@click.option("--email", prompt="Email")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@click.option("--department", default="", help="Faculty office (one of the 16 departments).")
@click.option("--course", default="", help="Student course, e.g. BSIT.")
@click.option("--year-level", default="", help="Student year level, e.g. 2nd Year.")
@click.option("--subject", "subjects", multiple=True,
              help="Subject-instructor assignment; repeat for each subject.")
def create_user_command(role, full_name, school_id, email, password,
                        department, course, year_level, subjects):
    """Create a student or faculty account without the public registration form."""
    from werkzeug.security import generate_password_hash

    full_name = full_name.strip()
    school_id = school_id.strip()
    email = email.strip().lower()
    department = department.strip()
    course = course.strip()
    year_level = year_level.strip()

    if not full_name or not school_id or not email:
        raise click.ClickException("Name, school ID, and email are required.")
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise click.ClickException("Enter a valid email address.")
    if len(password) < 8:
        raise click.ClickException("Password must be at least 8 characters long.")

    subject_lookup = {name.upper(): name for name, _ in SECOND_YEAR_SUBJECTS}
    assigned_subjects = []
    for raw in subjects:
        subject = subject_lookup.get(raw.strip().upper())
        if not subject:
            valid = ", ".join(s for s, _ in SECOND_YEAR_SUBJECTS)
            raise click.ClickException(f"Unknown subject '{raw}'. Valid subjects: {valid}")
        if subject not in assigned_subjects:
            assigned_subjects.append(subject)

    if assigned_subjects and role != "faculty":
        raise click.ClickException("Only faculty accounts can be assigned subjects.")
    if assigned_subjects and department:
        raise click.ClickException("Use --subject for instructors, not --department.")
    if role == "faculty" and not assigned_subjects:
        if department not in DEPARTMENTS:
            raise click.ClickException(
                "--department must be one of: " + ", ".join(DEPARTMENTS)
            )
    if assigned_subjects:
        department = "Subject Instructor"

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (role, full_name, school_id, email, password_hash, department, course, year_level) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (role, full_name, school_id, email, generate_password_hash(password),
             department, course, year_level),
        )
        user_id = cur.lastrowid
        for subject in assigned_subjects:
            db.execute(
                "INSERT OR IGNORE INTO instructor_subjects (user_id, subject) VALUES (?,?)",
                (user_id, subject),
            )
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise click.ClickException("That email or school ID already exists.") from exc

    click.echo(f"Created {role} #{user_id}: {full_name} <{email}>")
    if assigned_subjects:
        click.echo("Assigned subjects: " + ", ".join(assigned_subjects))
    else:
        click.echo("Scope: " + (department or course or "student"))


@app.cli.command("set-password")
@click.option("--email", prompt="Email")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
def set_password_command(email, password):
    """Change an account password from the Render Shell."""
    from werkzeug.security import generate_password_hash

    email = email.strip().lower()
    if len(password) < 8:
        raise click.ClickException("Password must be at least 8 characters long.")

    db = get_db()
    user = db.execute("SELECT id, full_name FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        raise click.ClickException(f"No account found for {email}.")
    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(password), user["id"]),
    )
    db.commit()
    click.echo(f"Password updated for {user['full_name']} <{email}>.")


@app.cli.command("list-users")
def list_users_command():
    """List accounts, roles, and scopes (useful after a fresh deploy)."""
    rows = get_db().execute(
        "SELECT id, role, full_name, email, department, course, year_level "
        "FROM users ORDER BY role, full_name"
    ).fetchall()
    if not rows:
        click.echo("No accounts yet. Run create-user or enable SEED_DEMO_ACCOUNTS.")
        return
    click.echo(f"{len(rows)} account(s):")
    for row in rows:
        scope = row["department"] or " ".join(
            part for part in (row["course"], row["year_level"]) if part
        ) or "-"
        click.echo(f"  #{row['id']:<3} {row['role']:<8} {row['full_name']} <{row['email']}>  [{scope}]")


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login"))
            if role and u["role"] != role:
                flash("Unauthorized access.", "error")
                return redirect(url_for("dashboard"))
            return fn(*a, **kw)
        return wrapper
    return deco

def refresh_clearance_status(clearance_id):
    db = get_db()
    clearance = db.execute("""
        SELECT c.id, u.year_level FROM clearances c
        JOIN users u ON c.student_id=u.id WHERE c.id=?
    """, (clearance_id,)).fetchone()
    if not clearance:
        return "pending"
    ensure_instructor_approvals(clearance_id, clearance["year_level"])
    rows = db.execute("SELECT department, status FROM approvals WHERE clearance_id=?", (clearance_id,)).fetchall()
    instructor_rows = db.execute(
        "SELECT subject, status FROM instructor_approvals WHERE clearance_id=?", (clearance_id,)
    ).fetchall()
    statuses = [r["status"] for r in rows]
    covered = {r["department"] for r in rows}
    instructor_statuses = [r["status"] for r in instructor_rows]
    instructor_covered = {r["subject"] for r in instructor_rows}
    required_subjects = {s for s, _ in subject_requirements(clearance["year_level"])}
    if not statuses:
        new_status = "pending"
    elif any(s == "declined" for s in statuses) or any(s == "declined" for s in instructor_statuses):
        new_status = "needs_revision"
    elif (all(s == "approved" for s in statuses) and covered >= set(DEPARTMENTS)
          and all(s == "approved" for s in instructor_statuses)
          and instructor_covered >= required_subjects):
        new_status = "completed"
    elif (any(s in ("approved", "declined") for s in statuses)
          or any(s in ("approved", "declined") for s in instructor_statuses)):
        new_status = "in_review"
    else:
        new_status = "pending"
    db.execute("UPDATE clearances SET status=?, updated_at=? WHERE id=?",
               (new_status, now_str(), clearance_id))
    db.commit()
    return new_status

def clearance_is_cleared(clearance, approvals, instructor_approvals=None):
    """True only when every assigned office and instructor has signed."""
    if not clearance or clearance["status"] != "completed":
        return False
    approved = {a["department"] for a in approvals if a["status"] == "approved"}
    if approved < set(DEPARTMENTS):
        return False
    required_subjects = {s for s, _ in subject_requirements(clearance["year_level"])}
    if required_subjects:
        if instructor_approvals is None:
            return False
        signed_subjects = {a["subject"] for a in instructor_approvals if a["status"] == "approved"}
        if signed_subjects < required_subjects:
            return False
    return True

def clearance_with_details(clearance_id):
    db = get_db()
    c = db.execute("""
        SELECT c.*, u.full_name AS student_name, u.school_id AS student_no,
               u.course, u.year_level, u.email AS student_email
        FROM clearances c JOIN users u ON c.student_id=u.id WHERE c.id=?
    """, (clearance_id,)).fetchone()
    if not c:
        return None, [], []
    ensure_instructor_approvals(c["id"], c["year_level"])
    rows = db.execute("""
        SELECT a.*, u.full_name AS faculty_name, u.school_id AS faculty_no
        FROM approvals a LEFT JOIN users u ON a.faculty_id=u.id
        WHERE a.clearance_id=?
    """, (clearance_id,)).fetchall()
    # order by official 16-step flow order
    order = {d: i for i, d in enumerate(DEPARTMENTS)}
    approvals = sorted(rows, key=lambda r: order.get(r["department"], 99))
    instructor_rows = db.execute(
        "SELECT * FROM instructor_approvals WHERE clearance_id=?", (clearance_id,)
    ).fetchall()
    subject_order = {s: i for i, (s, _) in enumerate(SECOND_YEAR_SUBJECTS)}
    instructor_approvals = sorted(instructor_rows, key=lambda r: subject_order.get(r["subject"], 99))
    return c, approvals, instructor_approvals

# ---------- Routes ----------
@app.route("/")
def index():
    u = current_user()
    return render_template("index.html", user=u, departments=DEPARTMENTS)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        from werkzeug.security import check_password_hash
        if user and check_password_hash(user["password_hash"], password):
            if user["role"] != role:
                flash(f"This account is registered as {user['role']}. Please use the {user['role']} tab.", "error")
                return render_template("login.html", active_role=role)
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["name"] = user["full_name"]
            session["department"] = user["department"] or ""
            flash(f"Welcome, {user['full_name']}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    active_role = request.args.get("role", "student")
    return render_template("login.html", active_role=active_role)

def render_register():
    return render_template(
        "register.html",
        departments=DEPARTMENTS,
        faculty_signup_enabled=faculty_signup_enabled(),
        faculty_code_required=faculty_code_required(),
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        from werkzeug.security import generate_password_hash
        role = request.form.get("role", "student").strip()
        full_name = request.form.get("full_name", "").strip()
        school_id = request.form.get("school_id", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        department = request.form.get("department", "").strip() if role == "faculty" else ""
        course = request.form.get("course", "").strip() if role == "student" else ""
        year_level = request.form.get("year_level", "").strip() if role == "student" else ""
        if role not in ("student", "faculty"):
            flash("Please choose a valid account type.", "error")
            return render_register()
        if not all([full_name, school_id, email, password]):
            flash("Please fill in all required fields.", "error")
            return render_register()
        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "error")
            return render_register()
        if role == "faculty":
            if not faculty_signup_enabled():
                flash("Faculty self-registration is closed. Contact the administrator to create your account.", "error")
                return render_register()
            if faculty_code_required() and not hmac.compare_digest(
                request.form.get("faculty_code", "").strip().encode("utf-8"),
                FACULTY_SIGNUP_CODE.encode("utf-8"),
            ):
                flash("Invalid faculty access code.", "error")
                return render_register()
            if department not in DEPARTMENTS:
                flash("Please select a valid department.", "error")
                return render_register()
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (role, full_name, school_id, email, password_hash, department, course, year_level) VALUES (?,?,?,?,?,?,?,?)",
                (role, full_name, school_id, email, generate_password_hash(password), department, course, year_level),
            )
            db.commit()
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login", role=role))
        except sqlite3.IntegrityError:
            flash("Email or ID number already exists.", "error")
    return render_register()

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
def dashboard():
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    if u["role"] == "student":
        return redirect(url_for("student_dashboard"))
    return redirect(url_for("faculty_dashboard"))

# ----- Student -----
@app.route("/student")
@app.route("/student/dashboard")
@login_required(role="student")
def student_dashboard():
    u = current_user()
    db = get_db()
    rows = db.execute("SELECT * FROM clearances WHERE student_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    clearances = []
    for c in rows:
        status = refresh_clearance_status(c["id"])
        row = dict(c)
        row["status"] = status
        row["year_level"] = u["year_level"]
        approvals = db.execute("SELECT * FROM approvals WHERE clearance_id=?", (c["id"],)).fetchall()
        instructor_rows = db.execute(
            "SELECT * FROM instructor_approvals WHERE clearance_id=? ORDER BY id", (c["id"],)
        ).fetchall()
        amap = {a["department"]: a for a in approvals}
        imap = {a["subject"]: a for a in instructor_rows}
        approved = sum(1 for a in approvals if a["status"] == "approved")
        approved_instructors = sum(1 for a in instructor_rows if a["status"] == "approved")
        approved_total = approved + approved_instructors
        total = len(DEPARTMENTS) + len(instructor_rows)
        cleared = clearance_is_cleared(row, approvals, instructor_rows)
        clearances.append({"row": row, "approvals": amap, "instructor_rows": instructor_rows,
                           "instructor_map": imap, "approved": approved_total,
                           "approved_offices": approved, "approved_instructors": approved_instructors,
                           "total": total, "cleared": cleared})
    has_instructors = is_second_year(u["year_level"])
    instructor_count = len(SECOND_YEAR_SUBJECTS) if has_instructors else 0
    return render_template("student_dashboard.html", user=u, clearances=clearances,
                           departments=DEPARTMENTS, has_instructors=has_instructors,
                           instructor_count=instructor_count,
                           required_total=len(DEPARTMENTS) + instructor_count)

@app.route("/student/request", methods=["POST"])
@login_required(role="student")
def student_request():
    u = current_user()
    school_year = request.form.get("school_year", "2025-2026").strip()
    semester = request.form.get("semester", "1st Semester").strip()
    purpose = request.form.get("purpose", "Enrollment / Graduation Clearance").strip()
    db = get_db()
    cur = db.cursor()
    now = now_str()
    cur.execute(
        "INSERT INTO clearances (student_id, school_year, semester, purpose, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (u["id"], school_year, semester, purpose, now, now),
    )
    cid = cur.lastrowid
    for dept in DEPARTMENTS:
        cur.execute("INSERT OR IGNORE INTO approvals (clearance_id, department) VALUES (?,?)", (cid, dept))
    db.commit()
    ensure_instructor_approvals(cid, u["year_level"])
    refresh_clearance_status(cid)
    flash(f"Clearance request #{cid} sent to all {len(DEPARTMENTS)} offices"
          + (f" and {len(SECOND_YEAR_SUBJECTS)} subject instructors." if is_second_year(u["year_level"]) else "."),
          "success")
    return redirect(url_for("student_dashboard"))

@app.route("/clearance/<int:cid>")
def clearance_view(cid):
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    c, approvals, instructor_approvals = clearance_with_details(cid)
    if not c:
        abort(404)
    # permission: student owns it, or faculty (any office can view)
    if u["role"] == "student" and c["student_id"] != u["id"]:
        abort(403)
    status = refresh_clearance_status(cid)
    c = dict(c)
    c["status"] = status
    cleared = clearance_is_cleared(c, approvals, instructor_approvals)
    signed_offices = sum(1 for a in approvals if a["status"] == "approved")
    signed_instructors = sum(1 for a in instructor_approvals if a["status"] == "approved")
    signed = signed_offices + signed_instructors
    total_signatures = len(approvals) + len(instructor_approvals)
    pending_offices = [a["department"] for a in approvals if a["status"] != "approved"]
    pending_instructors = [a["subject"] for a in instructor_approvals if a["status"] != "approved"]
    my_subjects = instructor_subjects_for_user(u["id"]) if u["role"] == "faculty" else []
    my_subject_rows = [a for a in instructor_approvals if a["subject"] in set(my_subjects)]
    subject_order = {s: i for i, (s, _) in enumerate(SECOND_YEAR_SUBJECTS)}
    my_subject_rows.sort(key=lambda a: (a["status"] == "approved", subject_order.get(a["subject"], 99)))
    my_actionable_subjects = [a for a in my_subject_rows if a["status"] != "approved"]
    return render_template("clearance_view.html", user=u, clearance=c, approvals=approvals,
                           instructor_approvals=instructor_approvals,
                           departments=DEPARTMENTS, cleared=cleared, signed=signed,
                           total_signatures=total_signatures,
                           pending_offices=pending_offices,
                           pending_instructors=pending_instructors,
                           my_subject_rows=my_subject_rows,
                           my_actionable_subjects=my_actionable_subjects,
                           user_is_instructor=bool(my_subjects))

# ----- Faculty -----
@app.route("/faculty")
@app.route("/faculty/dashboard")
@login_required(role="faculty")
def faculty_dashboard():
    u = current_user()
    dept = u["department"]
    db = get_db()
    my_subjects = instructor_subjects_for_user(u["id"])
    if my_subjects:
        placeholders = ",".join("?" for _ in my_subjects)
        rows = db.execute(f"""
            SELECT c.*, u.full_name AS student_name, u.school_id AS student_no, u.course, u.year_level,
                   ia.status AS my_status, ia.remarks AS my_remarks, ia.subject AS my_subject
            FROM instructor_approvals ia
            JOIN clearances c ON ia.clearance_id=c.id
            JOIN users u ON c.student_id=u.id
            WHERE ia.subject IN ({placeholders}) ORDER BY c.id DESC
        """, my_subjects).fetchall()
    else:
        # requests that have an approval row for my department
        rows = db.execute("""
            SELECT c.*, u.full_name AS student_name, u.school_id AS student_no, u.course, u.year_level,
                   a.status AS my_status, a.remarks AS my_remarks
            FROM approvals a
            JOIN clearances c ON a.clearance_id=c.id
            JOIN users u ON c.student_id=u.id
            WHERE a.department=? ORDER BY c.id DESC
        """, (dept,)).fetchall()
    # stats
    total = len(rows)
    pending = sum(1 for r in rows if r["my_status"] == "pending")
    approved = sum(1 for r in rows if r["my_status"] == "approved")
    declined = sum(1 for r in rows if r["my_status"] == "declined")
    # all requests overview (for context)
    all_rows = db.execute("""
        SELECT c.*, u.full_name AS student_name FROM clearances c
        JOIN users u ON c.student_id=u.id ORDER BY c.id DESC LIMIT 20
    """).fetchall()
    scope_label = ", ".join(my_subjects) if my_subjects else dept
    return render_template("faculty_dashboard.html", user=u, rows=rows,
                           stats={"total": total, "pending": pending, "approved": approved, "declined": declined},
                           all_rows=all_rows, my_subjects=my_subjects,
                           is_instructor=bool(my_subjects), scope_label=scope_label)

def normalized_signature(signature_data, typed_sig):
    """Validate and normalize an e-signature. Returns (stored_value, error)."""
    if not signature_data and not typed_sig:
        return "", "E-signature is required to approve. Draw, type, or upload a picture of your signature."
    if signature_data:
        # Security/robustness: only accept small PNG/JPEG/WebP data URLs.
        prefix = next((p for p in SIGNATURE_DATA_PREFIXES if signature_data.startswith(p)), None)
        if not prefix:
            return "", "Signature image must be a PNG, JPEG, or WebP file. Please upload it again."
        try:
            raw_sig = base64.b64decode(signature_data[len(prefix):], validate=True)
        except (binascii.Error, ValueError):
            return "", "The signature image could not be read. Please upload it again."
        if len(raw_sig) > MAX_SIGNATURE_BYTES:
            return "", "Signature image is too large. Please use an image smaller than 2 MB."
        return signature_data, None
    # store typed signature as text marker; frontend renders it stylized
    return "TEXT:" + typed_sig, None

@app.route("/faculty/review/<int:cid>", methods=["POST"])
@login_required(role="faculty")
def faculty_review(cid):
    u = current_user()
    dept = u["department"]
    action = request.form.get("action", "approved")  # approved | declined
    remarks = request.form.get("remarks", "").strip()
    signature_data = request.form.get("signature_data", "").strip()
    typed_sig = request.form.get("typed_signature", "").strip()
    subject = request.form.get("subject", "").strip()
    if action not in ("approved", "declined"):
        flash("Invalid action.", "error")
        return redirect(url_for("clearance_view", cid=cid))

    db = get_db()
    my_subjects = instructor_subjects_for_user(u["id"])
    if my_subjects:
        # ----- Subject instructor signature -----
        if subject not in my_subjects:
            flash("Please choose one of your assigned subjects.", "error")
            return redirect(url_for("clearance_view", cid=cid))
        row = db.execute(
            "SELECT * FROM instructor_approvals WHERE clearance_id=? AND subject=?", (cid, subject)
        ).fetchone()
        if not row:
            flash("This subject is not assigned to this clearance.", "error")
            return redirect(url_for("clearance_view", cid=cid))
        stored_sig = ""
        if action == "approved":
            stored_sig, error = normalized_signature(signature_data, typed_sig)
            if error:
                flash(error, "error")
                return redirect(url_for("clearance_view", cid=cid))
        elif signature_data:
            stored_sig, error = normalized_signature(signature_data, "")
            if error:
                flash(error, "error")
                return redirect(url_for("clearance_view", cid=cid))
        now = now_str()
        db.execute("""
            UPDATE instructor_approvals SET faculty_id=?, status=?, remarks=?, signature_data=?,
                   signed_at=?, updated_at=? WHERE clearance_id=? AND subject=?
        """, (u["id"], action, remarks, stored_sig, now, now, cid, subject))
        db.commit()
        refresh_clearance_status(cid)
        flash(f"Request #{cid} marked as {action.upper()} for {subject}.",
              "success" if action == "approved" else "error")
        return redirect(url_for("clearance_view", cid=cid))

    # ----- Office approval signature -----
    stored_sig = ""
    if action == "approved":
        stored_sig, error = normalized_signature(signature_data, typed_sig)
        if error:
            flash(error, "error")
            return redirect(url_for("clearance_view", cid=cid))
    elif signature_data:
        stored_sig, error = normalized_signature(signature_data, "")
        if error:
            flash(error, "error")
            return redirect(url_for("clearance_view", cid=cid))
    row = db.execute("SELECT * FROM approvals WHERE clearance_id=? AND department=?", (cid, dept)).fetchone()
    if not row:
        flash("This request is not assigned to your office.", "error")
        return redirect(url_for("faculty_dashboard"))
    now = now_str()
    db.execute("""
        UPDATE approvals SET faculty_id=?, status=?, remarks=?, signature_data=?, signed_at=?, updated_at=?
        WHERE clearance_id=? AND department=?
    """, (u["id"], action, remarks, stored_sig, now, now, cid, dept))
    db.commit()
    refresh_clearance_status(cid)
    flash(f"Request #{cid} marked as {action.upper()} for {dept}.", "success" if action == "approved" else "error")
    return redirect(url_for("clearance_view", cid=cid))

@app.route("/certificate/<int:cid>")
def certificate(cid):
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    c, approvals, instructor_approvals = clearance_with_details(cid)
    if not c:
        abort(404)
    if u["role"] == "student" and c["student_id"] != u["id"]:
        abort(403)
    status = refresh_clearance_status(cid)
    c = dict(c)
    c["status"] = status
    if not clearance_is_cleared(c, approvals, instructor_approvals):
        scope = ("all assigned offices and instructors" if is_second_year(c["year_level"])
                 else "all assigned offices")
        flash(f"Printing is locked. The certificate can only be printed after {scope} have signed.", "error")
        return redirect(url_for("clearance_view", cid=cid))
    signed = (sum(1 for a in approvals if a["status"] == "approved")
              + sum(1 for a in instructor_approvals if a["status"] == "approved"))
    return render_template("clearance_view.html", user=u, clearance=c, approvals=approvals,
                           instructor_approvals=instructor_approvals,
                           departments=DEPARTMENTS, print_mode=True, cleared=True,
                           signed=signed, total_signatures=len(approvals) + len(instructor_approvals),
                           pending_offices=[], pending_instructors=[],
                           my_subject_rows=[], my_actionable_subjects=[],
                           user_is_instructor=False)

@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    flash("Your session expired or the form was invalid. Please try again.", "error")
    return redirect(url_for("dashboard"))


@app.errorhandler(413)
def handle_too_large(error):
    flash("That upload is too large. Please use a signature image smaller than 2 MB.", "error")
    return redirect(url_for("dashboard"))


@app.route("/healthz")
def healthz():
    """Render health check: verifies the app can reach the SQLite database."""
    try:
        get_db().execute("SELECT 1").fetchone()
    except sqlite3.Error:
        LOG.exception("Health check failed")
        return jsonify(status="error"), 503
    return jsonify(status="ok")


# Create/upgrade the database as soon as the WSGI worker imports the app.
# This replaces the old `python app.py`-only initialization.
init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT") or "5000"),
        debug=env_flag("FLASK_DEBUG"),
    )
