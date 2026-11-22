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


# ---------- economic calendar ----------
IST = ZoneInfo("Asia/Kolkata")
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def calendar(state: dict, max_age_min: int = 60) -> list[dict]:
    """High-impact USD events this week, cached in state to respect the source's rate limits."""
    now = dt.datetime.now(dt.timezone.utc)
    cache = state.get("cal") or {}
    fetched = cache.get("fetched")
    if fetched and (now - dt.datetime.fromisoformat(fetched)).total_seconds() < max_age_min * 60:
        return cache["events"]
    req = urllib.request.Request(FF_URL, headers={"User-Agent": "Mozilla/5.0 (NQAD bot)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        if cache.get("events") is not None:
            log.warning("Calendar fetch failed, using cache: %s", e)
            return cache["events"]
        raise
    events = [{"title": e["title"], "date": e["date"], "forecast": e.get("forecast") or "",
               "previous": e.get("previous") or ""}
              for e in data if e.get("country") == "USD" and e.get("impact") == "High"]
    state["cal"] = {"fetched": now.isoformat(), "events": events}
    return events


def ev_time(e: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(e["date"])


def ev_line(e: dict) -> str:
    t = ev_time(e)
    extra = " · ".join(x for x in (f"F: {e['forecast']}" if e["forecast"] else "",
                                  f"P: {e['previous']}" if e["previous"] else "") if x)
    return (f"🔴 **{t.astimezone(IST).strftime('%I:%M %p')} IST** "
            f"({t.astimezone(NY).strftime('%I:%M %p')} ET) · **{e['title']}**"
            + (f"\n   ↳ {extra}" if extra else ""))


def post_calendar(d: Discord, cfg: dict, state: dict, force: bool) -> None:
    now = dt.datetime.now(NY)
    key = now.date().isoformat()
    if now.weekday() >= 5 and not force:
        log.info("Weekend, no calendar")
        return
    if state.get("cal_last") == key and not force:
        log.info("Calendar already posted today")
        return
    today = [e for e in calendar(state, 0) if ev_time(e).astimezone(NY).date() == now.date()]
    today.sort(key=ev_time)
    desc = ("\n".join(ev_line(e) for e in today) if today else
            "✅ No high-impact USD news today. Cleaner tape, normal size.")
    if today:
        desc += "\n\n⚠️ Expect spikes and slippage around these times. Flat or small size into the release."
    d.call(f"/channels/{cfg['NEWS_CHANNEL']}/messages", "POST", {"embeds": [{
        "title": f"📰 Red-Folder News | {now.strftime('%a %d %b')}",
        "description": desc + "\n\n*You'll get a ping 10 min before each release.*",
        "color": COLOR, "footer": {"text": "USD high-impact only · source: ForexFactory"}}]})
    state["cal_last"] = key
    log.info("Calendar posted (%d events)", len(today))


def news_warnings(d: Discord, cfg: dict, state: dict) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    warned = state.setdefault("warned", [])
    role = cfg.get("NEWS_ROLE")
    due = {}
    for e in calendar(state):
        mins = (ev_time(e) - now).total_seconds() / 60
        key = e["date"] + "|" + e["title"]
        if 0 < mins <= 20 and key not in warned:
            due.setdefault(e["date"], []).append((e, key))
    for date, items in sorted(due.items()):
        mins = int((dt.datetime.fromisoformat(date) - now).total_seconds() // 60)
        names = ", ".join(f"**{e['title']}**" for e, _ in items)
        d.call(f"/channels/{cfg['NEWS_CHANNEL']}/messages", "POST", {
            "content": (f"<@&{role}> " if role else "") +
                       f"⚠️ {names} in **{mins} min** "
                       f"({dt.datetime.fromisoformat(date).astimezone(IST).strftime('%I:%M %p')} IST). "
                       "Careful with size, expect volatility.",
            "allowed_mentions": {"roles": [role] if role else []}})
        warned += [k for _, k in items]
    state["warned"] = warned[-60:]


# ---------- countdown ----------
def countdown(d: Discord, cfg: dict, state: dict, force: bool = False) -> None:
    now = dt.datetime.now(NY)
    key = now.date().isoformat()
    in_window = now.weekday() < 5 and dt.time(8, 45) <= now.time() < dt.time(9, 30)
    if (not in_window or state.get("countdown_last") == key) and not force:
        return
    mins = max(1, int((dt.datetime.combine(now.date(), dt.time(9, 30), NY) - now).total_seconds() // 60))
    news = [e for e in (state.get("cal") or {}).get("events", [])
            if ev_time(e).astimezone(NY).date() == now.date()]
    extra = f"\n📰 News today: " + ", ".join(e["title"] for e in news[:3]) if news else ""
    d.call(f"/channels/{cfg['COUNTDOWN_CHANNEL']}/messages", "POST", {"embeds": [{
        "title": f"🔔 NY opens in {mins} min",
        "description": "Stream starting soon. Come trade the open with us 👇\n"
                       f"📺 https://youtube.com/@nqafterdark/live{extra}",
        "color": COLOR}]})
    state["countdown_last"] = key
    log.info("Countdown posted")


# ---------- payouts wall ----------
def payouts(d: Discord, cfg: dict, state: dict) -> None:
    ch = cfg["PAYOUTS_CHANNEL"]
    after = state.get("payouts_after")
    q = f"?limit=50" + (f"&after={after}" if after else "")
    msgs = sorted(d.call(f"/channels/{ch}/messages{q}") or [], key=lambda m: int(m["id"]))
    for m in msgs:
        state["payouts_after"] = m["id"]
        imgs = [a for a in m.get("attachments", []) if (a.get("content_type") or "").startswith("image")]
        if m["author"].get("bot") or not imgs:
            continue
        uid = m["author"]["id"]
        try:
            d.call(f"/guilds/{cfg['GUILD_ID']}/members/{uid}/roles/{cfg['FUNDED_ROLE']}", "PUT")
        except RuntimeError as e:
            log.warning("Funded role failed: %s", e)
        link = f"https://discord.com/channels/{cfg['GUILD_ID']}/{ch}/{m['id']}"
        note = (m.get("content") or "").strip()[:200]
        d.call(f"/channels/{cfg['WALL_CHANNEL']}/messages", "POST", {
            "content": f"🏆 <@{uid}> got paid!",
            "embeds": [{"description": (note + "\n\n" if note else "") + f"[Original post]({link})",
                        "image": {"url": imgs[0]["url"]}, "color": GOLD}],
            "allowed_mentions": {"users": []}})
        try:
            d.call(f"/channels/{ch}/messages/{m['id']}/reactions/{urllib.parse.quote('🏆')}/@me", "PUT")
        except RuntimeError:
            pass
        log.info("Payout walled for %s", m["author"]["username"])


# ---------- news role for members ----------
def sync_news_role(d: Discord, cfg: dict, state: dict) -> None:
    role, mem = cfg.get("NEWS_ROLE"), cfg.get("MEMBER_ROLE")
    if not role or not mem:
        return
    done = set(state.get("news_given", []))
    after = "0"
    while True:
        page = d.call(f"/guilds/{cfg['GUILD_ID']}/members?limit=1000&after={after}") or []
        for m in page:
            uid = m["user"]["id"]
            if m["user"].get("bot") or mem not in m["roles"] or uid in done:
                continue
            if role not in m["roles"]:
                d.call(f"/guilds/{cfg['GUILD_ID']}/members/{uid}/roles/{role}", "PUT")
            done.add(uid)
        if len(page) < 1000:
            break
        after = page[-1]["user"]["id"]
    state["news_given"] = sorted(done)


# ---------- weekly outlook ----------
def weekly(d: Discord, cfg: dict, state: dict, force: bool) -> None:
    now = dt.datetime.now(NY)
    week = now.strftime("%G-W%V")
    if state.get("weekly_last") == week and not force:
        log.info("Weekly already posted")
        return
    url = "https://query1.finance.yahoo.com/v8/finance/chart/NQ=F?interval=1h&range=1mo"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = [(dt.datetime.fromtimestamp(t, NY), o, h, l, c, v or 0)
            for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
            if None not in (o, h, l, c)]
    # last completed trading week (Sunday 18:00 ET -> Friday 17:00 ET)
    fri = now.date() - dt.timedelta(days=(now.weekday() - 4) % 7)
    if now.weekday() == 4 and now.hour < 17:
        fri -= dt.timedelta(days=7)
    start = dt.datetime.combine(fri - dt.timedelta(days=5), dt.time(18, 0), NY)
    end = dt.datetime.combine(fri, dt.time(17, 0), NY)
    wk = [b for b in bars if start <= b[0] < end]
    prev = [b for b in bars if start - dt.timedelta(days=7) <= b[0] < start]
    if not wk:
        raise RuntimeError("No bars for last week")
    hi, lo, op, cl = max(b[2] for b in wk), min(b[3] for b in wk), wk[0][1], wk[-1][4]
    chg = cl - (prev[-1][4] if prev else op)
    vp = profile([(b[0], b[2], b[3], b[4], b[5]) for b in wk], step=25.0)
    lines = [f"**Weekly high:** `{fmt(hi)}`", f"**Weekly low:** `{fmt(lo)}`",
             f"**Range:** `{fmt(hi - lo)}` pts", f"**Close:** `{fmt(cl)}` ({'+' if chg >= 0 else ''}{chg:,.2f})"]
    if vp:
        lines += ["", f"**Weekly POC:** `{fmt(vp['poc'])}` · **VAH:** `{fmt(vp['vah'])}` · **VAL:** `{fmt(vp['val'])}`"]
        if vp["lvn"]:
            lines.append("**Weekly LVNs:** " + " · ".join(f"`{fmt(x - 12.5)}–{fmt(x + 12.5)}`" for x in vp["lvn"]))
    events = sorted((e for e in calendar(state, 0) if ev_time(e) > dt.datetime.now(dt.timezone.utc)), key=ev_time)
    if events:
        lines += ["", "**📰 Big news this week**"]
        for e in events[:10]:
            t = ev_time(e)
            lines.append(f"• {t.astimezone(IST).strftime('%a %I:%M %p')} IST · **{e['title']}**")
    d.call(f"/channels/{cfg['WEEKLY_CHANNEL']}/messages", "POST", {"embeds": [{
        "title": f"🗓️ NQ Weekly Outlook | week of {(fri + dt.timedelta(days=3)).strftime('%d %b')}",
        "description": "\n".join(lines) + "\n\n*Approximate levels from hourly data. Not financial advice.*",
        "color": COLOR, "footer": {"text": f"Last week: {start.strftime('%d %b')} – {fri.strftime('%d %b')}"}}]})
    state["weekly_last"] = week
    log.info("Weekly posted")


# ---------- weekly stats (private) ----------
def stats(d: Discord, cfg: dict, state: dict, force: bool) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    week = now.strftime("%G-W%V")
    if state.get("stats_last") == week and not force:
        return
    g = d.call(f"/guilds/{cfg['GUILD_ID']}?with_counts=true")
    members = g.get("approximate_member_count", 0)
    prev = state.get("stats_members")
    since = now - dt.timedelta(days=7)
    counts, polls = {}, []
    for c in d.call(f"/guilds/{cfg['GUILD_ID']}/channels"):
        if c["type"] not in (0, 5) or c["id"] == cfg.get("STAFF_SKIP"):
            continue
        try:
            msgs, before = [], None
            for _ in range(5):
                page = d.call(f"/channels/{c['id']}/messages?limit=100" + (f"&before={before}" if before else ""))
                if not page:
                    break
                msgs += page
                before = page[-1]["id"]
                if snowflake_time(before) < since:
                    break
        except RuntimeError:
            continue
        for m in msgs:
            if snowflake_time(m["id"]) < since:
                continue
            if m.get("poll"):
                tot = sum(a.get("count", 0) for a in (m["poll"].get("results") or {}).get("answer_counts", []))
                polls.append(tot)
            if not m["author"].get("bot"):
                n = m["author"].get("global_name") or m["author"]["username"]
                counts[n] = counts.get(n, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:5]
    lines = [f"**Members:** {members}" + (f" ({'+' if members - prev >= 0 else ''}{members - prev} this week)" if prev is not None else ""),
             f"**Messages (7d):** {sum(counts.values())}",
             f"**Poll votes (7d):** {sum(polls)} across {len(polls)} polls"]
    if top:
        lines += ["", "**Most active**"] + [f"{i}. {n}: {c}" for i, (n, c) in enumerate(top, 1)]
    d.call(f"/channels/{cfg['STATS_CHANNEL']}/messages", "POST", {"embeds": [{
        "title": f"📊 Weekly Server Stats | {now.strftime('%d %b')}", "description": "\n".join(lines),
        "color": COLOR, "footer": {"text": "Private to staff · use these numbers in firm pitches"}}]})
    state["stats_last"], state["stats_members"] = week, members
    log.info("Stats posted")


def tick(d: Discord, cfg: dict, state: dict) -> None:
    for name, fn in (("news", lambda: news_warnings(d, cfg, state)),
                     ("countdown", lambda: countdown(d, cfg, state)),
                     ("payouts", lambda: payouts(d, cfg, state)),
                     ("newsrole", lambda: sync_news_role(d, cfg, state))):
        try:
            fn()
        except Exception as e:
            log.error("tick/%s failed: %s", name, e)


# ---------- main ----------
def main() -> int:
    p = argparse.ArgumentParser(description="NQAD community automations")
    p.add_argument("mode", choices=["premarket", "totw", "calendar", "tick", "weekly", "stats", "countdown", "auto"])
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
           ("GUILD_ID", "LEVELS_CHANNEL", "LEVELS_ROLE", "TOTW_CHANNEL", "TOTW_ROLE", "TOTW_ANNOUNCE",
            "NEWS_CHANNEL", "NEWS_ROLE", "MEMBER_ROLE", "COUNTDOWN_CHANNEL", "PAYOUTS_CHANNEL",
            "WALL_CHANNEL", "FUNDED_ROLE", "WEEKLY_CHANNEL", "STATS_CHANNEL")}
    d = Discord(token, a.dry_run)
    state = load_state()
    mode = a.mode
    if mode == "auto":
        mode = "totw" if dt.datetime.now(NY).weekday() == 4 and dt.datetime.now(NY).hour >= 16 else "premarket"
    try:
        if mode == "premarket":
            premarket(d, cfg["LEVELS_CHANNEL"], cfg["LEVELS_ROLE"] or None, state, a.force)
        elif mode == "calendar":
            post_calendar(d, cfg, state, a.force)
        elif mode == "tick":
            tick(d, cfg, state)
        elif mode == "weekly":
            weekly(d, cfg, state, a.force)
        elif mode == "stats":
            stats(d, cfg, state, a.force)
        elif mode == "countdown":
            countdown(d, cfg, state, a.force)
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
