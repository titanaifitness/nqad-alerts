#!/usr/bin/env python3
"""NQAD community automations.

Modes:
  premarket  post NQ key levels + a long/short poll before the NY open
  totw       count Trade of the Week votes, announce winner, rotate role
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API = "https://discord.com/api/v10"
NY = ZoneInfo("America/New_York")
COLOR = 0xFF4A12
GOLD = 0xFFB020
FIRE = "🔥"
STATE_FILE = "community_state.json"
log = logging.getLogger("nqad-community")


# ---------- helpers ----------
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


class Discord:
    def __init__(self, token: str, dry: bool):
        self.token, self.dry = token, dry

    def call(self, path: str, method: str = "GET", body=None):
        if self.dry and method != "GET":
            log.info("[dry] %s %s %s", method, path, json.dumps(body)[:300] if body else "")
            return {"id": "dry"}
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(5):
            req = urllib.request.Request(
                API + path, data=data, method=method,
                headers={"Authorization": f"Bot {self.token}", "Content-Type": "application/json",
                         "User-Agent": "DiscordBot (nqad-community, 1.0)"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                    log.warning("Rate limited, waiting %.1fs", wait)
                    time.sleep(wait + 0.5)
                    continue
                raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:200]!r}") from e
        raise RuntimeError(f"{method} {path} failed after retries")


def fetch_bars() -> list[tuple[dt.datetime, float, float, float, float]]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/NQ=F?interval=5m&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for t, h, l, c, v in zip(res["timestamp"], q["high"], q["low"], q["close"], q["volume"]):
        if None in (h, l, c):
            continue
        bars.append((dt.datetime.fromtimestamp(t, NY), h, l, c, v or 0))
    if not bars:
        raise RuntimeError("No price data returned")
    return bars


def levels(now_ny: dt.datetime) -> dict:
    bars = fetch_bars()
    today = now_ny.date()
    rth_days = sorted({b[0].date() for b in bars
                       if b[0].date() < today and dt.time(9, 30) <= b[0].time() < dt.time(16, 0)})
    if not rth_days:
        raise RuntimeError("No previous regular session found")
    prev = rth_days[-1]
    rth = [b for b in bars if b[0].date() == prev and dt.time(9, 30) <= b[0].time() < dt.time(16, 0)]
    on_start = dt.datetime.combine(prev, dt.time(18, 0), NY)
    on_end = dt.datetime.combine(today, dt.time(9, 30), NY)
    overnight = [b for b in bars if on_start <= b[0] < min(on_end, now_ny + dt.timedelta(minutes=5))]
    out = {
        "date": prev.strftime("%a %d %b"),
        "pdh": max(b[1] for b in rth), "pdl": min(b[2] for b in rth), "pdc": rth[-1][3],
        "last": bars[-1][3],
    }
    out["vp"] = profile(rth)
    if overnight:
        out["onh"] = max(b[1] for b in overnight)
        out["onl"] = min(b[2] for b in overnight)
    return out


def profile(bars, step: float = 10.0) -> dict | None:
    """Approximate volume profile from 5m bars: spread each bar's volume evenly over its range."""
    vol: dict[float, float] = {}
    for _, h, l, _c, v in bars:
        if v <= 0:
            continue
        lo, hi = int(l // step), int(h // step)
        share = v / (hi - lo + 1)
        for k in range(lo, hi + 1):
            vol[k] = vol.get(k, 0) + share
    if len(vol) < 5:
        return None
    keys = list(range(min(vol), max(vol) + 1))
    raw = [vol.get(k, 0) for k in keys]
    sm = [sum(raw[max(0, i - 1):i + 2]) / len(raw[max(0, i - 1):i + 2]) for i in range(len(raw))]
    poc_i = max(range(len(sm)), key=sm.__getitem__)
    total, lo_i, hi_i, acc = sum(raw), poc_i, poc_i, raw[poc_i]
    while acc < 0.7 * total and (lo_i > 0 or hi_i < len(raw) - 1):
        up = raw[hi_i + 1] if hi_i < len(raw) - 1 else -1
        dn = raw[lo_i - 1] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1; acc += up
        else:
            lo_i -= 1; acc += dn
    peak = max(sm)
    lvns = [i for i in range(2, len(sm) - 2)
            if sm[i] < sm[i - 1] and sm[i] <= sm[i + 1] and sm[i] < 0.35 * peak
            and max(sm[:i]) > 0.5 * peak and max(sm[i + 1:]) > 0.5 * peak]
    picked = []
    for i in sorted(lvns, key=sm.__getitem__):
        if all(abs(i - j) >= 5 for j in picked):
            picked.append(i)
    lvns = picked[:3]
    mid = lambda i: (keys[i] + 0.5) * step
    return {"poc": mid(poc_i), "vah": (keys[hi_i] + 1) * step, "val": keys[lo_i] * step,
            "lvn": sorted(mid(i) for i in lvns)}


def fmt(x: float) -> str:
    return f"{x:,.2f}"


# ---------- premarket ----------
def premarket(d: Discord, channel: str, role: str | None, state: dict, force: bool) -> None:
    now = dt.datetime.now(NY)
    if now.weekday() >= 5 and not force:
        log.info("Weekend, skipping")
        return
    key = now.date().isoformat()
    if state.get("premarket_last") == key and not force:
        log.info("Already posted today")
        return
    lv = levels(now)
    gap = lv["last"] - lv["pdc"]
    lines = [
        f"**Prev. day high (PDH):** `{fmt(lv['pdh'])}`",
        f"**Prev. day low (PDL):** `{fmt(lv['pdl'])}`",
        f"**Prev. close:** `{fmt(lv['pdc'])}`",
    ]
    if "onh" in lv:
        lines += [f"**Overnight high (ONH):** `{fmt(lv['onh'])}`",
                  f"**Overnight low (ONL):** `{fmt(lv['onl'])}`"]
    vp = lv.get("vp")
    if vp:
        lines += ["", "**Volume profile (prev. session, approx.)**",
                  f"**POC:** `{fmt(vp['poc'])}` · **VAH:** `{fmt(vp['vah'])}` · **VAL:** `{fmt(vp['val'])}`"]
        if vp["lvn"]:
            lines.append("**LVN zones:** " + " · ".join(f"`{fmt(x - 5)}–{fmt(x + 5)}`" for x in vp["lvn"]))
        lines.append("")
    lines.append(f"**Now:** `{fmt(lv['last'])}` ({'+' if gap >= 0 else ''}{gap:,.2f} vs close)")
    embed = {
        "title": f"📍 NQ Pre-Market Levels | {now.strftime('%a %d %b')}",
        "description": "\n".join(lines) +
                       "\n\nVote below 👇 then catch the NY open live with us.\n*Levels are reference only, not financial advice.*",
        "color": COLOR,
        "footer": {"text": f"Prev. session: {lv['date']} · NQ futures"},
    }
    d.call(f"/channels/{channel}/messages", "POST", {
        "content": f"<@&{role}>" if role else "",
        "embeds": [embed],
        "allowed_mentions": {"roles": [role] if role else []},
    })
    d.call(f"/channels/{channel}/messages", "POST", {
        "poll": {
            "question": {"text": "NQ today: long or short at the open?"},
            "answers": [
                {"poll_media": {"text": "Long", "emoji": {"name": "🟢"}}},
                {"poll_media": {"text": "Short", "emoji": {"name": "🔴"}}},
                {"poll_media": {"text": "Sitting out / chop", "emoji": {"name": "😴"}}},
            ],
            "duration": 1,
            "allow_multiselect": False,
        }
    })
    state["premarket_last"] = key
    log.info("Premarket posted")


# ---------- trade of the week ----------
def snowflake_time(sid: str) -> dt.datetime:
    return dt.datetime.fromtimestamp(((int(sid) >> 22) + 1420070400000) / 1000, dt.timezone.utc)


def totw(d: Discord, guild: str, channel: str, role: str, announce: str | None,
         state: dict, force: bool) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    week = now.strftime("%G-W%V")
    if state.get("totw_last") == week and not force:
        log.info("Already decided this week")
        return
    since = now - dt.timedelta(days=7)
    msgs, before = [], None
    while True:
        q = "?limit=100" + (f"&before={before}" if before else "")
        page = d.call(f"/channels/{channel}/messages{q}")
        if not page:
            break
        msgs += page
        before = page[-1]["id"]
        if snowflake_time(before) < since or len(page) < 100:
            break
    entries = [m for m in msgs if snowflake_time(m["id"]) >= since and not m["author"].get("bot")
               and m.get("type", 0) in (0, 19)]
    best, best_votes = None, 0
    for m in sorted(entries, key=lambda m: int(m["id"])):
        if not any(r["emoji"]["name"] == FIRE for r in m.get("reactions", [])):
            continue
        users = d.call(f"/channels/{channel}/messages/{m['id']}/reactions/"
                       f"{urllib.parse.quote(FIRE)}?limit=100")
        votes = sum(1 for u in users if not u.get("bot") and u["id"] != m["author"]["id"])
        if votes > best_votes:
            best, best_votes = m, votes
    prev = state.get("totw_winner")
    if prev and (not best or prev != best["author"]["id"]):
        try:
            d.call(f"/guilds/{guild}/members/{prev}/roles/{role}", "DELETE")
        except RuntimeError as e:
            log.warning("Could not remove old role: %s", e)
    if not best:
        d.call(f"/channels/{channel}/messages", "POST", {"embeds": [{
            "title": "🏆 Trade of the Week",
            "description": "No votes this week. New week starts now: post your best trade and vote with 🔥.",
            "color": GOLD}]})
        state["totw_winner"] = None
    else:
        uid = best["author"]["id"]
        d.call(f"/guilds/{guild}/members/{uid}/roles/{role}", "PUT")
        link = f"https://discord.com/channels/{guild}/{channel}/{best['id']}"
        embed = {
            "title": "🏆 Trade of the Week: Winner",
            "description": f"Congrats <@{uid}>! **{best_votes} 🔥** votes.\n[See the trade]({link})\n\n"
                           "New week starts now: post your best trade and vote with 🔥.",
            "color": GOLD,
        }
        if best.get("attachments"):
            embed["image"] = {"url": best["attachments"][0]["url"]}
        body = {"content": f"<@{uid}>", "embeds": [embed], "allowed_mentions": {"users": [uid]}}
        d.call(f"/channels/{channel}/messages", "POST", body)
        if announce:
            d.call(f"/channels/{announce}/messages", "POST", body)
        state["totw_winner"] = uid
    state["totw_last"] = week
    log.info("TOTW done: %s", best["author"]["username"] if best else "no votes")


# ---------- main ----------
def main() -> int:
    p = argparse.ArgumentParser(description="NQAD community automations")
    p.add_argument("mode", choices=["premarket", "totw", "auto"])
    p.add_argument("--force", action="store_true", help="ignore day/week guards")
    p.add_argument("--dry-run", action="store_true", help="log instead of posting")
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token and not a.dry_run:
        log.error("DISCORD_TOKEN secret is missing")
        return 1
    cfg = {k: os.getenv(k, "").strip() for k in
           ("GUILD_ID", "LEVELS_CHANNEL", "LEVELS_ROLE", "TOTW_CHANNEL", "TOTW_ROLE", "TOTW_ANNOUNCE")}
    d = Discord(token, a.dry_run)
    state = load_state()
    mode = a.mode
    if mode == "auto":
        mode = "totw" if dt.datetime.now(NY).weekday() == 4 and dt.datetime.now(NY).hour >= 16 else "premarket"
    try:
        if mode == "premarket":
            premarket(d, cfg["LEVELS_CHANNEL"], cfg["LEVELS_ROLE"] or None, state, a.force)
        else:
            totw(d, cfg["GUILD_ID"], cfg["TOTW_CHANNEL"], cfg["TOTW_ROLE"],
                 cfg["TOTW_ANNOUNCE"] or None, state, a.force)
    except Exception as e:
        log.error("%s failed: %s", mode, e)
        save_state(state)
        return 1
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
def a():
    if x:
        return 1
