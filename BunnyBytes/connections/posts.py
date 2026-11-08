"""Scheduled posts: a post for one website account (e.g. a Reddit account to a subreddit) at a set time.

When one is due, Connections starts a Master Agent session that posts it with `$R upload` (the site's
saved script, or a scouting run the first time). Posts live in DATA/posts.json; an attached image or
video is kept in ACCOUNTS/media/ so Hermes can read it at <hermes accounts>/media/.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import suppress
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPEATS = ("once", "daily", "weekly")
MEDIA_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".mov"}
LATE = 3600        # a post missed by more than an hour (Connections was down) isn't sent late; Post now sends it
KEEP_RUNS = 10
ACCOUNT_MODES = ("rotate", "random")     # several accounts: take turns, or a random one each time
DECIDE = re.compile(r"\{\?([^{}]*)\}")      # {? what to decide} / {? a | b | c} / {?}: the agent fills it in


def has_decide(*values: str) -> bool:
    return any(DECIDE.search(v or "") for v in values)


def pick_account(post: dict, existing: set[str], rng=None) -> str:
    """The account this run posts from: the post's only account, or one of several by turns or at random."""
    import random
    choices = [a for a in post.get("accounts") or [post["account"]] if a in existing]
    if not choices:
        return ""
    if post.get("account_mode") == "random":
        return (rng or random.SystemRandom()).choice(choices)
    return choices[len(post.get("runs") or []) % len(choices)]


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def next_run(x: dict, after: float) -> float | None:
    """The next posting time (epoch seconds) after `after`, or None when a one-off has passed."""
    tz = zone(x.get("tz", ""))
    hour, minute = (int(v) for v in x["time"].split(":"))
    day = Date.fromisoformat(x["date"])
    at = lambda d: datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).timestamp()
    if x["repeat"] == "once":
        return at(day) if at(day) > after else None
    step = timedelta(days=1 if x["repeat"] == "daily" else 7)
    if at(day) <= after:        # jump close to `after` (DST can be an hour off), then step past it
        day += step * max(int((after - at(day)) // (step.days * 86400)) - 1, 0)
        while at(day) <= after:
            day += step
    return at(day)


def summary(x: dict) -> str:
    day = Date.fromisoformat(x["date"])
    when = {"once": day.strftime("%a %d %b %Y"), "daily": "every day", "weekly": "every " + day.strftime("%A")}[x["repeat"]]
    return f"{x['time']} {when}"


class PostStore:
    def __init__(self, data: Path, accounts: Path, hermes_accounts: str) -> None:
        self.path = data / "posts.json"
        self.media = accounts / "media"
        self.hermes_media = hermes_accounts.rstrip("/") + "/media"

    def load(self) -> list[dict]:
        with suppress(OSError, ValueError):
            items = json.loads(self.path.read_text())
            if isinstance(items, list):
                return items
        return []

    def save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(items, indent=2) + "\n")
        temp.replace(self.path)

    def get(self, post_id: str) -> dict | None:
        return next((x for x in self.load() if x["id"] == post_id), None)

    def put(self, post: dict) -> None:
        items = self.load()
        self.save([post if x["id"] == post["id"] else x for x in items] + ([] if any(x["id"] == post["id"] for x in items) else [post]))

    def delete(self, post_id: str) -> dict | None:
        items = self.load()
        post = next((x for x in items if x["id"] == post_id), None)
        if post:
            self.save([x for x in items if x["id"] != post_id])
            self.drop_media(post)
        return post

    def drop_media(self, post: dict) -> None:
        if post.get("media"):
            with suppress(OSError):
                (self.media / post["media"]).unlink()

    def media_path(self, post: dict) -> str:
        return f"{self.hermes_media}/{post['media']}" if post.get("media") else ""


def clean_post(body: dict, current: dict, now: float, allow_blank: bool = False) -> dict:
    """Validate the editable fields; raises ValueError with a message for the page."""
    def text(key: str, limit: int) -> str:
        return str(body.get(key) if key in body else current.get(key) or "").strip()[:limit]
    where, title, content, tags = text("where", 200), text("title", 300), text("body", 40000), text("tags", 300)
    time_, day, repeat = text("time", 5), text("date", 10), text("repeat", 10) or "once"
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_):
        raise ValueError("Give the time as HH:MM")
    try:
        Date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("Pick a date") from exc
    if repeat not in REPEATS:
        raise ValueError(f"Repeat is one of {', '.join(REPEATS)}")
    if not title and not content and not (allow_blank or current.get("batch") or current.get("media")):
        raise ValueError("Write a title or some text (or attach a file)")
    post = {**current, "where": where, "title": title, "body": content, "tags": tags, "time": time_, "date": day,
            "repeat": repeat, "tz": zone(text("tz", 64) or current.get("tz", "")).key,
            "notes": text("notes", 1000)}
    post["next"] = next_run(post, now)
    if post["next"] is None:
        raise ValueError("That time has already passed")
    post["enabled"] = True
    return post


