import os, re, json, time, asyncio, hashlib, threading, traceback, datetime as dt, uuid, hmac
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException, Body, Depends
from fastapi.responses import FileResponse
from pylanis import Lanis
from pylanis.lanisapi import substitution as lanis_substitution
from pylanis.lanisapi import calendar as lanis_calendar
from pylanis.lanisapi import tasks as lanis_tasks
from pylanis.lanisapi.exceptions import WrongCredentialsError
from selectolax.lexbor import LexborHTMLParser as HTMLParser

E = os.environ
TZ = ZoneInfo("Europe/Berlin")
STATE = os.getenv("STATE_FILE", "state.json")
F = Fernet(E["SECRET_KEY"].encode())  # nur der Server kennt den Schlüssel
cache = {}  # uid -> (zeit, daten)
HOOK = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
TOPIC = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
BLOG_KEY = "__blog_posts__"
FEEDBACK_KEY = "__feedback__"
PRIVACY_KEY = "__privacy_acceptances__"
POLICY_VERSION = "2026-11-11-v3"
STATE_LOCK = threading.RLock()
feedback_limits = {}
FEEDBACK_LOCK = threading.Lock()


def db():
    with STATE_LOCK:
        try:
            with open(STATE, "rb") as f:
                raw = f.read()
        except OSError:
            return {}
        try:
            return json.loads(F.decrypt(raw).decode("utf-8"))
        except InvalidToken:
            # Migrate the existing plaintext JSON file on first read.
            try:
                legacy = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            save_db(legacy)
            return legacy
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}


def save_db(d):
    with STATE_LOCK:
        encrypted = F.encrypt(json.dumps(d, ensure_ascii=False).encode("utf-8"))
        temporary = STATE + ".tmp"
        with open(temporary, "wb") as f:
            f.write(encrypted)
        os.replace(temporary, STATE)


def is_admin(p):
    configured = E.get("ADMIN_USERNAME", "").strip().casefold()
    supplied = str(p.get("n", "")).strip().casefold()
    return bool(configured and supplied and hmac.compare_digest(configured, supplied))


def require_admin(p):
    if not is_admin(p):
        raise HTTPException(403, "Kein Admin-Zugang für dieses Konto")


def save_posts(posts):
    with STATE_LOCK:
        d = db()
        d[BLOG_KEY] = posts
        save_db(d)


def post_input(b):
    title = str(b.get("title", "")).strip()
    summary = str(b.get("summary", "")).strip()
    body = str(b.get("body", "")).strip()
    if not title or not body:
        raise HTTPException(400, "Titel und Text sind erforderlich")
    if len(title) > 160 or len(summary) > 400 or len(body) > 30000:
        raise HTTPException(413, "Der Beitrag ist zu lang")
    return {"title": title, "summary": summary, "body": body, "published": bool(b.get("published", False))}


def decode_token(x_token: str):
    try:
        return json.loads(F.decrypt(x_token.encode(), ttl=90 * 86400))
    except Exception:
        raise HTTPException(401)


def auth(x_token: str = Header("")):
    p = decode_token(x_token)
    accepted = db().get(PRIVACY_KEY, {}).get(p["u"], {})
    if accepted.get("version") != POLICY_VERSION:
        raise HTTPException(428, "Bitte bestätige zuerst die aktuelle Datenschutzerklärung")
    return p


def pick(o, fields):
    out = {}
    for f in fields:
        v = getattr(o, f, None)
        out[f] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


LOCK = threading.Lock()  # lanisapi nutzt globalen Zustand -> immer nur ein Nutzer gleichzeitig


def run(p, fn):
    """Pro Abruf eine eigene Lanis-Sitzung verwenden und danach schließen."""
    with LOCK:
        c = Lanis(school_id=p["s"], username=p["n"], password=p["p"])
        try:
            # Initialisiere die angemeldete Sitzung genau einmal. Der Apps-Dashboard-Aufruf
            # ist unnötig langsam und für die folgenden Abrufe nicht erforderlich.
            c._ensure_client()
            return fn(c)
        finally:
            c.reset()


