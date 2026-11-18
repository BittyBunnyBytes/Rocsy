# Rocsy Visual Overhaul for SillyTavern

A chat-platform layout over SillyTavern's own UI, switchable at any time:

- **Sidebar:** Lorebooks, Characters, search, recent chats grouped by day (Today, Yesterday, …), and
  every SillyTavern settings panel under one **Settings** group with plain names (Model & connection,
  Response settings, Prompt formatting, Lorebooks, Persona, Extensions, Backgrounds, Interface).
- **Header:** the current character and chat (click for its chat history), plus history, new chat,
  character details and connection buttons.
- **Chat:** a centred column, your turns in bubbles, the character's as plain text, message numbers,
  timers and token counts hidden (switchable), message buttons on hover, a rounded composer and a
  jump-to-latest button.
- **Settings sheets:** every SillyTavern panel opens as the same sheet on the right; Esc or a click
  outside closes it.
- **Themes:** Rocsy Dark, Midnight, Light, Follow system, or your own SillyTavern theme.
- **Lorebooks page (all in-site, SillyTavern's World Info panel is never shown):** every book with entry
  count, source and users, an "All chats" switch, and the activation settings for every lorebook (also
  Settings → World Info). A book's page: where it's used (all chats / this chat / characters, link and unlink),
  every entry with all its fields (keywords, filter + logic, content with token count, strategy, position,
  depth/role/outlet, order, trigger %, scan depth, case/whole words, groups, sticky/cooldown/delay, recursion,
  extra match sources, generation types, character/tag filter), add / duplicate / move / delete entries, and
  rename / duplicate / export / delete the book (through SillyTavern's own hidden buttons and popups).
  New chat is in the header and on each character row.
- **Shortcuts:** Ctrl+Shift+O new chat · Ctrl+K search chats · Ctrl+B sidebar · Esc closes a panel.

Switch it off from the sidebar (the columns icon next to the theme), from **Extensions → Rocsy Visual
Overhaul**, or with `/rocsy-ui off`. In the classic layout, the wand icon in the top bar (or
`/rocsy-ui on`) brings it back. Off, it changes nothing: every rule is scoped to `body.rvo`.

Install or remove it in Rocsy: **Models → Manager → Core Backend → SillyTavern → Rocsy add-ons**.

**Characters page** (Rocsy layout): a gallery with search, sorting and an AO3-style tag sidebar (tags grouped as Ratings, Archive Warnings, Categories, Fandoms, Characters, Relationships, Additional Tags, Content, Traits, SillyTavern Features and Other Tags, each with a count for the characters shown; tick to include, the ban button to exclude, Find a tag, Show all; a Filters button opens it on narrow screens). Tags no character or group uses any more are deleted automatically (when the gallery opens, after tag edits, when a character is deleted). Each character has a page
(description, tags, chats, voice) and an **editor page** of its own: sections with a jump list (Basics, Character,
Greetings, Examples, Prompts, Lorebook & more, Creator), every field autosaved (Ctrl+S saves at once), token counts
per field and in total, {{char}} / {{user}} / <START> insert buttons, alternate greetings as a list (add, reorder,
delete, make first), tags (yours, plus one-click adding of the card's own), linked lorebook, character's note
placement, talkativeness, favorite, picture change, Revert (undo since opening), Duplicate, Export PNG/JSON, and
SillyTavern's own editor / Delete in the ⋯ menu. Any character can be edited, not only the open one.


## Autotag

Characters → **Autotag** (next to Create / Import / Library) is a page where a llama-swap model reads each character
card and suggests tags: AO3 style (rating, warnings, category, fandom, relationships, characters, tropes),
Danbooru-style basics (gender, age when it matters, species, setting; no looks or clothing tags; Qwen3.8 models also check the picture) and
a Content group (SFW or NSFW, and Dead Dove: Do Not Eat when it applies), and SillyTavern feature tags worked out from the card itself (`ST: Lorebook`, `ST: Alternate Greetings`,
`ST: Expressions`, `ST: Regex Scripts`, `ST: HTML Formatting`, `ST: Macros`, …). Click a tag to leave it out,
then Apply: they become ordinary SillyTavern tags (new ones coloured by kind). Autotag remembers what it added,
so a re-run replaces its own tags and Remove takes them off again; tags you added yourself stay. **Strip the card first** (Applying) makes the new tags fully replace the old: every tag the character had (yours, the ones written in the card file and earlier Autotag ones) is removed; Remove puts them back. The model call
is Rocsy Addons' server route `POST /api/plugins/rocsy/autotag` (needs the Rocsy Addons add-on).

## Settings pages (in the site)

`settings-pages.js` turns SillyTavern's settings into Rocsy pages (sidebar → Settings, plus a **Groups** item under
Characters). None of them opens SillyTavern's own drawers:

- **Model & connection**: connection status + Connect, the Rocsy llama-swap section (Rocsy Addons' Connection tab,
  mounted in place), connection profiles (save / save as / rename / reapply / delete), API type, source and every
  field SillyTavern shows for it, API key (write-only), Prompt post-processing with a one-click **Use Strict** fix.
- **Response settings**: preset (update / save as / rename / import / export / delete), length, sampling, thinking,
  behaviour, utility prompts, and the **prompt list** (Prompt Manager: on/off, order, edit name/role/position/text,
  add, delete). A warning offers 8192 tokens when a thinking model has a short response length.
- **Persona**: gallery, one persona's page (picture, name, title, description + position/depth/role, persona
  lorebook, connections to characters and groups, default for new chats, lock to this chat, delete), create.
- **Groups**: gallery, new group (members picker, reply order, card handling, options), one group's page (members:
  mute / reorder / remove / add, settings, chats, new chat, delete).
