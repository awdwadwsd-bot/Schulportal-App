import os, re, json, time, asyncio, hashlib, datetime as dt
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Header, HTTPException, Body, Depends
from fastapi.responses import FileResponse
from lanisapi import LanisClient, LanisAccount

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


def client(p):
    c = LanisClient(LanisAccount(p["s"], p["n"], p["p"]))
    c.authenticate()
    return c


def fetch(p):
    c = client(p)
    try:
        now = dt.datetime.now(TZ).replace(tzinfo=None)
        tasks = []
        for t in c.get_tasks():
            d = pick(t, ["title", "date", "subject_name", "teacher", "description", "details"])
            d["id"] = hashlib.sha1(f"{d['title']}{d['date']}{d['subject_name']}".encode()).hexdigest()[:12]
            tasks.append(d)
        plan = c.get_substitution_plan()
        cal = c.get_calendar(now - dt.timedelta(days=7), now + dt.timedelta(days=90))
        return {
            "fetched": now.isoformat(),
            "tasks": tasks,
            "sub": {
                "date": plan.date.isoformat() if plan.date else None,
                "info": plan.info,
                "items": [pick(s, ["hours", "class_name", "subject", "teacher", "substitute", "room", "notice"]) for s in plan.substitutions],
            },
            "cal": [pick(e, ["title", "description", "place", "start", "end", "whole_day"]) for e in cal.events],
        }
    finally:
        c.close()


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
    return FileResponse("index.html")


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
        c = await asyncio.to_thread(client, {"s": school, "n": user, "p": pw})
        await asyncio.to_thread(c.close)
    except Exception:
        raise HTTPException(401)
    uid = hashlib.sha1(f"{school}:{user.lower()}".encode()).hexdigest()[:16]
    tok = F.encrypt(json.dumps({"u": uid, "s": school, "n": user, "p": pw}).encode()).decode()
    return {"token": tok}


@app.get("/api/data")
async def data(refresh: bool = False, p: dict = Depends(auth)):
    t, d = cache.get(p["u"], (0, None))
    if refresh or not d or time.time() - t > 600:
        try:
            d = await asyncio.to_thread(fetch, p)
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
