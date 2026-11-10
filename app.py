import os, re, json, time, asyncio, hashlib, threading, traceback, datetime as dt
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header, HTTPException, Body, Depends
from fastapi.responses import FileResponse
from pylanis import Lanis
from pylanis.lanisapi.exceptions import WrongCredentialsError

E = os.environ
TZ = ZoneInfo("Europe/Berlin")
STATE = os.getenv("STATE_FILE", "state.json")
F = Fernet(E["SECRET_KEY"].encode())  # nur der Server kennt den Schlüssel
cache = {}  # uid -> (zeit, daten)
HOOK = re.compile(r"^https://(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")
TOPIC = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def db():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_db(d):
    json.dump(d, open(STATE, "w"), ensure_ascii=False)


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
        events = c.get_calendar(now - dt.timedelta(days=7), now + dt.timedelta(days=90))
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


@app.get("/ping")
def ping():
    return "ok"


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
    return d


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