- **Quick Replies**, **Regex** (Global / character / preset scripts, full editor, tester), **Memory** (Summarize +
  Vector Storage) and **Expressions & images** (Expressions + Image generation).

Plain settings are *mirrored*: the Rocsy control writes through SillyTavern's own hidden input and fires its events,
so SillyTavern saves them exactly as before; fields SillyTavern hides for the current API/source are left out.
Characters → **Create** is an in-site form (name, picture, description, first message) that opens the editor; Delete
uses an in-site confirmation. Still SillyTavern's panels: Backgrounds, Interface.

- **Prompt formatting**: system prompt, reasoning formatting, stop strings / tokenizer / start reply with, context and
  instruct templates (each with its preset buttons), import / export all. With Chat Completion a note says which parts
  apply.
- **Extensions**: installed list (on/off with a reload banner, update and delete for third-party ones; Rocsy's own are
  managed by Rocsy), Install extension (SillyTavern's small URL popup), and every extension's settings: mirrored, or
  with **Original controls** the extension's own panel is borrowed into the page and put back on leaving
  (`SP.leave()`). Extensions with a Rocsy page (Regex, Quick Replies, Summarize, Vectors, Expressions, Image
  generation, Connection profiles, Rocsy Addons) link there.

## Visual pass: Personas, Lorebooks, Extensions (2026-11-14)

Shared pieces: `pageBanner()` (icon, title, text, stat tiles) and `filterChips()` (index.js, passed to the settings
pages), lorebook colours from the name (`nameHue`).

- **Personas**: banner with counts, "You are chatting as" spotlight (default / lock-to-chat switches, connected
  faces), filter chips (All, Default, Connected, No description), sort, portrait tiles with In use / default / chat
  badges. A persona's page uses the character-page layout: hero (picture, name and title edited in place), main
  column (description, placement, connections), side cards (Use switches, token/word/connection stats, About).
- **Lorebooks**: banner (books, every chat, with characters, entries), filter chips, books grouped (In every chat,
  In this chat, With characters, Not in use) as cards with a colour tile, pills and character faces. A book's page
  shows entry stats (on, always on, by keywords, vectorized) beside "Where it's used".
- **Extensions**: banner, filter chips (On, Off, With settings, Installed by you, Built-in), extensions grouped by
  category; one card per extension with its settings folded inside (panels matched to extensions by title), and
  "More settings" for panels without an owner.

## Expression gallery (2026-11-14)

`RocsyVisualOverhaul.showExpressionGallery(ch | avatar, label)`: full-screen viewer of every sprite in the character's
expression folder (`spriteFolder()`: the name, or SillyTavern's expression override path), from `/api/sprites/get`.
Arrows / ←→ / swipe step through them, a filmstrip jumps to any, Esc or Back closes (it's a history overlay). Opened
from the character page's **Expressions** side card and from the editor's Rocsy Image "Picture & expressions" grid
(rimage.js: one delegated click on finished `.rim-sprite.has` cells).

**Linked lorebooks on the character page** (`characterLore()`): a **Lorebook** side card lists the character's main
book (`data.extensions.world`) and extra books (`world_info.charLore`), with entry counts; clicking one opens it on the
Lorebooks page. Each book also gets a folded section in the main column showing its entries (title, keys, text,
Always on / Off tags, search box when there are more than 4). The card's embedded lorebook section is hidden once
it has been imported under the same name, so it isn't listed twice.

**Expression sprite on phones** (`startMobileExpression()`): SillyTavern hides its sprite at ≤1000px. Rocsy mirrors
`#expression-image` (MutationObserver on `#expression-wrapper`) into `#rvo-mexpr`, a portrait in the chat's top-right
corner with the expression name. Tap = big / small, × folds it into a mask button (tap to bring it back); the state is
`settings.mobileExpression` (small / big / mini). Hidden on pages, with the phone sidebar open, without a sprite, and on
wide screens.
Display mode `settings.mobileExpressionMode` (Settings → Expressions & images → **On phones**): `corner` (the portrait),
`background` (`#rvo-mexpr-bg` inside `#sheld`, behind `#chat`/`#form_sheld`, opacity `settings.mobileExpressionDim` %,
"Background strength" slider; the big portrait also has a "use as background" button) or `off`.

## Chats, chat menu and more pages (2026-11-15)

- **Chats page** (`chats-page.js`; sidebar "Chats", header history button, Ctrl+Shift+H). Every chat of every
  character and group from Rocsy Addons' `/chats/all`. You can search, sort, filter by character, pin, archive
  (archived chats are hidden from the sidebar), rename, duplicate, export as .jsonl or .txt, and delete. Branches and
  checkpoints are listed under the chat they came from (`chat_metadata.main_chat`).
  - **This chat** tab: the open chat's checkpoints and branches by message, with jump-to-message, back to the
    parent chat, save a checkpoint, and branch from the last message.
  - **Backups** tab: SillyTavern's automatic chat backups, matched to their character and chat by integrity id. You
    can look inside, restore as a new chat (nothing is overwritten), download, or delete.
  - Pins and archive are stored in `settings.chatPins` and `settings.chatArchive`. Sidebar chat rows get the same
    ⋯ menu.
- **Chat stats** card on the character page: messages, words, swipes, writing time, first and last message, and
  the longest chat. They are counted from the chat files (`/chats/stats`).
- **Chat menu** (`chat-tools.js`): in the Rocsy layout the ☰ by the chat box opens Rocsy's menu. It has new chat,
  chats, regenerate, continue, impersonate, delete messages, Author's Note, checkpoint, parent chat, convert to
  group, token probabilities, shortcuts and close. Each item clicks SillyTavern's own `#option_*` item. The menu is
  positioned with `top`, because on phones ST's CSS makes `<body>` the containing block.
- **Drafts**: unsent text is kept per chat in localStorage (`rocsy-drafts`, the newest 60) and restored when you
  switch chats. It is cleared on send.
- **Shortcuts**: Ctrl+/ or ? lists ST's and Rocsy's shortcuts. Ctrl+Shift+A opens Author's Note.
- **Home screen**: a `<link rel=manifest crossorigin=use-credentials>` points to Rocsy Addons'
  `manifest.webmanifest`, which uses a generated sun + bird icon.
- **Settings pages** (`settings-pages.js`):
  - **Backgrounds** writes `background_settings` or the chat's `custom_background`, the same as ST's picker. It
    can upload, rename, delete, auto-pick, and set the fit.
  - **Interface** has the Rocsy layout options, Phone & notifications (Rocsy Addons' `notify`), and ST's User
    Settings grouped by its own h4 headings. ST's colour pickers and Custom CSS are borrowed into the page.
  - **Author's Note** covers the chat note, character note and default note, mirroring ST's `#extension_floating_*`
    inputs.
  - No settings entry opens an ST drawer any more.
- **Expressions on wide screens**: `settings.desktopExpression` adds `body.rvo-mexpr-on` on any width, so the
  phone portrait or background CSS applies and ST's own sprite is hidden. Without it, the class is set only at
  ≤1000px.
- **Phones**: bigger message buttons and swipe arrows. ST's own swipe gestures on the last message still work.
