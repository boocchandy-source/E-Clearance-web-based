# TCC eClearance System

Torres Capitol College — Web-Based Student Clearance System

Stack: **HTML + CSS + JavaScript** (frontend) + **Python (Flask) + SQLite** (backend/database).
Theme and logo: `static/img/tcc_logo.jpg` (black shield / silver metallic).

## Clearance flow — 16 offices in order

1. Dept. Treasurer
2. CSC Treasurer
3. GPTCA
4. Yearbook Coordinator
5. Program Adviser
6. School Dean
7. Librarian
8. Publication Dep't
9. Guidance Office
10. VP for Student Affairs
11. Registrar
12. VP for Academic Affairs
13. Cashier
14. Account Officer
15. VP for Finance & Marketing
16. GMT Scholarship Coordinator

**2nd Year students** also need **9 subject-instructor e-signatures**:

ITAPDC 1 & ITPC 2 — Dave Mark Ybanez · GE 5 & GE 6 — Gellie Babe Romolo ·
GEE 3 — Joylen C. Cifra · PE 3 — Khun Khurt Serra · ITCC 5 — April Jean Villas ·
ITAPDC 2 — Jessie Mae Jusayan · ITPE 1 — Wilfredo Villas

## Features

- **Student + Faculty login** with role tabs (`/login`) and registration (`/register`)
- **Student requests clearance** → one click sends it to all 16 offices (plus the 9 subject instructors for 2nd Year)
- **Faculty approves/declines** with remarks and an **e-signature** (draw on canvas, type a name, or upload a signature picture — stored as a base64 image in SQLite). Subject instructors see their own subject queue and sign each assigned subject.
- **Student sees who signed**: each office and subject row shows the instructor name, ID, remarks, date, and signature image
- **Live status**: after all assigned offices and instructors sign, the clearance reads **Cleared** and students can check it anytime
- **Printing is locked until cleared**: the certificate refuses incomplete clearances (`/certificate/<id>`)
- **Printable certificate** (`/certificate/<id>` → `window.print()` → Save as PDF) with office and instructor signature blocks
- If declined → notify, revise, and resubmit flow

---

## Deploy to Render

This folder is a self-contained project root: `app.py`, `requirements.txt`, `render.yaml`, and `render-free.yaml` sit at the top level. Push **this folder** to a GitHub repository, not its parent. If the repository root is the parent folder, set the Render service's **Root Directory** to `web` and point the Blueprint at `web/render.yaml`.

### Which Blueprint should I use?

| File | Plan | Storage | Use it for |
| --- | --- | --- | --- |
| `render.yaml` | Paid `0.5c-512mb` + 1 GB disk | **Persistent** `/var/data` | Real use, demos you cannot afford to lose, production |
| `render-free.yaml` | Free (`free`: 0.1 CPU / 512 MB) | **Ephemeral** | Evaluation only — data resets on restart/redeploy |

> The free plan has **no persistent disk**. Accounts, clearance requests, and
> e-signatures are erased whenever the service restarts, redeploys, or spins down
> after inactivity. Keep that in mind before entering real student data.

### Push this folder to GitHub

