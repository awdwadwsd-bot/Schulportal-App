import os, re, json, time, asyncio, hashlib, threading, traceback, datetime as dt, uuid, hmac
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header, HTTPException, Body, Depends
from fastapi.responses import FileResponse
from pylanis import Lanis
from pylanis.lanisapi import substitution as lanis_substitution
from pylanis.lanisapi.exceptions import WrongCredentialsError

E = os.environ
TZ = ZoneInfo("Europe/Berlin")
STATE = os.getenv("STATE_FILE", "state.json")
F = Fernet(E["SECRET_KEY"].encode())  # nur der Server kennt den Schlüssel
cache = {}  # uid -> (zeit, daten)
HOOK = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
TOPIC = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
BLOG_KEY = "__blog_posts__"
STATE_LOCK = threading.RLock()


def db():
    with STATE_LOCK:
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def save_db(d):
    with STATE_LOCK:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)


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


def auth(x_token: str = Header("")):
    try:
        return json.loads(F.decrypt(x_token.encode(), ttl=90 * 86400))
    except Exception:
        raise HTTPException(401)


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
            # Der SDK-Client meldet sich beim ersten Portalaufruf an.
            c.get_available_apps()
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

    def tasks():
        out = []
        for t in c.get_tasks() or []:
            # "Mein Unterricht" enthält auch reine Unterrichtseinträge. Nur
            # Einträge mit dem Portal-Feld realHomework sind Hausaufgaben.
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
        events = c.get_calendar(now - dt.timedelta(days=30), now + dt.timedelta(days=365))
        return [pick(e, ["title", "description", "place", "start", "end", "whole_day"]) for e in events]

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
        "sub": part("Vertretung", sub, errors),
        "cal": part("Kalender", cal, errors) or [],
        "timetable": part("Stundenplan", timetable, errors) or {"class_name": "", "valid_from": "", "entries": []},
        "errors": errors,
    }


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
    if E.get("INVITE_CODE") and b.get("invite") != E["INVITE_CODE"]:
        raise HTTPException(403)
    school, user, pw = str(b.get("school", "")).strip(), str(b.get("username", "")).strip(), str(b.get("password", ""))
    try:
        await asyncio.to_thread(run, {"s": school, "n": user, "p": pw}, lambda c: None)
    except Exception as ex:
        print("LOGIN FAIL:", type(ex).__name__, ex, flush=True)
        if isinstance(ex, WrongCredentialsError):
            raise HTTPException(401, "Schulnummer, Name oder Passwort stimmt nicht")
        raise HTTPException(502, f"Schulportal nicht erreichbar ({type(ex).__name__})")
    uid = hashlib.sha1(f"{school}:{user.lower()}".encode()).hexdigest()[:16]
    tok = F.encrypt(json.dumps({"u": uid, "s": school, "n": user, "p": pw}).encode()).decode()
    return {"token": tok}


@app.get("/api/data")
async def data(refresh: bool = False, p: dict = Depends(auth)):
    t, d = cache.get(p["u"], (0, None))
    if refresh or not d or time.time() - t > 600:
        try:
            d = await asyncio.to_thread(run, p, fetch)
        except Exception as ex:
            raise HTTPException(502, f"Schulportal: {ex}")
        cache[p["u"]] = (time.time(), d)
    out = dict(d)
    out["profile"] = {
        "username": p["n"],
        "school_id": p["s"],
        "class_name": (d.get("timetable") or {}).get("class_name", ""),
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

