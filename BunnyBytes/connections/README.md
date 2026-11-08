# Connections

Connections is the BunnyBytes service at `/Connections/`. It supervises the
existing `telegram_comfyui_output.py` watcher and exposes a login-protected
status page with Start/Pause controls, Telegram configuration, and recent
activity. The token is masked and is never returned to the browser. The
container runs as UID 1000, listens on 127.0.0.1:8800, and uses read-only
mounts for the legacy Telegram configuration, watcher script, and ComfyUI
output folders. Its writable `data/` holds the managed Telegram configuration,
sent-file fingerprints, log, and enabled setting.

The existing host user service and this container must not run concurrently.
On migration, copy `.telegram-comfyui-state.json` and
`.telegram-comfyui-state-h3.json` from the Desktop script folder into
`connections/data/telegram-state.json` and `telegram-state-h3.json` before
starting this service. Both ComfyUI output directories are mounted at their
original absolute paths, so those fingerprints stay valid. Stop and disable
`comfyui-telegram.service` before starting Connections. Never use the watcher's
`--send-existing` option during migration.

Run `docker compose up -d --build connections` from BunnyBytes. The gateway
proxies `/Connections/` after a normal site login. `GET /Connections/api/status`
returns configuration and process status without credentials. Start/Pause
buttons call the service's JSON API through the gateway; requests need the
`X-Connections-Action: 1` header. `/health` is the local container check.

## Websites, accounts and actions

A **website** (`websites/<site>.json` in the accounts folder: name, url, notes) is worked out once. Its guide
(`sites/<site>.json`), upload script (`scripts/<site>_upload.cjs`) and action scripts (`scripts/<site>_actions/`)
are shared by every **account** on it (`<id>.json`: the login, `site` = the website id, username and/or email,
notes). Scripts take `--login <account json>` plus `--cdp URL` (headed) or `--profile DIR` (headless), so they never
hard-code an account. Accounts from before the split are moved under a website (one per host) at startup.

**Scout** takes a note (what the scouting is for) and an account, or none: `$R scout <website> --website` scouts
logged out (sign-up, login, public pages). **Action** also works on a website with no account (`$R act <website>
--website --run <id>`, runs kept in the website file). A created login whose JSON line names a website in Connections
(`"site"`) is added to Accounts by itself (`created/.imported.json` remembers which, so a removed one stays removed);
**Emails** lists every address on an account plus the created ones.

The agent can run `$R emails` to see those addresses and the saved credential file for each, without exposing
passwords. During account creation or later login, it matches the verification recipient, opens an accessible
mailbox with credentials read inside a script, uses the fresh message's link or code, and confirms the target site
accepted it. If no saved login opens that mailbox, the agent asks for the missing access.

**Action** on an account starts a Master Agent session running `$R act <account> --run <id>`: the agent logs in and
does the task. The runs are kept in the account file (`actions[]`); the Action log reads each run's steps back from
its Agent session. Logins an action creates are appended by the agent's script to `created/<account>.jsonl`.

`automation.json` holds the two toggles on the Websites section: `use_scripts` (run saved site scripts) and
`write_scripts` (write and fix them). The agent reads it on every scout, upload and action. **Scripts & notes** shows
every guide and script with its code, set-aside versions and the user's notes.

Scripts can call Rocsy while running through `rocsy-help.cjs`. For a CAPTCHA or a step the script cannot complete,
`help.handoff(page, "specific next step")` shares a screenshot and returns control to the active agent. The agent
completes the step in the visible browser, checks the result, then reruns the script. Scripts inspect the current
page before acting so a resumed run does not repeat a completed submission. The user is asked only when information
such as a 2FA code is unavailable to the agent.

For a visual CAPTCHA, the script passes its element selector to `help.handoff(page, reason, {selector})` so the
handoff image contains just the challenge. The agent reads that image with `vision_analyze` and fills the current
page. It does not reconstruct the challenge from CSS or ASCII art. The script invokes the agent on each fresh
challenge; it never accepts a reusable CAPTCHA answer as an argument. It checks the response for errors before
reporting success or saving a new login.

## Cloudflare tunnels

Connections manages local cloudflared processes without Docker socket access.
The existing `byte` tunnel (your hostname to the local gateway) was imported
from `cloudflared/config.yml` and is now run by Connections. The former
`bunny-cloudflared` container is stopped and assigned the `legacy-tunnel`
Compose profile for rollback. Telegram credentials are editable from the
Connections page. The old host-side `.telegram-comfyui.json` is imported once;
the managed copy is stored owner-only under `connections/data/`.