def part(name, fn, errors):
    """Jeder Teil (Aufgaben, Vertretung, Kalender) darf einzeln scheitern, der Rest wird trotzdem angezeigt."""
    try:
        return fn()
    except Exception as ex:
        print(f"FETCH {name} FAIL:", traceback.format_exc(), flush=True)
        errors[name] = f"{type(ex).__name__}: {ex}"
        return None


def fetch(c):
    now = dt.datetime.now(TZ).replace(tzinfo=None)
    errors = {}
    overview = {"html": None}

    def meinunterricht_html():
        if overview["html"] is None:
            client = c._client
            if client is None:
                raise RuntimeError("Schulportal-Sitzung ist nicht initialisiert")
            url = "https://start.schulportal.hessen.de/meinunterricht.php?cacheBreaker=" + str(int(time.time() * 1000))
            overview["html"] = client.get_page(url).text
        return overview["html"]

    def tasks():
        # Aufgaben werden aus derselben Mein-Unterricht-Seite wie Kurse und Anwesenheit gelesen.
        out = []
        for t in lanis_tasks.parse_tasks(meinunterricht_html()):
            description = (getattr(t, "description", "") or "").strip()
            if not description:
                continue
            d = pick(t, ["title", "date", "subject_name", "teacher", "description", "details"])
            d["id"] = hashlib.sha1(f"{d['title']}{d['date']}{d['subject_name']}".encode()).hexdigest()[:12]
            out.append(d)
        return out

    def sub():
        plan = c.get_substitution_plan()
        return {
            "date": plan.date.isoformat() if plan.date else None,
            "info": plan.info,
            "items": [pick(s, ["hours", "class_name", "subject", "teacher", "substitute", "room", "notice"]) for s in plan.substitutions],
        }

    def cal():
        # Fetch the calendar directly through the authenticated session.
        # The pylanis convenience method re-checks the apps dashboard; that
        # dashboard sometimes returns an invalid response while the calendar
        # endpoint itself is healthy.
        client = c._client
        if client is None:
            raise RuntimeError("Schulportal-Sitzung ist nicht initialisiert")
        start, end = now - dt.timedelta(days=30), now + dt.timedelta(days=365)
        raw = client._call(lambda: lanis_calendar.fetch_events(client.session, start, end))
        events = lanis_calendar.parse_calendar(raw, start, end).events
        return [pick(e, ["title", "description", "place", "start", "end", "whole_day"]) for e in events]

    def attendance():
        # Mirror Lanis Mobile's selectors: it reads #anwesend/thead tr and
        # tbody tr from the Mein-Unterricht overview.
        tree = HTMLParser(meinunterricht_html())
        table = tree.css_first("#anwesend")
        if table is None:
            return {"available": False, "reason": "Die Tabelle #anwesend fehlt auf der Mein-Unterricht-Seite.", "courses": [], "totals": {}}

        header = table.css_first("thead tr")
        all_rows = table.css("tr")
        if header is None and all_rows:
            header = all_rows[0]
        if header is None:
            return {"available": False, "reason": "Die Anwesenheitstabelle enthält keine Spaltenüberschriften.", "courses": [], "totals": {}}

        keys = [cell.text(strip=True).lower() for cell in header.css("th, td")]
        if not keys:
            keys = [cell.text(strip=True).lower() for cell in header.iter() if cell.tag in {"th", "td"}]
        if not keys:
            return {"available": False, "reason": "Die Anwesenheitsspalten konnten nicht gelesen werden.", "courses": [], "totals": {}}

        rows = table.css("tbody tr")
        if not rows:
            rows = all_rows[1:]
        courses = []
        totals = {}
        for row in rows:
            cells = row.css("th, td")
            if not cells:
                continue
            name = cells[0].text(strip=True)
            teacher = cells[1].text(strip=True) if len(cells) > 1 else ""
            values = {}
            for index, key in enumerate(keys):
                if key in {"kurs", "lehrkraft"}:
                    continue
                value = cells[index].text(strip=True) if index < len(cells) else "0"
                if not value:
                    value = "0"
                values[key] = value
                match = re.search(r"\d+", value)
                if match:
                    totals[key] = totals.get(key, 0) + int(match.group())
            if name or values:
                courses.append({"name": name or "Kurs", "teacher": teacher, "values": values})

        return {
            "available": bool(courses),
            "reason": None if courses else "Die Tabelle ist vorhanden, enthält aber keine Kurszeilen.",
            "courses": courses,
            "totals": {key: str(value) for key, value in totals.items()},
        }

    def courses():
        tree = HTMLParser(meinunterricht_html())
        mappen = tree.css_first("#mappen")
        row = mappen.css_first(".row") if mappen else None
        found = []
        by_url = {}
        if row is not None:
            for mappe in tree.css("#mappen .row > div"):
                link = mappe.css_first("a.btn.btn-primary")
                heading = mappe.css_first("h2")
                if link is None or heading is None:
                    continue
                href = link.attributes.get("href", "")
                match = re.search(r"[?&]id=([^&#]+)", href)
                course_id = match.group(1) if match else href
                teachers = []
                for button in mappe.css("div.btn-group > button"):
                    title = (button.attributes.get("title", "") or "").split(" (")[0].strip()
                    short = button.text(strip=True)
                    teacher = title or short
                    if teacher and teacher not in teachers:
                        teachers.append(teacher)
                course = {
                    "id": course_id,
                    "semester": None,
                    "course_name": heading.text(strip=True),
                    "course_code": "",
                    "teacher": ", ".join(teachers),
                    "entries": [],
                }
                found.append(course)
                by_url[href] = course

        for entry in tree.css("#aktuellTable tr.printable"):
            date_node = entry.css_first(".datum")
            if date_node is None:
                continue
            course_link = entry.css_first("td > h3 > a")
            if course_link is None:
                continue
            course = by_url.get(course_link.attributes.get("href", ""))
            if course is None:
                continue
            date_text = date_node.text(strip=True)
            try:
                date = dt.datetime.strptime(date_text, "%d.%m.%Y").date().isoformat()
            except (TypeError, ValueError):
                date = date_text
            topic_node = entry.css_first(".thema")
            content_node = entry.css_first(".inhalt span.markup") or entry.css_first(".inhalt .markup")
            homework_node = entry.css_first(".homework .realHomework")
            topic = topic_node.text(strip=True) if topic_node else ""
            content = content_node.text(strip=True) if content_node else ""
            homework_text = homework_node.text(strip=True) if homework_node else ""
            if topic or content or homework_text:
                course["entries"].append({
                    "date": date,
                    "topic": topic,
                    "content": content,
                    "homework": homework_text,
                })

        if found:
            return found
        groups = c.get_learning_groups() or []
        return [
            {
                "id": getattr(group, "id", None),
                "semester": getattr(group, "semester", None),
                "course_name": getattr(group, "course_name", "") or "",
                "course_code": getattr(group, "course_code", "") or "",
                "teacher": getattr(group, "teacher", "") or "",
                "entries": [],
            }
            for group in groups
        ]

    def timetable():
        plan = c.get_timetable()
        return {
            "class_name": plan.class_name,
            "valid_from": plan.valid_from,
            "entries": [pick(e, ["hour", "time", "day", "subject", "room", "teacher", "info", "duration"]) for e in plan.entries],
        }

    return {
        "fetched": now.isoformat(),
        "tasks": part("Aufgaben", tasks, errors) or [],
        "attendance": part("Anwesenheit", attendance, errors) or {"available": False, "courses": [], "totals": {}},
        "courses": part("Kurse", courses, errors) or [],
        "sub": part("Vertretung", sub, errors),
        "cal": part("Kalender", cal, errors) or [],
        "timetable": part("Stundenplan", timetable, errors) or {"class_name": "", "valid_from": "", "entries": []},
        "errors": errors,
    }