def set_accounts(post: dict, accounts: list[str], mode: str) -> dict:
    post.update(account=accounts[0], accounts=accounts, account_mode=mode if mode in ACCOUNT_MODES else "rotate")
    return post


def new_post(accounts: list[str], body: dict, now: float, mode: str = "rotate", allow_blank: bool = False, extra: dict | None = None) -> dict:
    post = {"id": uuid.uuid4().hex[:10], "created": now, "runs": [], "media": "", **(extra or {})}
    return clean_post(body, set_accounts(post, accounts, mode), now, allow_blank)


def is_blank(post: dict) -> bool:
    return not (post.get("title") or post.get("body") or post.get("media") or post.get("awaiting_file"))


DECIDE_NOTE = ("PARTS FOR YOU TO DECIDE: each {? ...} in the post is yours to fill in. Replace it with your own choice, "
               "following what it says: `{?}` alone = your call; `{? a | b | c}` = pick one of those; `{? some instruction}` = "
               "write or choose what it asks for. Base it on the post itself (look at the file with your vision tool, if there is "
               "one) and on the account's purpose. For a subreddit, community or channel, pick one that exists, is active, allows "
               "this kind of post from this account, and whose rules (title format, flair, NSFW, self-promotion) you then follow. "
               "Replace only the {? } parts; send no {? } text to the site. `$R say` what you chose before you post.\n")


def agent_prompt(post: dict, account_id: str, account: dict, media: str) -> str:
    post = {**post, "account": account_id}
    name = account.get("name") or account_id
    values = {k: v for k, v in {"where": post.get("where"), "title": post.get("title"), "text": post.get("body"),
                                "tags": post.get("tags"), "file": media}.items() if v}
    command = (f'`$R upload {post["account"]}' + (f' "{media}"' if media else "") + ' --title "..." --caption "..."'
               + (' --tags "..."' if post.get("tags") else "") + ' --field "NAME=..."`')
    head = f"📮 Scheduled post to {name}" + (f" {post['where']}" if post.get("where") else "") + f": {post.get('title') or post.get('body', '')[:60]}"
    return (f"{head}\nSCHEDULED POST ({summary(post)}): the user scheduled this post in Connections → Scheduled posts. Post it now to the "
            f"website account {post['account']} ({name}, {account.get('url')}); it is already approved, don't ask for confirmation.\n"
            f"The post, exactly as the user wrote it (JSON):\n{json.dumps(values, ensure_ascii=False, indent=2)}\n"
            "Run `$R accounts` to see the site's fields, then " + command + ": the text goes in --caption, and \"where\" "
            "(the subreddit, community, board or channel) in the site's own field for it (e.g. --field \"subreddit=r/...\"). "
            + ("" if media else "There is no file: it is a text post; leave the file out. ")
            + (f"The user's notes: {post['notes']}\n" if post.get("notes") else "")
            + (DECIDE_NOTE if has_decide(*values.values()) else "")
            + ("THE POST IS BLANK: the user left it for you. Write the title and text yourself, fitting the account's purpose "
               "and the destination's rules, as a {? } part would be. `$R say` what you wrote before you post.\n"
               if not (post.get("title") or post.get("body") or media) else
               "There is no title: if the site needs one, write a fitting one yourself (as for a {? } part).\n" if not post.get("title") else "")
            + ("" if post.get("tags") else "No tags or flair were given: if the site or community requires them, pick fitting ones.\n")
            + "Follow what upload returns. Don't change the wording" + (" outside the {? } parts" if has_decide(*values.values()) else "") + ". If the site refuses the post (rules, flair, "
            "karma, rate limit), don't retry around it: `$R notify` the user why and end. When it is up, `$R say` its link.")


# ---------------- bulk scheduling ----------------
# Many posts at once: pasted blocks (first line = title, the rest = text, blocks split by a "---" line)
# and/or files (one post each, paired with the blocks in order). Times start at a set moment and are
# spaced by an interval with random jitter, kept inside a daily posting window on the chosen weekdays.

MAX_BULK = 200


