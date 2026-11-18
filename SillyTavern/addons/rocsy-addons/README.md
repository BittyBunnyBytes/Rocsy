# Rocsy Addons for SillyTavern

Rocsy's services wired into SillyTavern. Open it from **Extensions → Rocsy Addons**, the Rocsy Visual
Overhaul sidebar (**Settings → Rocsy Addons**), or `/rocsy` (`/rocsy voice`, `/rocsy connection`,
`/rocsy library`).

- **Voice:** SillyTavern's TTS through Rocsy Voice. Pick the engine (Chatterbox Turbo, Qwen3-TTS,
  IndexTTS2, Fish Audio …), the default voice and a voice for each character in the chat, with a test
  button. New characters get the default voice automatically.
- **Connection:** creates the "Rocsy llama-swap" connection profile (Custom OpenAI-compatible,
  `http://127.0.0.1:8400/v1`) and uses it as the default; pick the model there. Rocsy's llama-swap key
  goes into SillyTavern's secret store on the server and is never sent to the browser.
- **Chats:** chat names the Master Agent way. After a set number of messages (default 2 = right
  after the first reply; the greeting doesn't count) the chat gets a short, specific 2-6 word name from
  the chat's own model (no model swap; another llama-swap model can be picked). It tries once more at
  the next message if naming failed. Names show in the Rocsy sidebar and header; rename there (pencil,
  F2 or double-click), a name you give is kept, an empty one hands naming back. The chat file keeps
  its name (renaming the file would reload the chat and cut off TTS).
- **Tokens per second** on each reply (the chip by the speaker's name; switch it off on the
  Connection tab).
- **Library:** browse character cards and import them in one click.
  - **Lorebooks:** the Characters | Lorebooks switch browses Chub and Bot Booru lorebooks (search, sort,
    details with every entry, bookmarks); Import puts one into SillyTavern's World Info. Chub lorebook and
    Bot Booru `/lorebook/N` links in the link box import too. Chub presets and extensions are Chub-only formats,
    so they aren't offered.
  - **Chub** (chub.ai, the default, ★) and **Bot Booru:** search, sort, NSFW on/off, a card grid with Import /
    Open chat. Searches run through the server half (no CORS trouble). Bot Booru shows NSFW cards
    only to signed-in people: paste your Bot Booru token under *Bot Booru sign-in* (it's kept in
    SillyTavern's secret store). Chub cards are downloaded by SillyTavern's own importer. Chub
    blocks some regions by IP; the tab then says so.
  - **datacat.run** (JanitorAI's characters with their full cards; pasted janitorai.com links open
    there too, since SillyTavern's own JanitorAI import goes through JannyAI, behind a Cloudflare bot check): the site itself, embedded; its *ST Chat* button sends the card it builds
    through datacat's SillyTavern bridge, and an already imported card is just opened.
  - **Card details:** clicking a card opens it in the Library with all its data (creator's notes,
    description, personality, scenario, first message, alternate greetings, examples, prompts,
    lorebook entries, stats), with Import and an "Open on <site>" link.
  - **Each site's own search:** Chub (text, tags to include / exclude, its 12 sorts + ascending,
    token range, age, has lorebook / alternate greetings / examples / gallery / expressions);
    Bot Booru (text, tags with `-tag` exclusion and suggestions, its sorts with a day / week / month /
    all window, curated order, token range). Clicking a tag on a card filters by it. Filters are remembered per site.
  - **Settings (gear, next to Characters | Lorebooks):** a global **blacklist** of tags, keywords
    (whole words in name, tagline, tags) and creators, edited as chips; matching cards and lorebooks are
    hidden on every site (not inside datacat.run, the site's own page), with an "N hidden — Show them" note.
    Blacklisted one-word tags are also sent to Chub (`excludetopics`) and Bot Booru (`-tag`) so pages stay
    full (switchable). Options: hide blacklisted bookmarks too, NSFW on/off, blur NSFW pictures, hide what's
    already imported, pages or endless list. **New users start with a default blacklist** (minors-related
    tags/keywords such as loli, shota, underage, plus NSFL/gore/guro/scat/snuff/necrophilia/bestiality);
    *Add default blacklist* merges it back in; *Clear all* empties every list at once (no popup; the toast undoes it) and each list has its own *Clear*, so you can start your own. Stored in `settings.library.blacklist`.
  - **Search by creator:** a card's creator ("by …" on the result tile, in the details header and in its
    About box) is a link that searches that site for everything they made: Chub `username`, Bot Booru cards
    by the Writer tag, Bot Booru lorebooks by uploader (the server reads up to 2,000 and filters). A
    "Showing cards by X ×" bar shows the filter; × or Back returns to the search and card you came from.
  - **Bookmarks:** a bookmark button on every Chub / Bot Booru card (and in its details); the
    **Bookmarks** button lists them all across sites, with search, a site filter and sorting, and
    opens, imports or un-bookmarks them. Each keeps a copy of the card's name, picture and tags.
  - **Card text** is shown formatted (markdown and creators' HTML, cleaned: no scripts, styles,
    frames or forms; links and pictures only over http(s), pictures lazy-loaded without a referrer);
    a Raw text toggle shows it as written. The details screen has a hero banner, the card text, and
    a side column with stats, about and comments (Bot Booru; a link for Chub).
  - **Accounts:** an Accounts button (and a panel on each source) with the site's own **Sign in** /
    **Create account** pages in a new tab (Chub: its home page, since chub.ai/login crashes when opened directly). Chub and Bot Booru accounts are then connected with the
    **Connect to Rocsy** bookmark (drag it to the bookmarks bar once, click it on the signed-in site:
    it posts the site's sign-in token to this Rocsy window only, and Rocsy accepts it only from that
    site's address; if the tab lost its link to Rocsy it opens Rocsy, which asks before saving), or by
    pasting the token. The server checks it with the site and keeps it in SillyTavern's secrets; a
    green dot marks connected sites, and a sign-in the site stops accepting is reported. Rocsy never
    asks for a site password. datacat signs in inside its own embedded page.
  - A link box imports any card link SillyTavern understands (Bot Booru, Chub, JanitorAI,
    RisuRealm, Pygmalion …). Imported cards are remembered, so their button becomes *Open chat*.

It has a server half (`server/` → SillyTavern's `plugins/rocsy`, routes at `/api/plugins/rocsy`), so
installing it from Rocsy (**Core Backend → SillyTavern → Rocsy add-ons**) turns on SillyTavern's server
plugins in its `compose.yml` and restarts it.

## Strict prompt post-processing

The "Rocsy llama-swap" profile uses Prompt Post-Processing **Strict** (user message first, alternating roles):
local chat templates (Qwen, Gemma) otherwise fail on "Continue" when the chat has only the greeting ("No user query
found"). New profiles get it when they're created; existing ones once (`settings.strictPostProcessing`), unless the
user had picked a different non-merge option. `RocsyAddons.mountConnection(node)` draws the Connection tab into a
page (the Rocsy layout's Model & connection page).

## Rocsy Image and character voices

`rimage.js` + `rimage.css` (client, `globalThis.RocsyImage`) and `server/rimage.mjs` + `server/rvoice.mjs`.

- **Image tab / SillyTavern's Image Generation**: the server plugin runs an A1111-compatible API on
  `127.0.0.1:8146` (`/sdapi/v1/options|sd-models|samplers|txt2img|img2img|progress|interrupt`, env
  `ROCSY_SDAPI_PORT`) that renders through Rocsy Image (`ROCSY_IMAGE_URL`, default `http://127.0.0.1:8300/Image`).
  SillyTavern's source "Stable Diffusion WebUI" pointed at it makes `/sd`, the wand menu and prompt prefixes use
  Rocsy's models and GPU queue. Set once automatically when SillyTavern still pointed at the dead Extras API.
  The bridge's model is kept in `plugins/rocsy/rimage-state.json`. Renders show in Image → Generations.
- **Character editor → Picture & expressions**: a card with SillyTavern's stock picture is flagged; Rocsy Image
  paints 1-4 candidates from a prompt the chat model writes from the card (`POST /image/avatar`, a task polled at
  `/image/task/:id`). Expressions: Qwen Image 2.1 edits the picture once per SillyTavern expression label
  (`POST /image/expressions`), one render at a time, written to `characters/<name>/<label>.png`; runs on the
  server (tab can close), stoppable, "missing only" or "redo all". **Master reference** (`makeMaster()`): the
  first step makes one neutral, front-facing, plain-background picture and every expression edits it (not the card
  picture), so a set stays consistent. Source option "Base the master on the character's picture": on = Qwen edit
  of the card picture; off (or stock picture) = the chat model writes a prompt from the card (`MASTER_SYSTEM`, adult /
  clothed / non-explicit, `looksMinor` checked) and Qwen paints it. Saved outside the sprite folder at
  `<user root>/rocsy-image/masters/<avatar>` + `.json` (source, prompt, card-picture mtime); reused until "Redo
  all", "New master" (`DELETE /image/master`), a source change or a new card picture. `GET /image/master` serves it;
  `/image/character` reports it. A failed or cancelled master stops the set (no fallback to the card picture).
  With no master yet, the card offers **Create master** (`POST /image/master/create`, task kind `master`, only the
  master step) and **Upload…** (`POST /image/master` {avatar, image: data URL}, decoded with Jimp and saved as PNG,
  source `upload`). An uploaded master always counts as fresh and "Redo all" keeps it; only "New master" removes it. Offers to switch SillyTavern's expression
  classifier to Local when it is off.
- **Character editor → Voice**: the voice picker plus "Create a voice": *Describe* (chat model writes a voice
  description + a line from the card → Qwen3-TTS VoiceDesign) or *From a recording* (15 s, music removed,
  Whisper transcript). Saved as a Rocsy Voice (category SillyTavern, card picture as portrait) and assigned via
  `RocsyAddons.setCharacterVoice`.
- Never draws minors: cards/prompts stating an age under 18 or loli/shota/underage terms are refused.

## Expression classifiers

Once per install (`expressionsOnce()` in rimage.js autoSetup, flag `expressionsSetup`): when the classifier was off or on
SillyTavern's own local model, it switches to Rocsy ONNX `Cohee/roberta-base-go_emotions-onnx` (same 28 labels, more
accurate, CPU). Models are cached in `data/_cache`, which must be owned by the container user (uid 1000).

`rclassify.js` (client) + `server/rclassify.mjs`. Rocsy Addons → Image (and Settings → Expressions & images →
Classifier) picks what decides each reply's expression sprite. SillyTavern's own options (its config.yaml local model,
the chat connection, WebLLM, off) are there, plus Rocsy's, which keep SillyTavern on "Local" and reroute its
`/api/extra/classify(/labels)` calls (a page fetch wrapper) to `/api/plugins/rocsy/classify(/labels)`:

- **ONNX model**: a list of checked transformers.js emotion models (GoEmotions 28, Ekman 7, 6/4-label ones) or any
  `owner/model`; runs on the CPU in the plugin's own pipeline (not SillyTavern's shared one, which a failed load
  would leave disposed). Models in the newer ONNX IR 10 format don't load in SillyTavern's onnxruntime.
- **llama-swap model**: one label, held to the list by a GBNF grammar, thinking off.
- **Decision model** (Laya, Jev-Style, OpenJev, Lev, Kev…): llama.cpp's `/v1/systemone` through llama-swap's
  `/upstream/<model>/` passthrough; one `choice` question, a probability per label. Needs a llama.cpp build with the
  API (merged 2026-10-02) and the model in llama-swap.
- **Pseudo emotion model**: your own label set (label + what it means, and for ONNX the model labels it is made from,
  e.g. smug = pride + amusement) on any of the three engines; presets Simple moods / Ekman 7 / Roleplay moods /
  GoEmotions. Settings: `extension_settings['rocsy-addons'].classifier`.

The character editor's Expressions card makes one sprite per label of the active classifier, using each label's
meaning in the Qwen edit prompt.

## Replies that survive a dropped connection

`rgen.js` (client) + `server/rgen.mjs`. A phone that locks or switches apps mid-reply freezes the tab and drops its
connection; SillyTavern then loses the reply (it saves only complete replies). Now the page's streamed
`POST /api/backends/chat-completions/generate` is wrapped: `POST /gen/start` makes that same call on the server
(forwarding the page's cookie + CSRF token) and keeps the stream; the page reads `GET /gen/:id/stream?from=<byte>` and
hands SillyTavern an ordinary streaming Response. A broken read waits until the tab is visible/online and resumes at
the byte it had (`/gen/:id/state` tells finished from dropped; `/gen/:id/ack` when complete). Stop → `/gen/:id/stop`.
SillyTavern aborts streaming on `beforeunload`; aborts while the page unloads are ignored (stop deferred one tick,
capture-phase flag), so a reload or discarded tab leaves the reply running. On CHAT_CHANGED, `/gen/recover?chatId=`
offers a finished reply the page never got: appended after the user's message, or filling in the partial reply
SillyTavern saved on the way out. Jobs live in memory for an hour (lost on restart). Non-streamed calls are untouched.

## Reply notifications, keep awake, chats helpers (2026-11-15)

- `rnotify.js` + `server/rpush.mjs`: **"Reply ready" notifications** via Web Push.
  - The page registers the service worker served at `api/plugins/rocsy/sw.js` (`Service-Worker-Allowed: /`,
    scope = the SillyTavern page) and subscribes with a per-user VAPID key. Keys and subscriptions are in
    `<user root>/rocsy-push.json`.
  - When a reply job (rgen) finishes and the page hasn't acked it within 2.5 s (phone locked or tab frozen), the
    server sends an **empty** push. The worker then asks `GET /push/latest?endpoint=` what to show: "<name>
    replied", plus the start of the reply only if that device turned previews on.
  - If the page is alive in the background, it shows the notification itself and marks the tab title.
  - Routes: `/push/key`, `/push/state`, `/push/subscribe`, `/push/unsubscribe`, `/push/latest`, `/push/test`.
  - Settings are on the overhaul's Interface page. Turning notifications on needs a tap and the https address.
- **Keep awake**: a screen wake lock while a reply is being written, and `awakeAfter` seconds after it for the
  voice. Settings live in `extension_settings['rocsy-addons'].phone`.
- **Missing voice warning**: if TTS is on and someone in the chat has no voice-map entry, a toast offers the
  default voice.
- `server/rchats.mjs` provides the Chats page routes:
  - `/chats/all` lists every chat with counts and its main_chat.
  - `/chats/stats?avatar=` (or `group=1&chats=`) returns stats counted from the files.
  - `/chats/backups` lists ST's chat backups, matched to the owner via `getBackupKey` and to the chat by integrity
    id.
  - `POST /chats/backups/restore` makes a new chat with a fresh integrity id.
  - `POST /chats/duplicate` copies a chat.
- `manifest.webmanifest` and `app-icon.png?size=180|192|512` are for "Add to Home screen".