def fetch_initial(c):
    """Fast first response: show the timetable while secondary portal data loads."""
    now = dt.datetime.now(TZ).replace(tzinfo=None)
    plan = c.get_timetable()
    return {
        "fetched": now.isoformat(),
        "tasks": [],
        "attendance": {"available": False, "courses": [], "totals": {}},
        "courses": [],
        "sub": None,
        "cal": [],
        "timetable": {
            "class_name": plan.class_name,
            "valid_from": plan.valid_from,
            "entries": [pick(e, ["hour", "time", "day", "subject", "room", "teacher", "info", "duration"]) for e in plan.entries],
        },
        "errors": {},
        "_partial": True,
    }


FETCH_LOCK = threading.Lock()
FETCHING = set()


def _refresh_user_data(p):
    uid = p["u"]
    try:
        result = run(p, fetch)
    except Exception as ex:
        print("BACKGROUND DATA FETCH FAIL:", type(ex).__name__, ex, flush=True)
        current = cache.get(uid, (0, {}))[1]
        result = dict(current)
        result.pop("_partial", None)
        result["errors"] = {**result.get("errors", {}), "Schulportal": f"{type(ex).__name__}: {ex}"}
    cache[uid] = (time.time(), result)
    with FETCH_LOCK:
        FETCHING.discard(uid)


