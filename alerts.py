#!/usr/bin/env python3
"""NQAD Alerts: posts YouTube live streams and new uploads to Discord webhooks."""
import argparse
import json
import logging
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (NQAD-Alerts)", "Accept-Language": "en-US,en;q=0.9"}
STATE_FILE = "state.json"
COLOR = 0xFF4A12
log = logging.getLogger("nqad")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("live", [])
            data.setdefault("videos", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"live": [], "videos": [], "first_run": True}


def save_state(state: dict) -> None:
    state["live"] = state["live"][-50:]
    state["videos"] = state["videos"][-50:]
    state.pop("first_run", None)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def post(webhook: str, content: str, embed: dict, role_id: str | None) -> None:
    payload = {
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"roles": [role_id] if role_id else []},
    }
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        log.info("Posted to Discord: HTTP %s", r.status)


def channel_id(handle: str) -> str:
    html = fetch(f"https://www.youtube.com/{handle}")
    m = re.search(r'"externalId":"(UC[\w-]{22})"', html) or re.search(
        r'"channelId":"(UC[\w-]{22})"', html
    )
    if not m:
        raise RuntimeError("Could not find YouTube channel ID")
    return m.group(1)


def check_live(handle: str, state: dict, webhook: str, role_id: str | None, dry: bool) -> None:
    html = fetch(f"https://www.youtube.com/{handle}/live")
    if '"isLiveNow":true' not in html:
        log.info("Not live")
        return
    vid = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
    if not vid:
        log.warning("Live flag found but no video ID")
        return
    vid = vid.group(1)
    if vid in state["live"]:
        log.info("Live %s already posted", vid)
        return
    t = re.search(r'<meta name="title" content="([^"]*)"', html)
    title = t.group(1) if t else "NQ After Dark is LIVE"
    url = f"https://www.youtube.com/watch?v={vid}"
    ping = f"<@&{role_id}> " if role_id else ""
    embed = {
        "title": f"🔴 {title}",
        "url": url,
        "description": "NQ futures live: order flow, London + NY session.\nCome trade with us 👇",
        "color": COLOR,
        "image": {"url": f"https://i.ytimg.com/vi/{vid}/maxresdefault_live.jpg"},
        "footer": {"text": "NQ After Dark · not financial advice"},
    }
    if dry:
        log.info("[dry] would post live %s", url)
    else:
        post(webhook, f"🔴 **We're LIVE!** {ping}\n{url}", embed, role_id)
    state["live"].append(vid)


def check_uploads(cid: str, state: dict, webhook: str, dry: bool) -> None:
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
    entries = root.findall("a:entry", ns)[:5]
    for e in reversed(entries):
        vid = e.findtext("yt:videoId", namespaces=ns)
        if not vid or vid in state["videos"] or vid in state["live"]:
            continue
        state["videos"].append(vid)
        if state.get("first_run"):
            continue
        title = e.findtext("a:title", default="New video", namespaces=ns)
        url = f"https://www.youtube.com/watch?v={vid}"
        embed = {
            "title": title,
            "url": url,
            "color": COLOR,
            "image": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"},
            "footer": {"text": "NQ After Dark"},
        }
        if dry:
            log.info("[dry] would post upload %s", url)
        else:
            post(webhook, f"📺 **New video is up!**\n{url}", embed, None)


def main() -> int:
    p = argparse.ArgumentParser(description="NQAD YouTube → Discord alerts")
    p.add_argument("--handle", default=os.getenv("YT_HANDLE", "@nqafterdark"))
    p.add_argument("--dry-run", action="store_true", help="log only, don't post")
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    live_hook = os.getenv("LIVE_WEBHOOK", "").strip()
    video_hook = os.getenv("VIDEO_WEBHOOK", "").strip() or live_hook
    role_id = os.getenv("ROLE_ID", "").strip() or None
    if not live_hook and not a.dry_run:
        log.error("LIVE_WEBHOOK secret is missing")
        return 1

    state = load_state()
    errors = 0
    try:
        check_live(a.handle, state, live_hook, role_id, a.dry_run)
    except Exception as ex:
        log.error("Live check failed: %s", ex)
        errors += 1
    try:
        check_uploads(channel_id(a.handle), state, video_hook, a.dry_run)
    except Exception as ex:
        log.error("Upload check failed: %s", ex)
        errors += 1
    save_state(state)
    return 1 if errors == 2 else 0


if __name__ == "__main__":
    sys.exit(main())
