// Mod test runs: build → static checks → a bounded HEADLESS run of the game with the mod, compared
// with a baseline run of the unmodified game (cached per game version), so only errors the mod
// adds count. Nothing needs the GPU, so a run can be started from any device. With screenshots
// on (shots.mjs) the game runs on a virtual display instead of headless and what it shows is saved.
//   rea-mods/<id>/runs/<run>/run.json   verdict, phases, new errors, killed processes, agent session
//   rea-mods/<id>/runs/<run>/*.log      build, check, run, baseline, game (engine log) …
//   rea-mods/<id>/runs/<run>/shots/     screenshots (base-* = the unmodified game), listed in run.json shots[]
// When a run fails, every leftover game process is killed and, unless the agent itself started the
// run, the Master Agent is asked (through the outbox) to find and fix the cause, with a /goal that
// keeps it going until a test run passes.
import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { Writable } from "node:stream";
import { MODS, installedFiles, modTools, claimJob, releaseJob, headlessSupport, originalFile, pckVersion, engineBrief, relevantFiles, relevantBrief } from "./mods.mjs";
import { queueAgent, modelFor, onDelivered } from "./outbox.mjs";
import { listTools, toolsBrief } from "./tools.mjs";
import { addSession, kbKey } from "./kb.mjs";
import { startDisplay, shooter, xvfbGrab, plan, schedule, xInput, DISPLAY_ENV, WIDTH, HEIGHT } from "./shots.mjs";

const { readProject, saveProject, installDir, run: runCmd, findFiles, adapter: adapterOf } = modTools;
const TEST = process.env.REA_TEST_DIR || "/home/node/rea-test";
const ENGINES = path.join(TEST, "engines");
const BASELINES = path.join(TEST, "baselines");
const GODOTSTEAM_URL = "https://codeberg.org/godotsteam/godotsteam/releases/download/v3.21/linux64-g352-s158-gs321.zip";
const GODOTSTEAM_SHA256 = "19d627a4a31057eca6848abcd03b722e020bb60d1bf110f0e61a7d94490c688b";
const MAX_LOG = 2 * 1024 * 1024;     // per log file: the page's viewer reads up to 2 MB
const LOGS = new Set(["decompile.log", "build.log", "loader.log"]);
const active = new Map();             // mod id -> run (in memory while it runs)
const exists = (p) => fs.existsSync(p);
const now = () => Date.now();

// ------------------------------------------------------------------------------ helpers --
// A real Writable (mods.mjs pipes child output into logs) that stops writing at MAX_LOG.
function cappedLog(file) {
  const out = fs.createWriteStream(file);
  let size = 0;
  const log = new Writable({
    write(chunk, _encoding, callback) {
      if (size >= MAX_LOG) return callback();
      const text = String(chunk);
      size += text.length;
      out.write(size >= MAX_LOG ? text.slice(0, text.length - (size - MAX_LOG)) + "\n[log truncated at 2 MB]\n" : text, callback);
    },
  });
  log.on("error", () => {});
  log.close = () => new Promise((resolve) => log.end(() => out.end(resolve)));
  return log;
}

// Start a process in its own process group so the whole tree can be killed; bounded by timeoutMs.
function launch(cmd, args, { cwd, env = {}, log, timeoutMs, onLine }) {
  return new Promise((resolve) => {
    const started = now();
    log.write(`$ ${[cmd, ...args].join(" ")}\n`);
    const child = spawn("nice", ["-n", "10", cmd, ...args], { cwd, env: { ...process.env, ...env }, detached: true, stdio: ["ignore", "pipe", "pipe"] });
    let timedOut = false, buffer = "";
    const feed = (chunk) => {
      log.write(chunk);
      if (!onLine) return;
      buffer += chunk;
      let i;
      while ((i = buffer.indexOf("\n")) >= 0) { onLine(buffer.slice(0, i)); buffer = buffer.slice(i + 1); }
    };
    child.stdout.on("data", feed);
    child.stderr.on("data", feed);
    const timer = setTimeout(() => { timedOut = true; killGroup(child.pid); }, timeoutMs);
    child.on("error", (error) => { clearTimeout(timer); log.write(`\n${error.message}\n`); resolve({ code: -1, timedOut, ms: now() - started, pid: child.pid }); });
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      killGroup(child.pid); // children it left behind
      resolve({ code, signal, timedOut, ms: now() - started, pid: child.pid });
    });
  });
}

function killGroup(pid) {
  if (!pid) return;
  try { process.kill(-pid, "SIGTERM"); } catch {}
  setTimeout(() => { try { process.kill(-pid, "SIGKILL"); } catch {} }, 3000).unref();
}

// Kill every process in this container that belongs to a test run: started from, running out
// of, or pointing at one of `roots` (the stage, the run's folder, the mod's install/). Ghidra or
// anything else looking at the original game folder is left alone.
async function killLeftovers(roots) {
  const killed = [];
  for (const pid of await fsp.readdir("/proc").catch(() => [])) {
    if (!/^\d+$/.test(pid) || Number(pid) === process.pid) continue;
    const [cmdline, cwd, exe] = await Promise.all([
      fsp.readFile(`/proc/${pid}/cmdline`, "utf8").catch(() => ""), fsp.readlink(`/proc/${pid}/cwd`).catch(() => ""),
      fsp.readlink(`/proc/${pid}/exe`).catch(() => "")]);
    const args = cmdline.split("\0");
    if (!roots.some((r) => cwd.startsWith(r) || exe.startsWith(r) || args.some((a) => a.includes(r)))) continue;
    try { process.kill(Number(pid), "SIGKILL"); killed.push({ pid: Number(pid), cmd: args.join(" ").slice(0, 200) }); } catch {}
  }
  return killed;
}

// A writable copy of the game: directories and big files are symlinks into the read-only game
// folder, small top-level files (the launcher executable) and everything from `overlay` are real.
// copyMatch: files that must be real copies wherever they are (the engine writes next to them).
async function stageGame(gameDir, overlay, stage, { skip = () => false, copyMatch = null } = {}) {
  await fsp.rm(stage, { recursive: true, force: true });
  await fsp.mkdir(stage, { recursive: true });
  // Installed mod builds are not the game: created files stay out, replaced ones come from the backup.
  const installed = await installedFiles(gameDir);
  async function merge(game, over, out, rel) {
    const names = new Set([...(await fsp.readdir(game).catch(() => [])), ...(over ? await fsp.readdir(over).catch(() => []) : [])]);
    for (const name of names) {
      const r = rel ? `${rel}/${name}` : name;
      if (skip(r)) continue;
      const g = path.join(game, name), o = over && path.join(over, name), t = path.join(out, name);
      const gs = await fsp.lstat(g).catch(() => null), os = o ? await fsp.lstat(o).catch(() => null) : null;
      // The game's own logs stay out: a symlinked log points into the read-only game folder, so the engine
      // can't write a fresh one and REA read the user's old play (a Unity game: every run "passed"
      // on BepInEx/LogOutput.log from a real launch hours earlier).
      if (!os && gs?.isFile() && /\.log(\.\d+)?$/i.test(name)) continue;
      if (!os && installed.has(r)) {
        const original = installed.get(r);
        if (original === null) throw new Error(`${r} in the game folder is an installed mod build whose original backup is missing: uninstall the mod (REA page) or verify the game files in Steam`);
        if (original) await fsp.symlink(original, t);
        continue;
      }
      if (os?.isDirectory() && (!gs || gs.isDirectory())) {
        await fsp.mkdir(t, { recursive: true });
        await merge(gs ? g : path.join(out, ".missing"), o, t, r);
      } else if (os) await fsp.cp(o, t, { recursive: true });
      else if (gs?.isDirectory() && ([...installed.keys()].some((k) => k.startsWith(`${r}/`)) || (copyMatch && await hasMatch(g, copyMatch)))) {
        await fsp.mkdir(t, { recursive: true });
        await merge(g, null, t, r);
      } else if (gs?.isFile() && ((!rel && gs.size < 64 * 1024 * 1024) || copyMatch?.test(name))) await fsp.copyFile(g, t);
      else if (gs) await fsp.symlink(g, t);
    }
  }
  await merge(gameDir, overlay, stage, "");
}

async function hasMatch(dir, re, depth = 0) {
  if (depth > 3) return false;
  for (const e of await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])) {
    if (e.isFile() && re.test(e.name)) return true;
    if (e.isDirectory() && await hasMatch(path.join(dir, e.name), re, depth + 1)) return true;
  }
  return false;
}

// Logs the game writes itself (Unity's player log can be hundreds of MB): keep the first and last MB.
async function trimLog(file) {
  const st = await fsp.stat(file).catch(() => null);
  if (!st || st.size <= MAX_LOG) return;
  const fd = await fsp.open(file, "r");
  const head = Buffer.alloc(MAX_LOG / 2), tail = Buffer.alloc(MAX_LOG / 2);
  try { await fd.read(head, 0, head.length, 0); await fd.read(tail, 0, tail.length, st.size - tail.length); } finally { await fd.close(); }
  await fsp.writeFile(file, Buffer.concat([head, Buffer.from(`\n\n[… ${((st.size - MAX_LOG) / 1048576).toFixed(1)} MB cut …]\n\n`), tail]));
}