def _start_data_refresh(p):
    uid = p["u"]
    with FETCH_LOCK:
        if uid in FETCHING:
            return
        FETCHING.add(uid)
    threading.Thread(target=_refresh_user_data, args=(dict(p),), daemon=True).start()


async def notify(s, msg):
    async with httpx.AsyncClient(timeout=15) as h:
        if TOPIC.match(s.get("ntfy", "")):
            await h.post("https://ntfy.sh/" + s["ntfy"], content=msg.encode(), headers={"Title": "Hausaufgaben"})
        if HOOK.match(s.get("discord", "")):
            await h.post(s["discord"], json={"content": msg})


async def reminders():
    while True:
        try:
            now = dt.datetime.now(TZ)
            today = now.date().isoformat()
            tom = (now.date() + dt.timedelta(days=1)).isoformat()
            sent = []
            for uid, s in db().items():
                try:
                    if now.strftime("%H:%M") < s.get("remindTime", "17:00") or s.get("sent") == today:
                        continue
                    done = s.get("done", [])
                    items = [f"• {h.get('subject') or '-'}: {h['title']}" for h in s.get("manual", [])
                             if h.get("due") == tom and h["id"] not in done]
                    items += [f"• {v.get('s') or '-'}: {v.get('t')}" for k, v in s.get("due", {}).items()
                              if isinstance(v, dict) and v.get("d") == tom and k not in done]
                    if items:
                        await notify(s, "📚 Morgen fällig:\n" + "\n".join(items))
                    sent.append(uid)
                except Exception as ex:
                    print("reminder error:", uid, ex)
            if sent:
                d = db()
                for uid in sent:
                    if uid in d:
                        d[uid]["sent"] = today
                save_db(d)
        except Exception as ex:
            print("reminder loop error:", ex)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_):
    asyncio.create_task(reminders())
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    # Resolve relative to this file so the page works regardless of Render's cwd.
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def pwa_manifest():
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent / "manifest.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def pwa_service_worker():
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/icon.svg")
def pwa_icon():
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent / "icon.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/my-schulportal-plus-logo.svg")
def brand_logo():
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent / "my-schulportal-plus-logo.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/datenschutz")
def privacy_page():
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent / "privacy.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/privacy/status")
def privacy_status(x_token: str = Header("")):
    p = decode_token(x_token)
    accepted = db().get(PRIVACY_KEY, {}).get(p["u"], {})
    return {"accepted": accepted.get("version") == POLICY_VERSION, "version": POLICY_VERSION}


@app.post("/api/privacy/accept")
def accept_privacy(b: dict = Body(...), x_token: str = Header("")):
    if b.get("accepted") is not True:
        raise HTTPException(400, "Die Bestätigung ist erforderlich")
    p = decode_token(x_token)
    d = db()
    records = d.setdefault(PRIVACY_KEY, {})
    records[p["u"]] = {"version": POLICY_VERSION, "accepted_at": dt.datetime.now(TZ).isoformat()}
    save_db(d)
    return {"ok": True, "version": POLICY_VERSION}


@app.get("/ping")
def ping():
    return "ok"


@app.get("/api/blog")
def public_blog():
    return sorted([post for post in db().get(BLOG_KEY, []) if post.get("published")], key=lambda post: post.get("created", ""), reverse=True)


@app.get("/api/admin/status")
def admin_status(p: dict = Depends(auth)):
    return {"admin": is_admin(p)}


@app.get("/api/admin/blog")
def admin_blog(p: dict = Depends(auth)):
    require_admin(p)
    return sorted(db().get(BLOG_KEY, []), key=lambda post: post.get("created", ""), reverse=True)