def parse_blocks(raw: str) -> list[dict]:
    blocks = []
    for chunk in re.split(r"^\s*---+\s*$", raw or "", flags=re.M):
        lines = chunk.strip().splitlines()
        if lines:
            blocks.append({"title": lines[0].strip()[:300], "body": "\n".join(lines[1:]).strip()[:40000]})
    return blocks


def file_title(template: str, name: str, n: int) -> str:
    stem = re.sub(r"[_\-.]+", " ", Path(name).stem).strip()
    return (template or "{name}").replace("{name}", stem).replace("{n}", str(n)).strip()[:300]


def _minutes(value, label: str, low: float, high: float) -> float:
    try:
        v = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not low <= v <= high:
        raise ValueError(f"{label} must be {low:g}-{high:g}")
    return v


def plan_times(n: int, plan: dict, now: float, rng=None, existing: list[float] = ()) -> list[float]:
    """n posting times (epoch seconds): start, then every interval ± jitter, moved into the window.
    `existing` = the batch's other post times: new ones come after them and count toward each day's posts.
    No start date = start now (continuing a batch)."""
    import random
    rng = rng or random.SystemRandom()
    tz = zone(plan.get("tz", ""))
    if not plan.get("start_date"):
        start = now
    else:
        try:
            start = datetime.fromisoformat(f"{plan['start_date']}T{plan.get('start_time') or '00:00'}").replace(tzinfo=tz).timestamp()
        except ValueError as exc:
            raise ValueError("Pick a start date and time") from exc
    window = [str(plan.get("window_start") or "00:00"), str(plan.get("window_end") or "23:59")]
    if not all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", w) for w in window):
        raise ValueError("Give the posting window as HH:MM")
    w0, w1 = ((int(w[:2]) * 60 + int(w[3:])) for w in window)
    if w1 <= w0:
        raise ValueError("The posting window must end after it starts (same day)")
    days = {int(d) for d in plan.get("days") or range(7) if str(d).isdigit() and 0 <= int(d) <= 6}
    if not days:
        raise ValueError("Pick at least one day to post on")
    per_day = int(_minutes(plan.get("per_day"), "Posts per day", 0, 48))
    interval = (w1 - w0) / per_day if per_day else _minutes(plan.get("interval_minutes"), "The spacing", 5, 60 * 24 * 14)
    cap = int(_minutes(plan.get("max_per_day"), "The daily limit", 0, 200))
    jitter = _minutes(plan.get("jitter_minutes"), "The random offset", 0, 60 * 24)
    jitter = min(jitter, interval * 0.45)        # keeps posts in order and at least ~10% of the spacing apart
    first = max(start, now + 120, *(t + max(interval - jitter, 1) * 60 for t in existing))
    taken: dict = {}
    for t in existing:
        taken[datetime.fromtimestamp(t, tz).date()] = taken.get(datetime.fromtimestamp(t, tz).date(), 0) + 1

    if per_day:        # N evenly spaced slots inside each day's window, each moved by up to ± jitter
        times, day = [], datetime.fromtimestamp(first, tz).date()
        while len(times) < n:
            if day.weekday() in days:
                opening = datetime(day.year, day.month, day.day, w0 // 60, w0 % 60, tzinfo=tz).timestamp()
                for k in range(per_day - taken.get(day, 0)):
                    k += taken.get(day, 0)
                    at = opening + ((k + 0.5) * interval + rng.uniform(-jitter, jitter)) * 60
                    if at >= first and len(times) < n:
                        times.append(at)
            day += timedelta(days=1)
        return times

    def fit(t: float) -> float:
        """t itself when it is inside the window on an allowed day, else the next window opening (+ a little jitter)."""
        moment = datetime.fromtimestamp(t, tz)
        minute = moment.hour * 60 + moment.minute
        if moment.weekday() in days and w0 <= minute < w1:
            return t
        day = moment.date() + timedelta(days=0 if minute < w0 else 1)
        for _ in range(8):
            if day.weekday() in days:
                break
            day += timedelta(days=1)
        opening = datetime(day.year, day.month, day.day, w0 // 60, w0 % 60, tzinfo=tz).timestamp()
        return opening + rng.uniform(0, min(jitter, (w1 - w0) * 30)) * 60

    def next_day(t: float) -> float:
        moment = datetime.fromtimestamp(t, tz)
        return fit(datetime(moment.year, moment.month, moment.day, 23, 59, tzinfo=tz).timestamp() + 60)

    times, base, per_date = [], first, taken
    for i in range(n):
        t = fit(base + (rng.uniform(-jitter, jitter) * 60 if i else rng.uniform(0, jitter) * 60))
        if times and t <= times[-1] + 60:
            t = fit(times[-1] + max(interval - jitter, 1) * 60)
        while cap and per_date.get(datetime.fromtimestamp(t, tz).date(), 0) >= cap:    # that day is full
            t = next_day(t)
        per_date[datetime.fromtimestamp(t, tz).date()] = per_date.get(datetime.fromtimestamp(t, tz).date(), 0) + 1
        times.append(t)
        base = t + interval * 60
    return times


def bulk_posts(plan: dict, files: list[str], now: float, rng=None, existing: list[float] = (), offset: int = 0) -> list[dict]:
    """The posts a bulk plan makes (not saved): each with its time; `file` names the file it waits for.
    Blank posts are allowed (`count` makes that many at least); `offset` = posts the batch already has."""
    import random
    rng = rng or random.SystemRandom()
    blocks = parse_blocks(str(plan.get("text") or ""))
    try:
        wanted = max(0, int(plan.get("count") or 0))
    except (TypeError, ValueError):
        wanted = 0
    count = max(len(blocks), len(files), wanted)
    if not count:
        raise ValueError("Paste some posts, add files, or set how many posts to make")
    if count > MAX_BULK:
        raise ValueError(f"Up to {MAX_BULK} posts at a time")
    wheres = [w.strip()[:200] for w in str(plan.get("where") or "").splitlines() if w.strip()] or [""]
    items = []
    for i in range(count):
        block = blocks[i] if i < len(blocks) else {"title": file_title(str(plan.get("file_title") or ""), files[i], offset + i + 1)
                                                     if i < len(files) and plan.get("file_title") else "", "body": ""}
        items.append({**block, "file": files[i] if i < len(files) else ""})
    if plan.get("shuffle"):
        rng.shuffle(items)
    accounts = [str(a) for a in plan.get("accounts") or [] if str(a)] or [str(plan.get("account") or "")]
    randomly = plan.get("account_mode") == "random"
    tz = zone(str(plan.get("tz") or "")).key
    out = []
    for i, (item, at) in enumerate(zip(items, plan_times(count, {**plan, "tz": tz}, now, rng, existing))):
        moment = datetime.fromtimestamp(at, zone(tz))
        out.append({"account": rng.choice(accounts) if randomly else accounts[(offset + i) % len(accounts)], "where": wheres[(offset + i) % len(wheres)], "title": item["title"], "body": item["body"],
                    "tags": str(plan.get("tags") or "").strip()[:300], "notes": str(plan.get("notes") or "").strip()[:1000],
                    "date": moment.date().isoformat(), "time": moment.strftime("%H:%M"), "repeat": "once", "tz": tz,
                    "at": at, "file": item["file"]})
    return out


# ---------------- batches ----------------
# A batch keeps its settings (accounts, destinations, timing, defaults) so it can be continued with
# more posts or re-timed later; it stays after its posts are all sent (then it shows as complete).

BATCH_KEYS = ("accounts", "account_mode", "where", "tags", "notes", "file_title", "per_day", "interval_minutes",
              "max_per_day", "jitter_minutes", "window_start", "window_end", "days", "tz", "shuffle", "blank")
BLANK_MODES = ("skip", "agent")      # a post still blank at its time: skip it, or let the agent write it


def batch_settings(body: dict) -> dict:
    settings = {k: body.get(k) for k in BATCH_KEYS if k in body}
    settings["blank"] = settings.get("blank") if settings.get("blank") in BLANK_MODES else "skip"
    return settings


def retime(items: list[dict], plan: dict, now: float, rng=None, existing: list[float] = ()) -> None:
    """Give these posts (in their current order) new times from the plan, after `existing`."""
    for post, at in zip(items, plan_times(len(items), plan, now, rng, existing)):
        moment = datetime.fromtimestamp(at, zone(plan.get("tz", "")))
        post.update(date=moment.date().isoformat(), time=moment.strftime("%H:%M"), tz=zone(plan.get("tz", "")).key, repeat="once")
        post["next"] = next_run(post, now)
        post.pop("missed", None)


class BatchStore:
    def __init__(self, data: Path) -> None:
        self.path = data / "batches.json"

    def load(self) -> dict[str, dict]:
        with suppress(OSError, ValueError):
            items = json.loads(self.path.read_text())
            if isinstance(items, dict):
                return items
        return {}

    def save(self, items: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(items, indent=2) + "\n")
        temp.replace(self.path)