```bash
cd ~/chandy/chantot/web
git init
git add .
git commit -m "TCC eClearance"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

If the folder is already a Git repository, skip `git init` / `git remote add`
and just commit and push. `credentials.txt`, the local `eclearance.db`,
`.secret_key`, and `__pycache__/` are gitignored.

### Option A — persistent deployment (recommended)

1. Push this folder to GitHub.
2. Render Dashboard → **New** → **Blueprint** → select the repository.
3. Keep the Blueprint file path as `render.yaml` and click **Apply**.
4. When prompted for `FACULTY_SIGNUP_CODE`, enter a code if faculty should be able
   to self-register, or leave it blank to keep faculty signup closed.
5. Wait for the deploy, then open:

   ```
   https://<your-service>.onrender.com/healthz   →   {"status":"ok"}
   ```

`render.yaml` creates a Python web service, a 1 GB disk mounted at `/var/data`,
a generated `SECRET_KEY`, health checks on `/healthz`, and the Singapore region.

> The persistent disk requires a paid instance. On Render, disks cannot be
> attached to free services. Prices change; confirm the current plan cost in the
> Render dashboard before applying.

### Option B — free evaluation deployment

1. Render Dashboard → **New** → **Blueprint** → select the repository.
2. Change **Blueprint file path** to `render-free.yaml`, then apply.
3. Wait for the first boot and open `/healthz`.

The app works normally, but expect a **~50 second cold start** after the service
sleeps, and **all data to reset** after every restart or redeploy. This is the
mode to use for a quick class demo where persistence is not required.

### First boot

`init_db()` runs automatically when the web process starts. It creates the
schema, enables WAL mode, and seeds demo accounts when
`SEED_DEMO_ACCOUNTS=1`. Check:

```bash
curl https://<your-service>.onrender.com/healthz
# {"status":"ok"}
```

If you already created a **manual** Web Service in the dashboard, a Blueprint
creates a second service. Verify the new one, then delete the old service (or
keep the free one as a separate demo).

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | Generated by both Blueprints | Signs session cookies and CSRF tokens. Keep it stable. If missing, the app generates one and stores it at `.secret_key` beside the database. |
| `DB_PATH` | `./eclearance.db` | SQLite file. Paid Blueprint: `/var/data/eclearance.db`. Free plan: leave unset. |
| `APP_TIMEZONE` | `Asia/Manila` | Timezone for signature timestamps. |
| `SEED_DEMO_ACCOUNTS` | `1` | Seeds the demo student, 16 office accounts, and 7 instructor accounts into an empty database. Set `0` for a clean install. |
| `FACULTY_SIGNUP_CODE` | unset | When set, faculty self-registration is enabled and this value is the required access code. Unset = faculty signup **closed**. |
| `SESSION_COOKIE_SECURE` | `1` on Render, `0` locally | Send session cookies only over HTTPS. |
| `PORT` | `5000` | Injected by Render; used by gunicorn. |
| `GUNICORN_WORKERS` / `GUNICORN_THREADS` | `1` / `8` | Keep workers at **1** while SQLite is attached. |

### Create real accounts from the Render Shell

The public registration form only creates students. Faculty accounts can be
created in the Render Shell with the built-in CLI:

```bash
flask --app app list-users

# Office account (department must match one of the 16 offices)
flask --app app create-user --role faculty \
  --name "Maria Santos" --school-id FAC-REG-001 \
  --email registrar@tcc.edu --department "Registrar"

# Subject instructor (repeat --subject for each assigned subject)
flask --app app create-user --role faculty \
  --name "Dave Mark Ybanez" --school-id FAC-INST-001 \
  --email dave.ybanez@tcc.edu \
  --subject "ITAPDC 1" --subject "ITPC 2"

# Student account
flask --app app create-user --role student \
  --name "Juan Dela Cruz" --school-id STU-2025-0001 \
  --email juan@tcc.edu --course BSIT --year-level "2nd Year"

# Change a password (including a seeded demo account)
flask --app app set-password --email student@tcc.edu
```

Omit `--password` to have the CLI prompt for it securely, so the password does
not end up in your shell history. The commands run against the same `DB_PATH`
as the web service.

**For a real install:** set `SEED_DEMO_ACCOUNTS=0` **before the first boot**,
then create the 16 office accounts and 7 instructor accounts with the commands
above. Alternatively, keep the seeded accounts and immediately change every
password with `set-password`.

### Backups

Render does not back up persistent disks automatically. From the Render Shell:

```bash
python -c "import sqlite3; s=sqlite3.connect('/var/data/eclearance.db'); d=sqlite3.connect('/var/data/eclearance-backup.db'); s.backup(d); d.close(); s.close()"
python -c "import sqlite3; print('\n'.join(sqlite3.connect('/var/data/eclearance.db').iterdump()))" > /var/data/eclearance-backup.sql
```

Copy the backup off the disk before major changes. Do not scale the service to
multiple instances while using SQLite.

### Production checklist

- [ ] Use `render.yaml` (paid + disk), not the free Blueprint.
- [ ] Set a stable `SECRET_KEY` (the Blueprint generates one).
- [ ] Change or remove every demo account; never publish `credentials.txt`.
- [ ] Set `SEED_DEMO_ACCOUNTS=0` for a clean install, or change all seeded passwords.
- [ ] Set `FACULTY_SIGNUP_CODE`, or keep it unset and create faculty accounts yourself.
- [ ] Keep **one** instance while SQLite and the disk are attached.
- [ ] Take a database backup before schema changes or a major release.
- [ ] Open `/healthz` after each deploy and sign in once as a student and a faculty member.

### Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| First request returns `503` and takes ~50 s | Free instance waking from sleep. Retry after a minute. |
| `502`/`503` after a deploy | Open **Logs**. Usually a bad start command or a failure in `init_db()`. `/healthz` returns a JSON error if the DB is unreachable. |
| `attempt to write a readonly database` | The disk is not mounted, or `DB_PATH` points outside it. Use `/var/data/eclearance.db` with the paid Blueprint. |
| `database is locked` | More than one instance is writing to the same SQLite file. Keep one instance. |
| `persistent disks are not supported on the free plan` | Use `render-free.yaml` (data resets) or upgrade the service. |
| Everyone is logged out after a deploy | `SECRET_KEY` changed. Set it in the dashboard so sessions survive redeploys. |
| Data disappeared after a restart | Free plan has no disk. This is expected; move to `render.yaml`. |
| Signature upload rejected | Use a PNG, JPEG, or WebP under 2 MB. |

---

## Run locally

```bash
cd ~/chandy/chantot/web
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