@app.post("/api/admin/blog")
def create_blog_post(b: dict = Body(...), p: dict = Depends(auth)):
    require_admin(p)
    post = post_input(b)
    now = dt.datetime.now(TZ).isoformat()
    post.update({"id": uuid.uuid4().hex, "created": now, "updated": now, "author": "Lukas"})
    posts = db().get(BLOG_KEY, [])
    posts.append(post)
    save_posts(posts)
    return post


@app.put("/api/admin/blog/{post_id}")
def update_blog_post(post_id: str, b: dict = Body(...), p: dict = Depends(auth)):
    require_admin(p)
    posts = db().get(BLOG_KEY, [])
    post = next((item for item in posts if item.get("id") == post_id), None)
    if post is None:
        raise HTTPException(404, "Beitrag nicht gefunden")
    post.update(post_input(b))
    post["updated"] = dt.datetime.now(TZ).isoformat()
    save_posts(posts)
    return post


@app.delete("/api/admin/blog/{post_id}")
def delete_blog_post(post_id: str, p: dict = Depends(auth)):
    require_admin(p)
    posts = db().get(BLOG_KEY, [])
    remaining = [item for item in posts if item.get("id") != post_id]
    if len(remaining) == len(posts):
        raise HTTPException(404, "Beitrag nicht gefunden")
    save_posts(remaining)
    return {"ok": True}


@app.post("/api/feedback")
def submit_feedback(b: dict = Body(...), p: dict = Depends(auth)):
    category = str(b.get("category", ""))
    message = str(b.get("message", "")).strip()
    if category not in {"bug", "idee", "sonstiges"}:
        raise HTTPException(400, "Bitte wähle eine Kategorie")
    if not message or len(message) > 3000:
        raise HTTPException(400, "Bitte gib eine Nachricht mit höchstens 3000 Zeichen ein")
    now = time.time()
    with FEEDBACK_LOCK:
        previous = feedback_limits.get(p["u"], 0)
        if now - previous < 30:
            raise HTTPException(429, "Bitte warte kurz, bevor du einen weiteren Bericht sendest")
        feedback_limits[p["u"]] = now
    d = db()
    reports = d.get(FEEDBACK_KEY, [])
    report = {"id": uuid.uuid4().hex, "category": category, "message": message,
              "page": str(b.get("page", ""))[:120], "created": dt.datetime.now(TZ).isoformat()}
    reports.append(report)
    d[FEEDBACK_KEY] = reports[-100:]
    save_db(d)
    return {"ok": True}


@app.get("/api/admin/feedback")
def list_feedback(p: dict = Depends(auth)):
    require_admin(p)
    return sorted(db().get(FEEDBACK_KEY, []), key=lambda item: item.get("created", ""), reverse=True)


@app.delete("/api/admin/feedback/{report_id}")
def delete_feedback(report_id: str, p: dict = Depends(auth)):
    require_admin(p)
    d = db()
    reports = d.get(FEEDBACK_KEY, [])
    remaining = [item for item in reports if item.get("id") != report_id]
    if len(remaining) == len(reports):
        raise HTTPException(404, "Meldung nicht gefunden")
    d[FEEDBACK_KEY] = remaining
    save_db(d)
    return {"ok": True}


_schools = []


def _load_schools():
    if not _schools:
        for s in Lanis.get_schools() or []:
            _schools.append({"id": str(s.id), "name": str(s.name), "city": str(s.city)})
    return _schools


@app.get("/api/schools")
async def schools(q: str = ""):
    q = q.strip().lower()
    if len(q) < 3:
        return []
    allx = await asyncio.to_thread(_load_schools)
    return [s for s in allx if q in f"{s['name']} {s['city']}".lower()][:12]