The UI shows routes and live edge connections, requests, and errors. It can
start, stop, restart, and edit routes for managed tunnels. Ingress rules are
validated with `cloudflared tunnel ingress validate` before changes are
applied. Restarting or stopping a tunnel briefly disconnects public access;
local access through port 8080 remains available.

To add a tunnel, import its credential JSON and hostname routes, or use a
Cloudflare API token plus account ID and zone ID to create a locally managed
tunnel and proxied CNAME records. The API token is used for that request only
and is not saved. For imported tunnels, create proxied CNAME records pointing
to `<tunnel-id>.cfargotunnel.com` in Cloudflare DNS. If you add a hostname to
an existing tunnel, update its DNS record separately.

## Scheduled posts

The Scheduled posts section (or "Schedule a post" on a website card) schedules a post for a website
account, such as a Reddit account posting to a subreddit. A post has a destination ("where"), a title,
text, optional tags or flair, an optional image or video (up to 48 MB), and notes for the agent. It
runs once, daily or weekly, in the browser's timezone. Posts are stored in `data/posts.json`; files
are stored in the accounts folder under `media/`, so Hermes can read them at
`/opt/data/rocsy-accounts/media/`.

When a post is due, Connections starts a Master Agent session that runs `$R upload` with the
site's saved script. If the site has no script yet, the session scouts the site first. The session is
pre-cleared for Uploads, so Ask first doesn't hold it up. Without a file, it is a text post. If the
Agent can't be reached, Connections retries until the post is an hour late. A post missed by more
than an hour (for example while Connections was down) is marked Missed and isn't sent; Post now
sends it.

**Queue many** creates a batch of one-off posts. Paste posts as blocks separated by `---` lines,
where the first line of each block is the title and the rest is the text. You can also add files:
they pair with the blocks in order, and extra files become posts titled from the "Title for
file-only posts" template (`{name}`, `{n}`). Several destinations take turns. You set the
timing with a start, either N posts per day (evenly spaced slots across the daily window) or a fixed
gap with an optional daily limit, a random offset of ± minutes, the window and the weekdays. You can
also shuffle the order. Preview shows the exact times, and the page's seed makes the saved times
match the preview; Re-roll picks new ones. Files upload one at a time after the batch is created
(`POST /api/posts/{id}/media`, which keeps each upload under the gateway's limit). A post whose
file fails to upload shows "File missing" and isn't scheduled until a file is attached. Batches can
be paused, resumed or deleted together (`POST /api/posts/batch/{id}/{pause|resume|delete}`).

**Several accounts.** Both dialogs let you tick more than one website account. Without **Random
account**, the accounts take turns: a single repeating post alternates on each run, and a batch
alternates post by post. With it, a random ticked account is used: a single post picks one each
time it is sent, while a batch picks one per post when it is planned, so the preview shows the
choice. Accounts that have since been removed are skipped.

**Let the agent decide.** Any part of Where, Title, Text or Tags written as `{? ...}` is filled in
by the agent when the post is sent. `{?}` alone is its call, `{? r/art | r/drawing}` means pick one
of those, and `{? a fitting art subreddit}` means follow that instruction. It bases the choice on
the post and its file, checks that a chosen subreddit exists and what its rules are, says what it
picked, and keeps everything outside the `{? }` parts word for word. The "🎲 Agent decides" buttons
insert `{? }` at the cursor.

**Batches** are saved in `data/batches.json` with their settings, and they stay after every post
is sent. They show as Complete and can be continued. Open a batch to see all its posts in a
gallery (click one to edit it), add posts, change its settings, pause or resume it, or delete it.
"Add posts" continues after the batch's last post by default, keeping the account and destination
turns and each day's count. Saving settings can re-time the posts not sent yet, and can apply new
accounts, destinations, tags and notes to them (only where a post still has the old batch value or
none). Pause and resume affect only posts not sent yet.

**Blank posts.** A batch can contain blank posts: "Number of posts" fills up with empty ones,
which you then fill in from the gallery. Tags or flair can also be left blank, in which case the
agent picks them only if the site requires them. A post with a file but no title gets a title
written by the agent. For a post that is still completely blank when its time comes, the batch
setting decides: skip it (shown as "Skipped (blank)"), or let the agent write it from the account's
purpose.