`eclearance.db` auto-creates on first run (SQLite, no setup). `gunicorn` is the
production server and does not run on Windows; use `python app.py` locally. For
custom local settings, set the variables from `.env.example` in your shell
before running (the app reads process environment variables directly).

## Demo accounts (faculty password: `faculty123`)

> **Evaluation data only.** These accounts are created by the seed routine. Set
> `SEED_DEMO_ACCOUNTS=0` before the first production boot, or change/delete the
> accounts afterward. `credentials.txt` is gitignored and must never be published.

- Student: `student@tcc.edu / student123`
- Dept. Treasurer: `dept.treasurer@tcc.edu`
- CSC Treasurer: `csc.treasurer@tcc.edu`
- GPTCA: `gptca@tcc.edu`
- Yearbook Coordinator: `yearbook@tcc.edu`
- Program Adviser: `adviser@tcc.edu`
- School Dean: `dean@tcc.edu`
- Librarian: `librarian@tcc.edu`
- Publication Dep't: `publication@tcc.edu`
- Guidance Office: `guidance@tcc.edu`
- VP for Student Affairs: `vpsa@tcc.edu`
- Registrar: `registrar@tcc.edu`
- VP for Academic Affairs: `vpaa@tcc.edu`
- Cashier: `cashier@tcc.edu`
- Account Officer: `account.officer@tcc.edu`
- VP for Finance & Marketing: `vpfm@tcc.edu`
- GMT Scholarship Coordinator: `gmt@tcc.edu`

### 2nd Year subject instructors (password: `faculty123`)

- ITAPDC 1 / ITPC 2: `dave.ybanez@tcc.edu` — Dave Mark Ybanez
- GE 5 / GE 6: `gellie.romolo@tcc.edu` — Gellie Babe Romolo
- GEE 3: `joylen.cifra@tcc.edu` — Joylen C. Cifra
- PE 3: `khun.serra@tcc.edu` — Khun Khurt Serra
- ITCC 5: `april.villas@tcc.edu` — April Jean Villas
- ITAPDC 2: `jessie.jusayan@tcc.edu` — Jessie Mae Jusayan
- ITPE 1: `wilfredo.villas@tcc.edu` — Wilfredo Villas

## Test the full flow

1. Register a **2nd Year** student (the seeded demo student is 3rd Year) → login → + New Clearance Request → Send
2. Logout → login as e.g. `dept.treasurer@tcc.edu` → Review & E-Sign → draw, type, or upload a signature → Approve
3. Repeat for all 16 offices
4. Logout → login as each subject instructor (e.g. `dave.ybanez@tcc.edu`) → sign each assigned subject (Dave signs ITAPDC 1 and ITPC 2 separately)
5. Login as student → View + Signatures → see all office and instructor e-signatures → Print certificate

> 3rd/4th Year students keep the original 16-office flow — the 9 instructor rows are only added for **2nd Year** clearances.

## Files

- `app.py` — Flask + SQLite backend (config from env, CSRF, health check, account CLI, production entry point)
- `render.yaml` — paid Render Blueprint (web service, 1 GB disk, env vars, health check)
- `render-free.yaml` — free Render Blueprint (no disk; data resets)
- `gunicorn.conf.py` — production WSGI server settings (1 worker, 8 threads, preloaded app)
- `requirements.txt` — pinned runtime dependencies
- `.python-version` — Python 3.12.7 for Render
- `.env.example` — documented local environment variables
- `templates/` — HTML (base, index, login, register, student_dashboard, faculty_dashboard, clearance_view)
- `static/css/style.css` — TCC black/silver theme
- `static/js/main.js` — global UI helpers (flash auto-dismiss, modal behavior); page-specific tabs and signature-pad code live in the templates
- `static/img/tcc_logo.jpg` — logo

## Production notes

- Sessions and CSRF tokens are signed with `SECRET_KEY`; both Blueprints generate it automatically.
- All forms are CSRF-protected with Flask-WTF.
- Security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, HSTS on Render) are added to every response.
- Signature timestamps use `Asia/Manila`, not the server's UTC clock.
- SQLite runs in WAL mode with a 30-second busy timeout so reads and approval writes do not block each other.
- Keep a single web instance while SQLite is attached to the disk.