@app.post("/api/login")
async def login(b: dict = Body(...)):
    if b.get("privacy_accepted") is not True:
        raise HTTPException(428, "Bitte lies und bestätige die Datenschutzerklärung")
    if E.get("INVITE_CODE") and b.get("invite") != E["INVITE_CODE"]:
        raise HTTPException(403)
    school, user, pw = str(b.get("school", "")).strip(), str(b.get("username", "")).strip(), str(b.get("password", ""))
    started = time.perf_counter()
    try:
        # Verify credentials and load the timetable first; the rest is fetched in the background.
        initial_data = await asyncio.to_thread(run, {"s": school, "n": user, "p": pw}, fetch_initial)
    except Exception as ex:
        print("LOGIN FAIL:", type(ex).__name__, ex, flush=True)
        if isinstance(ex, WrongCredentialsError):
            raise HTTPException(401, "Schulnummer, Name oder Passwort stimmt nicht")
        raise HTTPException(502, f"Schulportal nicht erreichbar ({type(ex).__name__})")
    uid = hashlib.sha1(f"{school}:{user.lower()}".encode()).hexdigest()[:16]
    d = db()
    records = d.setdefault(PRIVACY_KEY, {})
    records[uid] = {"version": POLICY_VERSION, "accepted_at": dt.datetime.now(TZ).isoformat()}
    save_db(d)
    cache[uid] = (time.time(), initial_data)
    print(f"LOGIN PORTAL FETCH: {time.perf_counter() - started:.2f}s", flush=True)
    tok = F.encrypt(json.dumps({"u": uid, "s": school, "n": user, "p": pw}).encode()).decode()
    return {"token": tok}


@app.get("/api/data")
async def data(refresh: bool = False, p: dict = Depends(auth)):
    t, d = cache.get(p["u"], (0, None))
    if d and not refresh:
        stale = d.get("_partial") or time.time() - t > 600
        if stale:
            _start_data_refresh(p)
        out = dict(d)
        partial = out.pop("_partial", False)
        out["refreshing"] = bool(stale or partial)
    else:
        try:
            d = await asyncio.to_thread(run, p, fetch)
        except Exception as ex:
            raise HTTPException(502, f"Schulportal: {ex}")
        cache[p["u"]] = (time.time(), d)
        out = dict(d)
        out["refreshing"] = False
    out["profile"] = {
        "username": p["n"],
        "school_id": p["s"],
        "class_name": (out.get("timetable") or {}).get("class_name", ""),
    }
    return out


@app.get("/api/substitution")
async def substitution_for_day(date: str, p: dict = Depends(auth)):
    """Load the substitution plan for a selected day (not only the current day)."""
    try:
        selected = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "Datum muss im Format JJJJ-MM-TT angegeben werden")
    day = selected.strftime("%d.%m.%Y")

    def fetch_selected(c):
        client = c._client  # Lanis currently exposes only the current-day wrapper.
        if client is None:
            raise RuntimeError("Schulportal-Sitzung ist nicht initialisiert")
        client._require_app("Vertretungsplan")
        page = client._call(lambda: lanis_substitution.fetch_page(client.session))
        _, notice = lanis_substitution.parse_info(page)
        raw = client._call(lambda: lanis_substitution.fetch_substitutions(client.session, day))
        if raw == lanis_substitution.NO_PERMISSION_FOR_WHOLE_PLAN:
            try:
                raw = lanis_substitution.parse_own_entries(page, day)
            except Exception:
                # Students may only receive their own entries for the current
                # day in the HTML view. A future day can still be requested
                # when the school's endpoint allows the whole plan.
                raw = []
        plan = lanis_substitution.parse_plan(raw, day, notice)
        return {
            "date": plan.date.isoformat() if plan.date else selected.isoformat(),
            "info": plan.info,
            "items": [pick(s, ["hours", "class_name", "subject", "teacher", "substitute", "room", "notice"]) for s in plan.substitutions],
        }

    try:
        return await asyncio.to_thread(run, p, fetch_selected)
    except Exception as ex:
        print("FETCH Vertretung FAIL:", traceback.format_exc(), flush=True)
        raise HTTPException(502, f"Vertretungsplan konnte nicht geladen werden ({type(ex).__name__})")


@app.get("/api/state")
def get_state(p: dict = Depends(auth)):
    return db().get(p["u"], {"updated": 0})


@app.put("/api/state")
def put_state(s: dict = Body(...), p: dict = Depends(auth)):
    if len(json.dumps(s)) > 200_000:
        raise HTTPException(413)
    d = db()
    s["sent"] = d.get(p["u"], {}).get("sent", "")
    d[p["u"]] = s
    save_db(d)
    return {"ok": True}