// Error lines compared across runs: no timestamps, addresses, object ids or durations.
const normalize = (line) => line.replace(/^\s*\[?\d{1,2}:\d{2}:\d{2}(\.\d+)?\]?\s*/, "").replace(/0x[0-9a-f]+/gi, "0x?")
  .replace(/\b(id|ID|instance|Object)\s*[#:]?\s*\d+/g, "$1 ?").replace(/\d+(\.\d+)?\s*(ms|s)\b/g, "?$2").replace(/\s+/g, " ").trim().slice(0, 300);

const noLines = (line) => line.replace(/(\.(gd|tscn|tres|cs)):\d+/g, "$1");

// Unity logs an exception as a block: the error line, then its stack trace up to a blank line (BepInEx:
// up to the next "[Level : Source]" entry). Games that throw every frame (one Unity game's GalaxyManager
// without its native lib: ~1,300 blocks/s) buried the mod's one exception in a 97 MB log that trimLog cut.
const ERROR_START = /^\S*(Exception|Error)\b.*:|^\[(Error|Fatal)\s*:/;
function errorBlocks(text, keep = 3) {
  const lines = text.split("\n"), out = [], traces = new Map(), counts = new Map();
  for (let i = 0; i < lines.length; i++) {
    if (!ERROR_START.test(lines[i])) { out.push(lines[i]); continue; }
    let j = i + 1;
    const bracket = lines[i].startsWith("[");
    while (j < lines.length && j - i < 40 && lines[j].trim() && !/^\[\w+\s*:/.test(lines[j]) && (bracket || !ERROR_START.test(lines[j]))) j++;
    const block = lines.slice(i, j), key = normalize(block.join("\n"));
    const n = (counts.get(key) || 0) + 1;
    counts.set(key, n);
    if (!traces.has(normalize(lines[i]))) traces.set(normalize(lines[i]), block.slice(1, 16).map((l) => l.trim().slice(0, 300)));
    if (n <= keep) out.push(...block);
    else if (n === keep + 1) out.push(`[REA: further repeats of this error block are hidden; count at the end of the log]`);
    i = j - 1;
  }
  const hidden = [...counts.entries()].filter(([, n]) => n > keep);
  if (hidden.length) out.push("", "[REA: repeated error blocks, shown " + keep + "x above]", ...hidden.map(([k, n]) => `  ${n}x ${k.split("\n")[0].slice(0, 200)}`));
  return { text: out.join("\n"), traces };
}

function newErrors(errors, baseline) {
  const known = new Set(baseline.map(normalize));
  const seen = new Set();
  return errors.filter((e) => { const n = normalize(e); if (known.has(n) || seen.has(n)) return false; seen.add(n); return true; });
}

async function fingerprint(file) {
  const st = await fsp.stat(file).catch(() => null);
  return st ? `${st.size}:${Math.round(st.mtimeMs)}` : "missing";
}

// With screenshots on, the baseline also runs on a display (rendering adds its own errors) and its
// screenshots are kept next to the cache, then copied into every run as base-*.png.
async function cachedBaseline(key, make, log, ctx = null) {
  if (ctx?.shots) key += ctx.shotPlan === "three" ? "-shots3" : "-shots";
  const file = path.join(BASELINES, `${key}.json`);
  const store = path.join(BASELINES, `${key}.shots`), out = ctx ? path.join(ctx.dir, "shots") : "";
  try {
    const hit = JSON.parse(await fsp.readFile(file, "utf8"));
    log.write(`Baseline (unmodified game) from ${new Date(hit.at).toISOString()}: ${hit.errors.length} error line(s) the game already has\n`);
    if (ctx) ctx.baselineErrorCount = new Set((hit.errors || []).map(normalize)).size;
    if (ctx?.shots && hit.shots?.length) {
      await fsp.mkdir(out, { recursive: true });
      for (const s of hit.shots) await fsp.copyFile(path.join(store, s.file), path.join(out, s.file)).catch(() => {});
      ctx.baseShots = hit.shots;
    }
    return hit;
  } catch {}
  log.write(`Running the unmodified game once for a baseline${ctx?.shots ? " with screenshots" : ""} (cached for this game version)…\n`);
  const result = await make();
  if (ctx) ctx.baselineErrorCount = new Set((result.errors || []).map(normalize)).size;
  await fsp.mkdir(BASELINES, { recursive: true });
  if (ctx?.shots && result.shots?.length) {
    await fsp.rm(store, { recursive: true, force: true });
    await fsp.mkdir(store, { recursive: true });
    for (const s of result.shots) await fsp.copyFile(path.join(out, s.file), path.join(store, s.file)).catch(() => {});
    ctx.baseShots = result.shots;
  }
  await fsp.writeFile(file, JSON.stringify({ ...result, at: now() }, null, 1));
  return result;
}

// The agent's "three" plan takes only its three timed frames, no extra event captures.
const shotLimits = (ctx) => ctx.shotPlan === "three" ? { maxEvents: 0 } : {};

// One play on a virtual X display with the capture plan running next to it (or plain headless when
// screenshots are off: fn gets null). The play's result gets .shots.
async function onDisplay(ctx, base, log, fn) {
  if (!ctx.shots) return fn(null);
  const disp = await startDisplay(path.join(ctx.scratch, base ? "x-base" : "x-mod"), log);
  const shoot = shooter({ dir: path.join(ctx.dir, "shots"), prefix: base ? "base-" : "", base, log, grab: xvfbGrab(disp.fb, ctx.shotPlan === "three"), ...shotLimits(ctx) });
  const plan_ = schedule(plan(ctx.timeoutMs, xInput(disp.display), ctx.shotPlan), shoot);
  try {
    const result = await fn({ env: { ...DISPLAY_ENV, DISPLAY: disp.display }, shoot });
    return { ...result, shots: shoot.shots };
  } finally {
    plan_.stop();
    await shoot.done();
    disp.stop();
    if (!base) ctx.modShots = shoot.shots;
  }
}

// ------------------------------------------------------------------------------- godot --
// Official headless Godot for that version, SHA-512 checked against the release's SHA512-SUMS.txt.
// display: Godot 3's server build can't render, so screenshot runs use its X11 build (Godot 4 has one binary).
async function godotBinary(v, log, display = false) {
  const dir = path.join(ENGINES, `godot-${v.version}`);
  const zip = v.major >= 4 ? `Godot_v${v.version}-stable_linux.x86_64.zip` : `Godot_v${v.version}-stable_${display ? "x11" : "linux_server"}.64.zip`;
  const bin = path.join(dir, zip.replace(/\.zip$/, ""));
  const managed = path.join(ENGINES, "official", `godot-${v.version}`, zip.replace(/\.zip$/, ""));
  if (exists(managed)) return managed;
  if (exists(bin)) return bin;
  const base = `https://github.com/godotengine/godot/releases/download/${v.version}-stable/`;
  log.write(`Downloading the ${display ? "" : "headless "}Godot ${v.version} engine (${zip})…\n`);
  const sums = await (await fetch(base + "SHA512-SUMS.txt")).text();
  const want = sums.split("\n").find((l) => l.trim().endsWith(zip))?.split(/\s+/)[0];
  if (!want) throw new Error(`Godot ${v.version} publishes no checksum for ${zip}`);
  const response = await fetch(base + zip);
  if (!response.ok) throw new Error(`Download failed: HTTP ${response.status}`);
  const data = Buffer.from(await response.arrayBuffer());
  const got = createHash("sha512").update(data).digest("hex");
  if (got !== want) throw new Error(`Checksum mismatch for ${zip}`);
  await fsp.mkdir(dir, { recursive: true });
  await fsp.writeFile(path.join(dir, zip), data);
  await runCmd(["unzip", "-o", "-q", path.join(dir, zip), "-d", dir], log);
  await fsp.rm(path.join(dir, zip));
  await fsp.chmod(bin, 0o755);
  log.write(`Godot ${v.version} ready (sha512 verified)\n`);
  return bin;
}

async function godotSteamBinary(v, log) {
  if (v.version !== "3.5.2") throw new Error(`No pinned GodotSteam runtime for ${v.version}`);
  const installed = path.join(ENGINES, "godotsteam", "godotsteam-3.5.2", "linux-352-editor.64");
  if (exists(installed)) return installed;
  const local = path.join(ENGINES, "godotsteam-local-3.5.2");
  const bin = path.join(local, "linux-352-editor.64");
  if (exists(bin) && exists(path.join(local, "libsteam_api.so"))) return bin;
  const staging = path.join(ENGINES, `.godotsteam-${process.pid}-${randomBytes(4).toString("hex")}`);
  await fsp.mkdir(staging, { recursive: true });
  try {
    log.write("Downloading pinned GodotSteam 3.5.2 engine for this test run…\n");
    const response = await fetch(GODOTSTEAM_URL);
    if (!response.ok) throw new Error(`GodotSteam download failed: HTTP ${response.status}`);
    const archive = Buffer.from(await response.arrayBuffer());
    if (createHash("sha256").update(archive).digest("hex") !== GODOTSTEAM_SHA256)
      throw new Error("GodotSteam archive SHA-256 mismatch");
    const zip = path.join(staging, "godotsteam.zip");
    await fsp.writeFile(zip, archive);
    await runCmd(["unzip", "-q", zip, "linux-352-editor.64", "libsteam_api.so", "-d", staging], log);
    await fsp.rm(zip);
    await fsp.chmod(path.join(staging, "linux-352-editor.64"), 0o755);
    await fsp.rm(local, { recursive: true, force: true });
    await fsp.rename(staging, local);
    return bin;
  } finally { await fsp.rm(staging, { recursive: true, force: true }).catch(() => {}); }
}

const GODOT_ERROR = /^(SCRIPT ERROR|ERROR|USER ERROR|USER SCRIPT ERROR|Parse Error|Parser Error|handle_crash|CrashHandlerException)|Segmentation fault|Invalid call\.|Attempt to call function/;

function godotErrors(lines) {
  // An error line plus its "At: res://…" line, as one entry.
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (!GODOT_ERROR.test(lines[i].trim())) continue;
    const at = /^\s*[Aa]t: /.test(lines[i + 1] || "") ? ` ${lines[i + 1].trim()}` : "";
    out.push(lines[i].trim() + at);
  }
  return out;
}

async function testGodot(p, ctx) {
  const pack = path.join(installDir(p), p.pckRel);
  if (!exists(pack)) throw new Error(`install/${p.pckRel} is missing: the build didn't produce the patched pack`);
  const fd = await fsp.open(pack, "r");
  try {
    const marker = Buffer.alloc(4);
    await fd.read(marker, 0, 4, 0);
    if (/\.exe$/i.test(p.pckRel) && marker.subarray(0, 2).toString() !== "MZ")
      throw new Error(`install/${p.pckRel} is a raw PCK, not an executable; rebuild with executable pack assembly`);
  } finally { await fd.close(); }
  const gamePack = await originalFile(p);
  const v = await pckVersion(gamePack);
  ctx.note(`Godot ${v.version} game`);
  const report = await fsp.readFile(path.join(MODS, p.id, "decompiled/project/gdre_export.log"), "utf8").catch(() => "");
  const needsSteam = /Godot editor version .*Steam edition/i.test(report);
  let bin;
  try { bin = needsSteam ? await godotSteamBinary(v, ctx.check) : await godotBinary(v, ctx.check); }
  catch (error) {
    if (!needsSteam) throw error;
    ctx.runtimeLimit = true;
    ctx.inconclusive = `GodotSteam ${v.version} is required but could not be downloaded: ${error.message}. It is also available in Manager → Core Backend → REA Engines.`;
    ctx.check.write(ctx.inconclusive + "\n");
    return { phase: "check" };
  }
  const steamDir = path.dirname(bin);
  const engineCommand = needsSteam ? "xvfb-run" : bin;
  const engineArgs = (args) => needsSteam ? ["-a", "-s", "-screen 0 1280x720x24", bin, ...args] : args;
  if (needsSteam) ctx.note(`GodotSteam ${v.version} engine`);
  const home = path.join(ctx.scratch, "home");
  await fsp.mkdir(home, { recursive: true });
  // No sound card here: a null ALSA config keeps ~150 "ALSA lib …" lines per run out of the logs.
  const alsa = path.join(ctx.scratch, "asound.conf");
  await fsp.writeFile(alsa, "pcm.!default { type null }\nctl.!default { type hw card 0 }\n");
  const env = { HOME: home, XDG_DATA_HOME: path.join(home, ".local/share"), XDG_CONFIG_HOME: path.join(home, ".config"),
    SDL_AUDIODRIVER: "dummy", ALSA_CONFIG_PATH: alsa, ...(needsSteam ? { LD_LIBRARY_PATH: steamDir } : {}) };
  const headless = v.major >= 4 ? ["--headless"] : [];

  // 1. Syntax: compile every GDScript the mod adds or replaces INSIDE the running recovered project,
  // like Validate build: the game's autoload singletons load first, then a checker scene load()s each
  // script. Per-file --check-only reported every script that reaches the game's mutually-referencing
  // singletons as a cyclic reference (one Godot game: 17 false errors, "failed",
  // while Validate build compiled 198/198).
  const files = await findFiles(path.join(MODS, p.id, "src/files"), (f) => f.endsWith(".gd"), 12);
  const recovered = path.join(MODS, p.id, "decompiled/project");
  if (files.length && exists(path.join(recovered, "project.godot"))) {
    const stage = path.join(ctx.scratch, "project"), original = path.join(ctx.scratch, "original");
    // Keep the recovered import cache (.import/*.stex, .godot/imported): without it every texture-backed
    // preload fails ("Can't preload resource…") and the agent chases errors the mod didn't cause.
    await stageGame(recovered, path.join(MODS, p.id, "src/files"), stage);
    const check = await import("./buildcheck.mjs");
    const cctx = { scratch: path.join(ctx.scratch, "compile"), log: ctx.check };
    await fsp.mkdir(path.join(cctx.scratch, "home"), { recursive: true });
    try {
      const eng = await check.godotEngine(cctx, v, needsSteam);
      const mine = await check.godotCompileAll(cctx, eng, stage, files);
      let broken = [...mine.broken];
      // Fails the same way in the original game's script (e.g. GodotSteam's Steam singleton)? Not the mod's fault.
      const replaced = broken.map(([f]) => f.replace(/^res:\/\//, "")).filter((f) => exists(path.join(recovered, f)));
      if (replaced.length) {
        await stageGame(recovered, null, original);
        const theirs = await check.godotCompileAll(cctx, eng, original, replaced);
        for (const [f] of broken) if (theirs.broken.has(f)) ctx.check.write(`(${f}: also fails in the original game's script, ignored)\n`);
        broken = broken.filter(([f]) => !theirs.broken.has(f));
      }
      for (const [f, errors] of broken) ctx.errors.push(...(errors.length ? errors : ["failed to load"]).slice(0, 5).map((e) => `[syntax] ${f}: ${e}`));
      for (const n of mine.notes.slice(0, 10)) ctx.check.write(`warning: ${n}\n`);
    } finally { cctx.display?.stop(); }
    if (ctx.errors.length) return { phase: "check" };
    ctx.check.write(`${files.length} GDScript file(s) compile inside the recovered game\n`);
  } else ctx.check.write(files.length ? "No recovered project.godot: skipped the per-script compile check\n" : "No GDScript files in the mod\n");

  // 2. Run the patched pack headless (or on a virtual display, for screenshots), then the original
  // one (baseline), and compare.
  const modFiles = files.length ? files : await findFiles(path.join(MODS, p.id, "src/files"), () => true, 12);
  const playBin = ctx.shots && !needsSteam ? await godotBinary(v, ctx.check, true) : bin;
  const play = (packFile, log, base) => onDisplay(ctx, base, log, async (screen) => {
    const lines = [];
    const shown = screen ? ["--audio-driver", "Dummy", "--resolution", `${WIDTH}x${HEIGHT}`, "--position", "0,0",
      ...(v.major >= 4 ? ["--rendering-driver", "opengl3", "--rendering-method", "gl_compatibility"] : [])] : null;
    const onLine = (l) => {
      lines.push(l);
      if (!screen) return;
      const res = /^Loading resource: (res:\/\/\S+)/.exec(l)?.[1];
      if (!base && res && modFiles.some((f) => res === `res://${f}`)) screen.shoot.event(`mod file loaded: ${res}`);
      else if (GODOT_ERROR.test(l.trim())) screen.shoot.event(`error: ${l.trim().slice(0, 140)}`);
    };
    const r = screen
      ? await launch(playBin, [...shown, "--verbose", "--main-pack", packFile], { cwd: ctx.scratch, env: { ...env, ...screen.env }, log, timeoutMs: ctx.timeoutMs, onLine })
      : await launch(engineCommand, engineArgs([...headless, "--verbose", "--main-pack", packFile]), { cwd: ctx.scratch, env, log, timeoutMs: ctx.timeoutMs, onLine });
    return { errors: godotErrors(lines), early: !r.timedOut, code: r.code, ms: r.ms,
      steamUnavailable: lines.some((l) => /Failed to initialize Steam|SteamAPI_IsSteamRunning\(\) did not locate/.test(l)),
      loaded: lines.filter((l) => /^Loading resource: res:\/\//.test(l)).map((l) => l.slice(18).trim()) };
  });
  const baselineKey = `${needsSteam ? "godotsteam" : "godot"}-${v.version}-${createHash("sha1").update(p.gamePath + (await fingerprint(gamePack))).digest("hex").slice(0, 16)}-${ctx.timeoutMs}`;
  const base = await cachedBaseline(baselineKey,
    async () => { const r = await play(gamePack, ctx.baseline, true); return { errors: r.errors, early: r.early, code: r.code, ms: r.ms, steamUnavailable: r.steamUnavailable, shots: r.shots }; }, ctx.run, ctx);
  const mine = await play(pack, ctx.run, false);
  ctx.ranMs = mine.ms;
  if (base.early) ctx.limited = base.steamUnavailable
    ? `The unmodified game exits under GodotSteam ${v.version} because Steam is unavailable in the isolated REA test container; only startup could be tested.`
    : `The unmodified game itself stops after ${Math.round((base.ms || 0) / 1000)}s on ${needsSteam ? "GodotSteam" : "stock Godot"} ${v.version}` +
      `${base.errors[0] ? ` (first error: ${base.errors[0].slice(0, 160)})` : ""}; only startup could be tested.`;
  ctx.errors.push(...newErrors(mine.errors, base.errors));
  if (mine.early && !base.early && mine.code !== 0) ctx.errors.push(`The game exited early (code ${mine.code}) while the unmodified game kept running`);
  const used = modFiles.filter((f) => mine.loaded.includes(`res://${f}`) || mine.loaded.some((x) => x.startsWith(`res://${f}`)));
  ctx.coverage = { loaded: used, total: modFiles.length };
  if (ctx.limited) ctx.runtimeLimit = true;
  if (ctx.limited) ctx.inconclusive = ctx.limited + ` Only ${used.length} of ${modFiles.length} mod files were observed loading; gameplay was not verified.`;
  else if (!used.length) ctx.inconclusive = "The game ran, but none of the mod's files were loaded during the run (so nothing of the mod was exercised).";
  return { phase: "run" };
}

// IL at the failing offset of a game frame ("at MenuGUI+<X>d__667.MoveNext () [0x00f05]"): the
// instruction there names what was null, which the decompiled C# can't (seen in a Unity mod: two
// guessed fixes for an NRE that was MenuGUI.charSelectType == null). ilspycmd -il ignores -t, so the
// whole Assembly-CSharp is dumped once per game build (~1 min, ~90 MB) and looked up with grep/sed.
const sh = (cmd, args, opts = {}) => new Promise((resolve) => {
  const c = spawn(cmd, args, { stdio: ["ignore", "pipe", "ignore"], ...opts });
  let out = "";
  c.stdout.on("data", (d) => { if (out.length < 4e6) out += d; });
  c.on("close", () => resolve(out));
  c.on("error", () => resolve(""));
});
async function ilDump(dll) {
  const file = path.join(BASELINES, `il-${createHash("sha1").update(dll + await fingerprint(dll)).digest("hex").slice(0, 16)}.il`);
  if (exists(file)) return file;
  await fsp.mkdir(BASELINES, { recursive: true });
  const out = fs.openSync(file + ".tmp", "w");
  await new Promise((resolve) => {
    const c = spawn("ilspycmd", ["-il", dll], { stdio: ["ignore", out, "ignore"], cwd: BASELINES });
    const kill = setTimeout(() => c.kill("SIGKILL"), 300000);
    c.on("close", () => { clearTimeout(kill); resolve(); });
    c.on("error", () => { clearTimeout(kill); resolve(); });
  });
  fs.closeSync(out);
  if ((await fsp.stat(file + ".tmp").catch(() => null))?.size > 1000) await fsp.rename(file + ".tmp", file);
  else { await fsp.rm(file + ".tmp", { force: true }); return ""; }
  return file;
}
async function ilAt(p, trace) {
  const dll = path.join(p.gamePath, p.dataDir || "", "Managed", "Assembly-CSharp.dll");
  if (!exists(dll)) return "";
  for (const frame of trace) {
    const m = frame.match(/^at ([\w.+<>`]+?)\.([\w<>`.]+) \(.*?\) \[0x([0-9a-f]+)\]/i);
    if (!m || /^(UnityEngine|System|Mono|BepInEx|HarmonyLib|MonoMod|Steamworks|Galaxy)\b/.test(m[1])) continue;
    const dump = await ilDump(dll);
    if (!dump) return "";
    const inner = m[1].split("+").pop(), quoted = /[<>]/.test(inner) ? `'${inner}'` : inner;
    const label = `IL_${Number.parseInt(m[3], 16).toString(16).padStart(4, "0")}:`;
    const ends = (await sh("grep", ["-n", "-F", `// end of method ${quoted}::${m[2]}`, dump])).split("\n").filter(Boolean).map((l) => Number(l.split(":")[0]));
    for (const end of ends) {
      const body = (await sh("sed", ["-n", `${Math.max(1, end - 6000)},${end}p`, dump])).split("\n");
      const start = body.map((l) => /^\s*\.method /.test(l)).lastIndexOf(true);
      const at = body.findIndex((l, i) => i > start && l.trim().startsWith(label));
      if (at < 0) continue;
      return `${m[1]}.${m[2]} at ${label.slice(0, -1)} (the instruction at the offset and the ones after it are what failed; ` +
        `for a NullReferenceException the ldfld/callvirt there names the null field or call):\n` +
        body.slice(Math.max(start, at - 14), at + 6).map((l) => l.replace(/^\t+/, "  ")).join("\n");
    }
  }
  return "";
}

// ------------------------------------------------------------------------------- unity --
// The user's settings and saves for this game (~/.config/unity3d/<company>/<product>, mounted read-only
// under UNITY_DATA): copied fresh into the run's HOME before each launch, so the game starts like the
// user's install instead of on first-launch screens. Logs are left out; nothing is written back.
const UNITY_DATA = process.env.REA_UNITY_DATA || "/unity-data";
async function unitySaves(p) {
  const info = (await fsp.readFile(path.join(p.gamePath, p.dataDir || "", "app.info"), "utf8").catch(() => "")).split("\n").map((x) => x.trim());
  const [company, product] = info;
  let src = "";
  if (company && product)
    for (const root of await fsp.readdir(UNITY_DATA).catch(() => [])) {
      const dir = path.join(UNITY_DATA, root, company, product);
      if (exists(dir)) { src = dir; break; }
    }
  return async (home, log) => {
    if (!src) return;
    const out = path.join(home, ".config/unity3d", company, product);
    await fsp.rm(out, { recursive: true, force: true });
    await fsp.cp(src, out, { recursive: true, filter: (f) => !/(^|\/)Player(-prev)?\.log$/.test(f) });
    log.write(`Copied the game's settings and saves (${company}/${product}) into the test HOME\n`);
  };
}

async function testUnity(p, ctx) {
  const exe = (await fsp.readdir(p.gamePath)).find((f) => f.endsWith(".x86_64") && f.replace(/\.x86_64$/, "") === p.dataDir.replace(/_Data$/, ""))
    || (await fsp.readdir(p.gamePath)).find((f) => f.endsWith(".x86_64"));
  if (!exe) throw new Error("No Linux executable (*.x86_64) in the game folder");
  const home = path.join(ctx.scratch, "home");
  await fsp.mkdir(home, { recursive: true });
  const seedSaves = await unitySaves(p);
  // run_bepinex.sh asks `file` for the executable's architecture; the image has no `file`, and the
  // game was already detected as a 64-bit Linux build.
  const shims = path.join(ctx.scratch, "bin");
  await fsp.mkdir(shims, { recursive: true });
  await fsp.writeFile(path.join(shims, "file"), "#!/bin/sh\necho 'ELF 64-bit LSB pie executable, x86-64'\n", { mode: 0o755 });
  const pluginDir = `BepInEx/plugins/${p.className}`;
  // The game folder's own BepInEx (the user's real install) is read-only here and has their installed
  // plugins: BepInEx must write its cache and config (a symlinked config killed the preloader before it
  // logged anything), and only this mod's plugin may load (the user's own installed mod DLL from an
  // older build loaded next to every test build).
  const noCache = (r) => r === "BepInEx/cache" || r === "BepInEx/config" ||
    (r.startsWith("BepInEx/plugins/") && r !== pluginDir && !r.startsWith(`${pluginDir}/`));
  const writableConfig = async (stage) => {
    if (exists(path.join(p.gamePath, "BepInEx/config")))
      await fsp.cp(path.join(p.gamePath, "BepInEx/config"), path.join(stage, "BepInEx/config"), { recursive: true });
  };
  const pluginRe = new RegExp(`Loading \\[${p.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}|${p.className}`, "i");
  const play = (stage, log, playerLog, base) => onDisplay(ctx, base, log, async (screen) => {
    await seedSaves(home, log);
    // On a display: windowed 1280x720, OpenGL (Mesa's software renderer; there is no Vulkan driver here).
    const mode = screen ? ["-screen-fullscreen", "0", "-screen-width", String(WIDTH), "-screen-height", String(HEIGHT), "-force-glcore"] : ["-batchmode", "-nographics"];
    let watch = null;
    if (screen) {
      // BepInEx writes its log as it goes: screenshot when the plugin loads or the first errors appear.
      let seen = 0;
      watch = setInterval(async () => {
        const text = await fsp.readFile(path.join(stage, "BepInEx/LogOutput.log"), "utf8").catch(() => "");
        for (const line of text.slice(seen).split("\n")) {
          if (!base && pluginRe.test(line)) screen.shoot.event(`mod loaded: ${line.trim().slice(0, 140)}`, 3000);
          else if (/^\[(Error|Fatal)\s*:/.test(line)) screen.shoot.event(`error: ${line.trim().slice(0, 140)}`);
        }
        seen = text.length;
      }, 1000);
    }
    const r = await launch("sh", ["run_bepinex.sh", `./${exe}`, ...mode, "-logFile", playerLog],
      { cwd: stage, env: { HOME: home, PATH: `${shims}:${process.env.PATH}`, ...(screen?.env || {}) }, log, timeoutMs: ctx.timeoutMs });
    clearInterval(watch);
    const bep = await fsp.readFile(path.join(stage, "BepInEx/LogOutput.log"), "utf8").catch(() => "");
    const player = await fsp.readFile(playerLog, "utf8").catch(() => "");
    const pb = errorBlocks(player), bb = errorBlocks(bep);
    if (player) await fsp.writeFile(playerLog, pb.text);   // repeats collapsed, so the mod's own errors survive trimLog
    await trimLog(playerLog);
    const errors = [
      ...bep.split("\n").filter((l) => /^\[(Error|Fatal)\s*:/.test(l)),
      ...player.split("\n").filter((l) => /^\S*(Exception|Error)\b.*:/.test(l) && !/^(Fallback|Unable to|Desktop is)/.test(l))];
    return { errors, early: !r.timedOut, code: r.code, ms: r.ms, bep: bb.text, player, traces: new Map([...pb.traces, ...bb.traces]) };
  });
  const stage = path.join(ctx.scratch, "stage");
  // Baseline = the game with the loader but without this mod's plugin.
  const base = await cachedBaseline(`unity4-${createHash("sha1").update(p.gamePath + (await fingerprint(path.join(p.gamePath, exe))) + p.engine).digest("hex").slice(0, 16)}-${ctx.timeoutMs}`,
    async () => {
      await stageGame(p.gamePath, installDir(p), stage, { skip: (r) => r === pluginDir || noCache(r) });
      await writableConfig(stage);
      const r = await play(stage, ctx.baseline, path.join(ctx.dir, "baseline-player.log"), true);
      return { errors: r.errors, early: r.early, code: r.code, ms: r.ms, shots: r.shots };
    }, ctx.run, ctx);
  await stageGame(p.gamePath, installDir(p), stage, { skip: noCache });
  await writableConfig(stage);
  const mine = await play(stage, ctx.run, path.join(ctx.dir, "player.log"), false);
  ctx.ranMs = mine.ms;
  await fsp.writeFile(path.join(ctx.dir, "game.log"), mine.bep.slice(0, MAX_LOG));
  const fresh = newErrors(mine.errors, base.errors);
  ctx.errors.push(...fresh);
  ctx.errorDetails = fresh.slice(0, 25).map((e) => ({ error: e.slice(0, 300), trace: mine.traces.get(normalize(e)) || [] }));
  for (const d of ctx.errorDetails.slice(0, 3)) d.il = await ilAt(p, d.trace).catch(() => "") || undefined;
  if (mine.early && !base.early && mine.code !== 0) ctx.errors.push(`The game exited early (code ${mine.code}) while it kept running without the mod`);
  const loaded = pluginRe.test(mine.bep);
  ctx.coverage = { loaded: loaded ? [p.className] : [], total: 1 };
  if (!mine.bep) ctx.inconclusive = `BepInEx wrote no log: the loader didn't start in the ${ctx.shots ? "" : "headless "}run.`;
  else if (base.early && loaded) {
    ctx.runtimeLimit = true;
    ctx.inconclusive = `The game without the mod also stops after ${Math.round((base.ms || 0) / 1000)}s headless (it may need Steam or a display): the plugin loaded without new errors, but only startup could be tested.`;
  }
  else if (!loaded) ctx.errors.push(`BepInEx started but never loaded the plugin ${p.className} (see game.log)`);
  return { phase: "run" };
}

// ---------------------------------------------------------------------------- rpgmaker --
// The game in headless Chromium (software rendering, no GPU), errors read over the DevTools protocol.
// Screenshots come from Chromium itself (Page.captureScreenshot renders the real frame, so no X
// display is needed); inputs are DevTools key/mouse events: Enter on the title screen = New Game.
async function testRpgMaker(p, ctx) {
  const webRoot = path.dirname(p.jsDir); // "www" or "."
  const play = async (overlay, log, base) => {
    const server = http.createServer(async (req, res) => {
      const rel = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/+/, "") || "index.html";
      for (const root of [overlay && path.join(overlay, webRoot), path.join(p.gamePath, webRoot)].filter(Boolean)) {
        const file = path.resolve(root, rel);
        if (!file.startsWith(path.resolve(root) + path.sep)) continue;
        const st = await fsp.stat(file).catch(() => null);
        if (!st?.isFile()) continue;
        const type = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".png": "image/png", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".css": "text/css" }[path.extname(file)] || "application/octet-stream";
        res.writeHead(200, { "Content-Type": type });
        fs.createReadStream(file).pipe(res);
        return;
      }
      res.writeHead(404).end();
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    const port = server.address().port;
    const errors = [];
    let pluginScripts = [];
    let ws = null, shoot = null;
    const profile = path.join(ctx.scratch, `chrome-${randomBytes(3).toString("hex")}`);
    const done = launch("chromium", ["--headless=new", ...(ctx.shots ? [`--window-size=${WIDTH},${HEIGHT}`] : []), "--no-sandbox", "--disable-gpu", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
      "--mute-audio", "--autoplay-policy=no-user-gesture-required", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
    { cwd: ctx.scratch, log, timeoutMs: ctx.timeoutMs + 30000, onLine: (line) => {
      const m = line.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m && !ws) ws = m[1];
    } });
    for (let i = 0; i < 100 && !ws; i++) await new Promise((r) => setTimeout(r, 100));
    if (!ws) { server.close(); throw new Error("Headless Chromium didn't start (see run.log)"); }
    const socket = new WebSocket(ws);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = () => reject(new Error("DevTools connection failed")); });
    let id = 0;
    const waiting = new Map();
    const call = (method, params = {}, sessionId) => new Promise((resolve) => {
      const n = ++id; waiting.set(n, resolve); socket.send(JSON.stringify({ id: n, method, params, ...(sessionId ? { sessionId } : {}) }));
      setTimeout(() => { if (waiting.delete(n)) resolve({}); }, 30000).unref();
    });
    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && waiting.has(msg.id)) { waiting.get(msg.id)(msg.result || {}); waiting.delete(msg.id); return; }
      if (msg.method === "Runtime.exceptionThrown") {
        const d = msg.params.exceptionDetails;
        errors.push(`Uncaught ${(d.exception?.description || d.text || "").split("\n")[0]}${d.url ? ` (${d.url.replace(/^http:\/\/[^/]+/, "")}:${d.lineNumber + 1})` : ""}`);
        shoot?.event(`error: ${errors.at(-1).slice(0, 140)}`, 500);
      } else if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
        errors.push(`console.error ${msg.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 300)}`);
      } else if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
        errors.push(`${msg.params.entry.text} ${(msg.params.entry.url || "").replace(/^http:\/\/[^/]+/, "")}`.trim());
      }
      log.write(msg.method ? `${msg.method} ${JSON.stringify(msg.params).slice(0, 400)}\n` : "");
    };
    const { targetId } = await call("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await call("Target.attachToTarget", { targetId, flatten: true });
    await call("Runtime.enable", {}, sessionId);
    await call("Log.enable", {}, sessionId);
    await call("Page.enable", {}, sessionId);
    let plan_ = null;
    if (ctx.shots) {
      await call("Emulation.setDeviceMetricsOverride", { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false }, sessionId);
      shoot = shooter({ dir: path.join(ctx.dir, "shots"), prefix: base ? "base-" : "", base, log, ...shotLimits(ctx),
        grab: async () => { const { data } = await call("Page.captureScreenshot", { format: "png",
          ...(ctx.shotPlan === "three" ? { clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT, scale: 0.5 } } : {}) }, sessionId); return data ? { png: Buffer.from(data, "base64") } : null; } });
      const key = (type, k, code, vk) => call("Input.dispatchKeyEvent", { type, key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk }, sessionId);
      const input = async (step) => {
        if (step === "enter") { await key("keyDown", "Enter", "Enter", 13); await new Promise((r) => setTimeout(r, 120)); await key("keyUp", "Enter", "Enter", 13); }
        else if (step === "click") for (const type of ["mousePressed", "mouseReleased"])
          await call("Input.dispatchMouseEvent", { type, x: WIDTH / 2, y: HEIGHT / 2, button: "left", clickCount: 1 }, sessionId);
        else if (step === "move") { await key("keyDown", "ArrowRight", "ArrowRight", 39); await new Promise((r) => setTimeout(r, 2000)); await key("keyUp", "ArrowRight", "ArrowRight", 39); }
      };
      plan_ = schedule(plan(ctx.timeoutMs, input, ctx.shotPlan), shoot);
    }
    await call("Page.navigate", { url: `http://127.0.0.1:${port}/index.html` }, sessionId);
    await new Promise((r) => setTimeout(r, ctx.timeoutMs));
    plan_?.stop();
    await shoot?.done();
    const result = await call("Runtime.evaluate", { expression: "JSON.stringify(typeof PluginManager !== 'undefined' ? PluginManager._scripts : [])", returnByValue: true }, sessionId);
    try { pluginScripts = JSON.parse(result.result?.value || "[]"); } catch {}
    await Promise.race([call("Browser.close"), new Promise((r) => setTimeout(r, 3000))]);
    socket.close();
    server.close();
    await done;
    return { errors, pluginScripts, shots: shoot?.shots || [] };
  };
  const plugins = (await findFiles(path.join(installDir(p), p.jsDir, "plugins"), (f) => f.endsWith(".js"), 2)).map((f) => path.basename(f, ".js"));
  const base = await cachedBaseline(`rpgmaker-${createHash("sha1").update(p.gamePath + (await fingerprint(path.join(p.gamePath, p.jsDir, "plugins.js")))).digest("hex").slice(0, 16)}-${ctx.timeoutMs}`,
    async () => { const r = await play(null, ctx.baseline, true); return { errors: r.errors, shots: r.shots }; }, ctx.run, ctx);
  const mine = await play(installDir(p), ctx.run, false);
  ctx.modShots = mine.shots;
  ctx.errors.push(...newErrors(mine.errors, base.errors));
  const loaded = plugins.filter((x) => mine.pluginScripts.includes(x));
  ctx.coverage = { loaded, total: plugins.length };
  if (plugins.length && !loaded.length) ctx.errors.push(`The game started but PluginManager never loaded ${plugins.join(", ")}`);
  return { phase: "run" };
}

// ------------------------------------------------------------------------------- renpy --
// Ren'Py's own "lint" (parses and checks every script) with the bundled Linux runtime.
async function testRenpy(p, ctx) {
  const top = await fsp.readdir(p.gamePath);
  const launcher = top.find((f) => f.endsWith(".sh") && top.includes(f.replace(/\.sh$/, ".py")));
  if (!launcher) throw Object.assign(new Error("This Ren'Py build has no Linux launcher (*.sh): only the Windows runtime ships with it"), { unsupported: true });
  const env = { SDL_VIDEODRIVER: "dummy", SDL_AUDIODRIVER: "dummy", HOME: path.join(ctx.scratch, "home") };
  await fsp.mkdir(env.HOME, { recursive: true });
  const lint = async (overlay, log, out) => {
    const stage = path.join(ctx.scratch, overlay ? "stage" : "original");
    await stageGame(p.gamePath, overlay, stage, { copyMatch: /\.(rpy|rpyc|rpymc)$/ });
    await launch("sh", [launcher, "lint", out], { cwd: stage, env, log, timeoutMs: Math.max(ctx.timeoutMs, 120000) });
    const text = await fsp.readFile(out, "utf8").catch(() => "");
    const errors = text.split("\n").filter((l) => /^game\/.*:\d+ /.test(l) || /Exception|Error:/.test(l));
    const traceback = await fsp.readFile(path.join(stage, "traceback.txt"), "utf8").catch(() => "");
    if (traceback) errors.push(...traceback.split("\n").filter((l) => /Error|Exception/.test(l)).slice(0, 5));
    return { errors };
  };
  const base = await cachedBaseline(`renpy-${createHash("sha1").update(p.gamePath + (await fingerprint(path.join(p.gamePath, launcher)))).digest("hex").slice(0, 16)}`,
    async () => lint(null, ctx.baseline, path.join(ctx.dir, "baseline-lint.txt")), ctx.run);
  const mine = await lint(installDir(p), ctx.run, path.join(ctx.dir, "lint.txt"));
  ctx.errors.push(...newErrors(mine.errors, base.errors));
  // The verdict is lint's; with screenshots on, the modded game is also started on a display to see it.
  if (ctx.shots) {
    await onDisplay(ctx, false, ctx.run, (screen) => launch("sh", [launcher], { cwd: path.join(ctx.scratch, "stage"),
      env: { ...env, ...screen.env, SDL_VIDEODRIVER: "x11" }, log: ctx.run, timeoutMs: ctx.timeoutMs }));
    ctx.note("Screenshots are from a display run of the modded game; the verdict comes from Ren'Py lint");
  }
  ctx.coverage = { loaded: [], total: 0 };
  return { phase: "run" };
}

const RUNNERS = { godot: testGodot, "unity-mono": testUnity, "unity-il2cpp": testUnity, rpgmaker: testRpgMaker, renpy: testRenpy };

// ---------------------------------------------------------------------------- the run --
const runsDir = (id) => path.join(MODS, id, "runs");
async function saveRun(r) {
  await fsp.writeFile(path.join(runsDir(r.mod), r.id, "run.json"), JSON.stringify(r, null, 1));
}

// Everything a run's verdict depends on: the mod's source (a test run rebuilds from src/), the game
// files, the installed REA engines, the timeout and this runner's code.
const RUNNER_CODE = fs.statSync(new URL(import.meta.url)).mtimeMs;
async function runInputs(project, timeoutMs, screenshots = false, shotPlan = "full") {
  const hash = createHash("sha1").update(`${RUNNER_CODE}|${timeoutMs}|${screenshots ? `shots-${shotPlan}|` : ""}${project.engine}|${project.gamePath}|`);
  const src = path.join(MODS, project.id, "src");
  for (const f of (await findFiles(src, () => true, 14)).sort()) {
    hash.update(f + "\0");
    hash.update(await fsp.readFile(path.join(src, f)).catch(() => Buffer.alloc(0)));
  }
  if (project.pckRel) hash.update(await fingerprint(await originalFile(project).catch(() => path.join(project.gamePath, project.pckRel))));
  // Two levels: Core Backend installs engines inside the mounted engines/godotsteam/ folder.
  for (const e of (await fsp.readdir(ENGINES).catch(() => [])).sort()) {
    hash.update(`|engine:${e}`);
    for (const sub of (await fsp.readdir(path.join(ENGINES, e)).catch(() => [])).sort()) hash.update(`/${sub}`);
  }
  return hash.digest("hex").slice(0, 20);
}

// The [PROOF <tag>] milestones a run's logs show (see proofRule), digits/values stripped so two runs compare.
const proofTag = (p) => p.className || p.name || p.id;
async function proofSeen(p, dir) {
  const tag = `[PROOF ${proofTag(p)}]`, seen = new Map();
  for (const name of ["game.log", "run.log", "player.log"]) {
    const text = await fsp.readFile(path.join(dir, name), "utf8").catch(() => "");
    for (const line of text.split("\n")) {
      const at = line.indexOf(tag);
      if (at < 0) continue;
      const rest = line.slice(at + tag.length).trim();
      const key = rest.split(":")[0].replace(/\d+/g, "#").trim().slice(0, 120);
      if (key && !seen.has(key)) seen.set(key, rest.slice(0, 240));
    }
  }
  return [...seen.values()];
}
// Compiler / syntax-check errors (file:line: error CODE: message), deduplicated: "dotnet exited with 1"
// alone sent the agent back to reading whole logs.
async function buildErrors(read) {
  const lines = [...(await read("build.log")).split("\n"), ...(await read("check.log")).split("\n")]
    .filter((l) => /\berror\b|SCRIPT ERROR|Parse Error|SyntaxError/i.test(l) && !/^\s*\d+ Error\(s\)/.test(l))
    .map((l) => l.replace(/\s*\[[^\]]+\.csproj\]\s*$/, "").replace(/^\/opt\/data\/rocsy-workspace\/rea-mods\/[0-9a-f]+\//, "").trim());
  const uniq = [...new Set(lines)].slice(0, 25);
  return uniq.length ? [`## Build / syntax errors (${uniq.length})`, ...uniq.map((l) => `- ${l.slice(0, 400)}`), ""] : [];
}

// digest.md: what a run showed, small enough to read whole (the agent kept read_file-ing the 75K-line
// Unity player log, and every read forced a multi-minute context compression).
async function writeDigest(p, r, dir) {
  const read = (name) => fsp.readFile(path.join(dir, name), "utf8").catch(() => "");
  const tag = proofTag(p), engine = new Set(["BepInEx", "Console", "Unity Log", "HarmonyX", "Preloader"]);
  const mine = (await read("game.log")).split("\n").filter((l) => {
    const src = l.match(/^\[\w+\s*:\s*([^\]]+)\]/)?.[1]?.trim();
    return src && !engine.has(src);
  });
  const unity = (await read("player.log")).split("\n").filter((l) => l.includes(tag) || l.includes("[PROOF"));
  const lines = [...new Set([...mine, ...unity])];
  const shown = lines.length > 120 ? [...lines.slice(0, 60), `... ${lines.length - 120} more ...`, ...lines.slice(-60)] : lines;
  const out = [
    `# Test run ${r.id}: ${r.state}`, "", r.why ? `Why it was run: ${r.why}` : "", "", `Summary: ${r.summary || ""}`, "",
    ...await buildErrors(read),
    `## [PROOF ${tag}] milestones seen (${(r.proof || []).length})`, ...(r.proof || []).map((x) => `- ${x}`), "",
    `## The mod's own log lines (game.log plugin lines + player.log lines with "${tag}" or [PROOF]; ${lines.length})`, ...shown.map((l) => l.slice(0, 300)), "",
    `## New errors (${(r.errors || []).length}; errors the unmodified game also logs are left out)`,
    ...(r.errorDetails || r.errors?.map((e) => ({ error: e })) || []).slice(0, 8).flatMap((d) =>
      [`- ${d.error}`, ...(d.trace || []).slice(0, 6).map((t) => `    ${t}`), ...(d.il ? ["  IL at the failing offset:", ...d.il.split("\n").map((l) => `    ${l}`)] : [])]), "",
    `## Screenshots`, ...(r.shots || []).filter((x) => !x.base).map((x) => `- shots/${x.file}: ${x.label}`), "",
    "Full logs (grep them, don't read them whole): game.log (BepInEx), player.log (Unity), run.log, build.log, check.log, baseline.log",
  ].filter((l, i, a) => l !== "" || a[i - 1] !== "");
  await fsp.writeFile(path.join(dir, "digest.md"), out.join("\n") + "\n").catch(() => {});
}

export async function startTestRun(project, { timeout = 60, agentOnFail = true, by = "user", screenshots = false, why = "" } = {}) {
  const timeoutMs = Math.min(600, Math.max(15, Number(timeout) || 60)) * 1000;
  screenshots = Boolean(screenshots);
  // The agent's runs take 3 frames (15 s / half way / end, shown in its chat); the user's take the full plan.
  const shotPlan = by === "agent" ? "three" : "full";
  const inputs = await runInputs(project, timeoutMs, screenshots, shotPlan).catch(() => "");
  claimJob(project, "testrun");
  const id = `${new Date().toISOString().replace(/[-:]/g, "").replace(/\..*/, "")}-${randomBytes(2).toString("hex")}`;
  const dir = path.join(runsDir(project.id), id);
  const r = { id, mod: project.id, modName: project.name, folder: project.folder, engine: project.engine, by, started: now(), state: "running",
    phase: "build", timeout: timeoutMs / 1000, agentOnFail: Boolean(agentOnFail) && by !== "agent", phases: [], errors: [], killed: [], inputs,
    why: String(why || "").trim().slice(0, 600) || undefined, screenshots, shotPlan: screenshots ? shotPlan : undefined, shots: [] };
  try {
    await fsp.mkdir(dir, { recursive: true });
    await saveRun(r);
  } catch (error) { releaseJob(); throw error; }
  active.set(project.id, r);
  execute(project, r, dir, timeoutMs).catch((error) => console.error(`test run ${id}: ${error.stack || error.message}`));
  return r;
}

async function execute(project, r, dir, timeoutMs) {
  const logs = { build: cappedLog(path.join(dir, "build.log")), check: cappedLog(path.join(dir, "check.log")),
    run: cappedLog(path.join(dir, "run.log")), baseline: cappedLog(path.join(dir, "baseline.log")) };
  const scratch = path.join(TEST, project.id);
  const phase = async (name, fn) => {
    r.phase = name;
    const ph = { name, started: now() };
    r.phases.push(ph);
    await saveRun(r);
    try { const out = await fn(); ph.state = "ok"; return out; }
    catch (error) { ph.state = "failed"; ph.error = error.message; throw error; }
    finally { ph.finished = now(); }
  };
  let p = project;
  try {
    // 1. Build (same steps as the Build button; the mod's build.log is updated too).
    await phase("build", async () => {
      const steps = adapterOf(p).build(p);
      p.steps.build = { state: "running", started: now() };
      let error = "";
      try {
        for (const step of steps) {
          if (typeof step === "function") await step(logs.build);
          else await runCmd(step, logs.build, path.join(MODS, p.id));
        }
      } catch (e) { error = e.message; }
      logs.build.write(error ? `\nFAILED: ${error}\n` : "\nDONE\n");
      p = (await readProject(p.id)) || p;
      p.steps.build = { state: error ? "failed" : "done", started: p.steps.build?.started || r.started, finished: now(), error };
      await saveProject(p);
      await fsp.copyFile(path.join(dir, "build.log"), path.join(MODS, p.id, "build.log")).catch(() => {});
      r.buildFinished = p.steps.build.finished;
      if (error) { r.errors.push(`[build] ${error}`); throw new Error(`Build failed: ${error}`); }
    });
    // 2 + 3. Static checks and the headless run (engine specific).
    const unsupported = headlessSupport(p);
    const runner = RUNNERS[p.engine];
    if (unsupported || !runner) {
      r.state = "static";
      r.summary = `Built and syntax-checked. ${unsupported || "No headless runner for this engine."}`;
    } else {
      await fsp.rm(scratch, { recursive: true, force: true });
      await fsp.mkdir(scratch, { recursive: true });
      const ctx = { dir, scratch, timeoutMs, errors: [], check: logs.check, run: logs.run, baseline: logs.baseline, shots: r.screenshots, shotPlan: r.shotPlan,
        note: (text) => { r.notes = [...(r.notes || []), text]; } };
      // Screenshots show up on the page while the run is still going.
      const live = r.screenshots && setInterval(() => { r.shots = [...(ctx.baseShots || []), ...(ctx.modShots || [])]; }, 2000);
      let result;
      try {
        result = await phase("run", () => runner(p, ctx));
      } catch (error) {
        if (!error.unsupported) throw error;
        r.state = "static";
        r.summary = `Built and syntax-checked. ${error.message}`;
      } finally {
        clearInterval(live);
        r.shots = [...(ctx.baseShots || []), ...(ctx.modShots || [])];
      }
      if (result) {
        r.baselineErrorCount = ctx.baselineErrorCount ?? null;
        r.errors.push(...ctx.errors.slice(0, 200));
        if (ctx.errorDetails?.length) r.errorDetails = ctx.errorDetails;
        r.coverage = ctx.coverage || null;
        r.proof = await proofSeen(p, dir);
        if (ctx.errors.length) {
          r.state = "failed";
          r.summary = `${ctx.errors.length} new error${ctx.errors.length === 1 ? "" : "s"} ${result.phase === "check" ? "in the syntax check" : `while the game ran ${r.screenshots ? "on a virtual display" : "headless"}`}: ${ctx.errors[0].slice(0, 200)}`;
          const il = r.errorDetails?.find((d) => d.il)?.il;
          if (il) r.summary += `\nIL at the failing instruction (run.json errorDetails[].il):\n${il.slice(0, 1800)}`;
        } else if (ctx.inconclusive) {
          r.state = "inconclusive";
          r.summary = ctx.inconclusive;
          if (ctx.runtimeLimit) {
            // The test environment, not the mod, is what stops here: the next run would say the same.
            r.limit = "runtime";
            r.summary += " This is a limit of the test runtime, not a mod error: running the test again will give the same" +
              " result until the mod's files or the installed REA engines change. Treat the syntax check as the available" +
              " verification, record this blocker, and ask the user for an in-game test.";
          }
        } else {
          r.state = "passed";
          r.summary = `Built, syntax OK, ran ${r.screenshots ? "on a virtual display" : "headless"} for ${Math.round((ctx.ranMs || timeoutMs) / 1000)}s with no new errors` +
            (r.coverage?.total ? ` (${r.coverage.loaded.length} of ${r.coverage.total} mod file(s) loaded)` : "") +
            (r.screenshots ? `, ${r.shots.filter((x) => !x.base).length} screenshot(s)` : "") + ".";
          // No errors is not the goal working: say which milestones the run actually reached.
          r.summary += r.proof.length ? ` [PROOF] milestones seen (${r.proof.length}): ${r.proof.join(" | ")}. Milestones not listed here did not happen in this run.`
            : ` No [PROOF ${proofTag(p)}] line appeared, so no goal milestone was shown to happen.`;
        }
      }
    }
  } catch (error) {
    r.state = "failed";
    r.summary ||= error.message.slice(0, 300);
    if (!r.errors.length) r.errors.push(error.message);
  } finally {
    // Every process this run started (game, engine, browser) goes, whatever the verdict.
    r.killed = await killLeftovers([scratch, dir, installDir(p)]);
    if (r.killed.length) logs.run.write(`\nKilled ${r.killed.length} leftover game process(es): ${r.killed.map((k) => `${k.pid} ${k.cmd}`).join("; ")}\n`);
    await Promise.all(Object.values(logs).map((l) => l.close()));
    for (const name of ["check.log", "run.log", "baseline.log"])
      if ((await fsp.stat(path.join(dir, name)).catch(() => null))?.size === 0) await fsp.rm(path.join(dir, name), { force: true });
    r.finished = now();
    r.phase = "";
    // Only into the mod's own agent session: a failed run of a mod nobody started an agent for opened a
    // new /goal session on its own (a throwaway self-test mod, 2026-10-17) that then queued for the GPU.
    if ((r.state === "failed" || (r.state === "inconclusive" && r.limit !== "runtime")) && r.agentOnFail && p.session) {
      try { r.agent = await askAgent(p, r); } catch (error) { r.agent = { error: error.message }; }
    }
    await writeDigest(p, r, dir);
    await saveRun(r);
    const fresh = (await readProject(p.id)) || p;
    fresh.testRun = { id: r.id, state: r.state, at: r.finished, buildFinished: r.buildFinished || 0, summary: r.summary };
    await saveProject(fresh);
    active.delete(p.id);
    await fsp.rm(scratch, { recursive: true, force: true }).catch(() => {});
    releaseJob();
  }
}

// ------------------------------------------------------------------------- agent on fail --
export const FIX_GOAL = "the build artifact has the correct executable or pack format, all added scripts and scenes validate, " +
  "and an REA test run exercises the mod without new errors; if an external runtime or game asset prevents this, record the exact blocker and leave the goal incomplete";

// Same rule as proofRule() in static/app.js: milestones leave a greppable line in the game's log.
export function proofRule(p) {
  const tag = p.className || p.name || p.id;
  return `Proof lines: every milestone of the goal (mod loaded, each feature's entry point reached, each feature actually doing its job, e.g. level loaded, 12 agents spawned, round 3 started, item bought) writes ONE log line "[PROOF ${tag}] <milestone>: <concrete values>" through the engine's log (BepInEx Logger.LogInfo or Debug.Log, Godot print(), JS console.log, Lua print(), Ren'Py renpy.log). Write it where the effect happens (after the spawn returned, inside the patched method, when the state changed), never up front at startup, and never for something that didn't happen. PLAN.md lists each feature's proof line. After every test run or play test, grep its logs (game.log, run.log, player log) for "[PROOF ${tag}]" and report per milestone: seen (quote the line) or missing. A feature without its proof line in a passing run is unproven: say so, never claim it works.`;
}

async function askAgent(p, r) {
  const api = "http://127.0.0.1:8976/REA/api";
  const text = `/goal infinite Fix the mod "${p.name}" (REA mod project ${p.id}, game "${p.gameName}", engine ${p.engine}) until ${FIX_GOAL}.\n\n` +
    `REA test run ${r.id} ${r.state.toUpperCase()}: ${r.summary}\n` +
    `New errors (compared with the unmodified game):\n${r.errors.slice(0, 25).map((e) => {
      const trace = (r.errorDetails || []).find((d) => d.error === e.slice(0, 300))?.trace || [];
      return `- ${e.slice(0, 300)}${trace.length ? `\n    ${trace.slice(0, 8).join("\n    ")}` : ""}`;
    }).join("\n")}\n\n` +
    `Logs: /opt/data/rocsy-workspace/rea-mods/${p.id}/runs/${r.id}/ (build.log, check.log, run.log, baseline.log, game.log).\n` +
    `Use the skills rocsy/rea-modding and rocsy/rea-knowledge (read /opt/data/skills/rocsy/rea-modding/SKILL.md first; KB KEY ${kbKey({ name: path.basename(p.gamePath), path: p.gamePath })}). ` +
    `Find the cause in the logs and the game's decompiled code, fix src/, then start a new test run yourself: ` +
    `\`$R reamod test ${p.id} --why "<what you changed> -> <what you expect now>"\`, then \`$R wait\` and end your turn (you are resumed with the verdict). ` +
    `No blind reruns: if a run doesn't show what you expected, read its logs and trace the code before changing anything again. Inspect inconclusive results and address their cause, including installing the compatible REA engine through Core Backend when available. ` +
    `Repeat until a run passes or record a specific external blocker. Record what you fixed in the knowledge base. ` +
    `Mark the goal done only with a passing run id and verified artifact format. Never install into the real game folder.\n` +
    proofRule(p) +
    await relevantFiles(p).then((f) => relevantBrief(p, f)).then((b) => b ? `\n${b}` : "", () => "") +
    await engineBrief(p).then((b) => b ? `\n\n${b}` : "", () => "") +
    await listTools().then((tools) => toolsBrief(tools, p.engine)).then((b) => b ? `\n${b}` : "", () => "");
  const item = await queueAgent({ key: `testfail:${p.id}`, kind: "testfail", session: p.session || "", text, model: await modelFor("refine"),
    meta: { mod: p.id, run: r.id, gamePath: p.gamePath, name: p.name } });
  return { queued: item.id, at: now() };
}

// The Agent picked the request up: link the session to the run, the mod and the game's Sessions tab.
onDelivered("testfail", async (item, session) => {
  const { mod, run, gamePath, name } = item.meta;
  const file = path.join(runsDir(mod), run, "run.json");
  try {
    const r = JSON.parse(await fsp.readFile(file, "utf8"));
    r.agent = { ...(r.agent || {}), session, delivered: now() };
    await fsp.writeFile(file, JSON.stringify(r, null, 1));
  } catch {}
  const p = await readProject(mod);
  if (p && !p.session) { p.session = session; await saveProject(p); }
  if (p?.session === session) await addSession(gamePath, { id: session, kind: "mod", label: `Mod: ${name}`, mod });
  else await addSession(gamePath, { id: session, kind: "test", label: `Fix test run: ${name}`, mod });
});

// ------------------------------------------------------------------------------- routes --
async function runSummaries(id) {
  const out = [];
  for (const rid of await fsp.readdir(runsDir(id)).catch(() => [])) {
    if (!/^[0-9TZ-]+-[0-9a-f]{4}$/.test(rid)) continue;
    try {
      const r = active.get(id)?.id === rid ? active.get(id) : JSON.parse(await fsp.readFile(path.join(runsDir(id), rid, "run.json"), "utf8"));
      // REA restarted mid-run: it will never finish.
      if (r.state === "running" && active.get(id)?.id !== rid) Object.assign(r, { state: "failed", phase: "", summary: r.summary || "Interrupted: REA restarted during this run" });
      const files = (await fsp.readdir(path.join(runsDir(id), rid)).catch(() => [])).filter((f) => f !== "run.json" && f !== "shots");
      out.push({ ...r, errors: r.errors?.slice(0, 50), errorCount: r.errors?.length || 0, files });
    } catch {}
  }
  return out.sort((a, b) => b.started - a.started);
}

export async function handleTestRuns({ method, api, url, req, res, send, readJson, httpError, targets }) {
  // Every log of one game's mods: test runs + the current decompile/build/loader logs.
  if (api === "/mods/logs") {
    const folder = targets[url.searchParams.get("folder")];
    if (!folder || folder.type !== "folder") throw httpError(404, "Unknown game folder");
    const projects = [];
    for (const id of await fsp.readdir(MODS).catch(() => [])) {
      const p = /^[0-9a-f]{12}$/.test(id) ? await readProject(id) : null;
      if (p && (p.folder === folder.id || p.gamePath === folder.path)) projects.push(p);
    }
    if (method === "GET") {
      const runs = [], jobLogs = [];
      for (const p of projects) {
        runs.push(...await runSummaries(p.id));
        for (const name of LOGS) {
          const st = await fsp.stat(path.join(MODS, p.id, name)).catch(() => null);
          if (st) jobLogs.push({ mod: p.id, modName: p.name, name, size: st.size, mtime: st.mtimeMs });
        }
      }
      send(res, 200, { runs: runs.sort((a, b) => b.started - a.started), jobLogs: jobLogs.sort((a, b) => b.mtime - a.mtime) });
      return true;
    }
    if (method === "DELETE") {
      let deleted = 0;
      for (const p of projects) {
        for (const rid of await fsp.readdir(runsDir(p.id)).catch(() => [])) {
          if (active.get(p.id)?.id === rid) continue;
          await fsp.rm(path.join(runsDir(p.id), rid), { recursive: true, force: true });
          deleted++;
        }
        for (const name of LOGS) if (await fsp.rm(path.join(MODS, p.id, name)).then(() => true, () => false)) deleted++;
      }
      send(res, 200, { deleted });
      return true;
    }
  }
  // One screenshot of a run.
  const shot = api.match(/^\/mods\/([0-9a-f]{12})\/runs\/([0-9TZ]+-[0-9a-f]{4})\/shots\/((?:base-)?\d{2}\.png)$/);
  if (shot && (method === "GET" || method === "HEAD")) {
    const file = path.join(runsDir(shot[1]), shot[2], "shots", shot[3]);
    const st = await fsp.stat(file).catch(() => null);
    if (!st?.isFile()) throw httpError(404, "Unknown screenshot");
    res.writeHead(200, { "Content-Type": "image/png", "Content-Length": st.size, "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff" });
    if (method === "HEAD") res.end(); else fs.createReadStream(file).pipe(res);
    return true;
  }
  const match = api.match(/^\/mods\/([0-9a-f]{12})\/(testrun|runs|logs)(?:\/([0-9TZa-f-]{6,40}|[a-z]+\.log))?$/);
  if (!match) return false;
  const project = await readProject(match[1]);
  if (!project) throw httpError(404, "Unknown mod");
  project.engine ||= "unity-mono";
  const [, , what, sub] = match;
  if (what === "testrun" && method === "POST" && !sub) {
    const body = await readJson(req);
    if (!exists(project.gamePath)) throw httpError(410, "The game folder is not mounted any more");
    // Who asked: the page comes through the gateway from a browser (Origin / Sec-Fetch headers); anything
    // else (curl from the agent, the Agent service) is the agent, whatever its body says.
    const browser = Boolean(req.headers.origin || req.headers["sec-fetch-site"] || req.headers["sec-fetch-mode"]);
    const by = browser && body.by !== "agent" ? "user" : "agent";
    const r = await startTestRun(project, { timeout: body.timeout, agentOnFail: body.agentOnFail !== false, by,
      screenshots: body.screenshots === true || (by === "agent" && body.screenshots !== false), why: body.why });   // the agent's runs take 3 frames (see startTestRun)
    send(res, 202, r);
    return true;
  }
  if (what === "runs" && method === "GET" && !sub) { send(res, 200, { runs: await runSummaries(project.id), active: active.get(project.id) || null }); return true; }
  if (what === "runs" && sub && /^[0-9TZ-]+-[0-9a-f]{4}$/.test(sub)) {
    const rdir = path.join(runsDir(project.id), sub);
    if (method === "GET") {
      const r = active.get(project.id)?.id === sub ? active.get(project.id) : await fsp.readFile(path.join(rdir, "run.json"), "utf8").then(JSON.parse).catch(() => null);
      if (!r) throw httpError(404, "Unknown run");
      send(res, 200, { ...r, files: (await fsp.readdir(rdir).catch(() => [])).filter((f) => f !== "run.json" && f !== "shots") });
      return true;
    }
    if (method === "DELETE") {
      if (active.get(project.id)?.id === sub) throw httpError(409, "This run is still going");
      await fsp.rm(rdir, { recursive: true, force: true });
      send(res, 200, { ok: true });
      return true;
    }
  }
  if (what === "logs" && method === "DELETE" && LOGS.has(sub)) {
    await fsp.rm(path.join(MODS, project.id, sub), { force: true });
    send(res, 200, { ok: true });
    return true;
  }
  return false;
}

// Shared with the Decompiled tab's build check (buildcheck.mjs).
export { pckVersion, godotBinary, godotSteamBinary, stageGame, ilAt, writeDigest };
