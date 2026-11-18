# SillyTavern (optional service)

[SillyTavern](https://github.com/SillyTavern/SillyTavern) is a chat frontend for characters and
role-play. Rocsy runs the official image as the `sillytavern` service in
`BunnyBytes/docker-compose.yml` (compose profile `sillytavern`) and the gateway serves it behind
the Rocsy login at `/SillyTavern/`, pointed at your local LLMs (llama-swap) and Voice.

Turn it on, update it or turn it off in **Models → Manager → Core Backend → Agent & apps →
SillyTavern** (or tick it in the installer). Turning it on adds `sillytavern` to
`COMPOSE_PROFILES` in `BunnyBytes/.env`, pulls the image, starts it and recreates the gateway.

Your chats, characters, settings and API keys live in this folder (`config/`, `data/`, plus
`plugins/` and `extensions/` for installed add-ons). Those folders are git-ignored and never go
into an installer. Turning SillyTavern off keeps them, so turning it on again picks up where you
left off.

Connect it to Rocsy from SillyTavern's API panel (Rocsy Addons does this for you):

- **LLM:** Chat Completion → Custom (OpenAI-compatible), URL `http://127.0.0.1:8400/v1`, with a
  key from the Llama API page.
- **TTS:** OpenAI Compatible, URL `http://127.0.0.1:8950/Voice/v1/audio/speech`.

## Add-ons

`addons/` holds the Rocsy packages the Core Backend card installs into SillyTavern:

- **Rocsy Addons** (`addons/rocsy-addons/`): Rocsy Voice for TTS (engine, default voice, a voice per
  character), "Rocsy llama-swap" as the default connection, tokens per second on replies, the
  character/lorebook Library, chat names and model load/unload. Its server half goes to `plugins/rocsy`.
- **Rocsy Visual Overhaul** (`addons/rocsy-visual-overhaul/`): a chat-platform layout with a sidebar,
  recent chats, settings sheets and themes; switch it on and off inside SillyTavern.
