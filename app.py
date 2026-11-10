import os, re, json, time, asyncio, hashlib, threading, traceback, datetime as dt
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header, HTTPException, Body, Depends
from fastapi.responses import FileResponse
from lanisapi import LanisClient, LanisAccount
from lanisapi.exceptions import WrongCredentialsError
from lanisapi.helpers.request import Request
import lanisapi.client as _lc

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


def _get_sid(url, cookies, schoolid):
    """Ersatz für lanisapi.get_authentication_sid: das Schulportal hat das Cookie-Format geändert
    (die Bibliothek wirft IndexError und schreibt zudem die Session-ID ins Log)."""
    r = Request.head(url, cookies=cookies)
    m = re.search(r"\bsid=([0-9a-fA-F]+)", r.headers.get("set-cookie") or "")
    if not m:
        raise WrongCredentialsError("Keine Sitzung erhalten")
    ck = httpx.Cookies()
    ck.set("i", schoolid)
    ck.set("sid", m.group(1))
    return ck


_lc.get_authentication_sid = _get_sid

LOCK = threading.Lock()  # lanisapi nutzt globalen Zustand -> immer nur ein Nutzer gleichzeitig


def run(p, fn):
    """Frisch einloggen, fn(client) ausführen, danach alles aufräumen (keine Session-Reste zwischen Nutzern)."""
    with LOCK:
        c = None
        try:
            if os.path.exists("session.json"):
                os.remove("session.json")
            # lanisapi schließt seinen globalen HTTP-Client bei close() dauerhaft -> jedes Mal einen frischen anlegen
            Request.client = httpx.Client(timeout=httpx.Timeout(30.0, connect=60.0))
            c = LanisClient(LanisAccount(p["s"], p["n"], p["p"]))
            c.authenticate(force=True)
            if not c.authenticated:
                raise WrongCredentialsError("Login fehlgeschlagen")
            return fn(c)
        finally:
            if c:
                try:
                    c.close()
                except Exception:
                    pass
            if os.path.exists("session.json"):
                os.remove("session.json")


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
        r = c.get_calendar(now - dt.timedelta(days=7), now + dt.timedelta(days=90))
        return [pick(e, ["title", "description", "place", "start", "end", "whole_day"]) for e in r.events]

    return {
        "fetched": now.isoformat(),
        "tasks": part("Aufgaben", tasks, errors) or [],
        "sub": part("Vertretung", sub, errors),
        "cal": part("Kalender", cal, errors) or [],
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
    return FileResponse("index.html", headers={"Cache-Control": "no-cache"})


@app.get("/ping")
def ping():
    return "ok"


_schools = []


def _load_schools():
    if not _schools:
        for s in LanisClient(None).get_schools() or []:
            g = lambda *k: next((str(s[x]) for x in k if x in s), "")
            _schools.append({"id": g("Id", "id", "ID"), "name": g("Name", "name"), "city": g("Ort", "city", "Stadt")})
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
