// "Validate build": is it complete, and does it actually work? Three subjects, one engine set:
//   decompiled  (Decompiled tab) the game's recovered output vs what the game ships
//   mod         (mod Overview)   the built mod: every source file packaged, the whole patched game
//                                compiles, and the patched game runs like the original
//   rewrite     (rewrite Overview) the standalone reimplementation: parity checklist, compiles, runs
// The checks per subject:
//   completeness  Godot pack listing, Unity assemblies, Ren'Py .rpyc, Ghidra functions done/total,
//                 stale/failed records; mods: packaged files + build newer than the source; rewrites: PARITY.json
//   compile       Godot: the project starts normally (autoloads first) with a checker scene that load()s
//                 every script on the game's own engine version; Unity/.NET: dotnet build against the
//                 game's assemblies; JS/Lua/Python/Java: parsers / javac
//   run           Godot: a bounded run next to the original pack (decompiled, mod) or on its own (rewrite);
//                 other mods: the latest Test run of the current build
// One check at a time in the CPU job slot shared with mod builds/test runs.
// Results: build-check.json + build-check.log in rea-kb/<KEY>/decompiled/, rea-mods/<id>/ or rea-rewrites/<id>/.
import { spawn } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { kbDir, outDir, linkTree, lookup as lookupDecomp } from "./decomp.mjs";
import { validateSource, languageOf } from "./validate.mjs";

const RUN_MS = 25000;
const MAX_LIST = 200;
const MODS = "/opt/data/rocsy-workspace/rea-mods";
const REWRITES = "/opt/data/rocsy-workspace/rea-rewrites";
const ENGINES = path.join(process.env.REA_TEST_DIR || "/home/node/rea-test", "engines");
const running = new Map(); // subject id → report while it runs
const exists = (p) => fs.existsSync(p);

async function progress(key) {
  try { return JSON.parse(await fsp.readFile(path.join(kbDir(key), "decompiled", "progress.json"), "utf8")).binaries || {}; }
  catch { return {}; }
}

async function current(gamePath, bin) {
  const st = await fsp.stat(path.join(gamePath, bin.rel)).catch(() => null);
  if (!st) return "missing";
  return !st.isDirectory() && (st.size !== bin.size || Math.round(st.mtimeMs) !== bin.mtime) ? "stale" : "ok";
}

async function walk(dir, test, out = [], base = dir, cap = 50000, skipDir = () => false) {
  for (const e of await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])) {
    if (out.length >= cap) break;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) { if (!skipDir(e.name)) await walk(full, test, out, base, cap, skipDir); }
    else if (e.isFile() && test(e.name)) out.push(path.relative(base, full).replaceAll(path.sep, "/"));
  }
  return out;
}

async function newestMtime(dir) {
  let newest = 0;
  for (const f of await walk(dir, () => true, [], dir, 100000)) newest = Math.max(newest, (await fsp.stat(path.join(dir, f)).catch(() => null))?.mtimeMs || 0);
  return newest;
}

// A bounded process in its own group (killed with its children), output collected as lines.
function exec(cmd, args, { cwd, env = {}, timeoutMs = 120000, log } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn("nice", ["-n", "10", cmd, ...args], { cwd, env: { ...process.env, ...env }, detached: true, stdio: ["ignore", "pipe", "pipe"] });
    let text = "", timedOut = false;
    const feed = (d) => { if (text.length < 4 * 1024 * 1024) text += d; };
    child.stdout.on("data", feed);
    child.stderr.on("data", feed);
    const kill = () => { try { process.kill(-child.pid, "SIGKILL"); } catch {} };
    const timer = setTimeout(() => { timedOut = true; kill(); }, timeoutMs);
    const done = (code) => {
      clearTimeout(timer);
      kill();
      if (log && text) log.write(text.length > 20000 ? `${text.slice(0, 10000)}\n[…]\n${text.slice(-10000)}` : text);
      resolve({ code, timedOut, ms: Date.now() - started, lines: text.split("\n") });
    };
    child.on("error", (e) => { text += e.message; done(-1); });
    child.on("close", done);
  });
}

async function pool(items, size, fn) {
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, async () => { while (i < items.length) await fn(items[i++]); }));
}

const check = (status, detail, extra = {}) => ({ status, detail, ...extra });
const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
const skipped = (detail = "") => check("skip", detail);

// --------------------------------------------------------------------------- godot engine --
// What a Godot pack entry is recovered as: X.gdc → X.gd, X.tscn.remap → X.tscn, X.png.import → X.png
// (the source asset; GDRE converts the imported texture back), project.binary → project.godot.
function godotExpected(packPath) {
  const p = packPath.replace(/^res:\/\//, "");
  if (p.startsWith(".import/") || p.startsWith(".godot/")) return null;
  if (/\.gd\.remap$/.test(p)) return null; // counted through its .gdc
  if (/\.(gdc|gde)$/.test(p)) return { want: p.replace(/\.(gdc|gde)$/, ".gd"), kind: "scripts" };
  if (p === "project.binary") return { want: "project.godot", kind: "other" };
  const file = p.replace(/\.remap$/, "").replace(/\.import$/, "");
  const ext = path.extname(file).toLowerCase();
  const kind = ext === ".gd" ? "scripts" : [".tscn", ".scn"].includes(ext) ? "scenes"
    : p.endsWith(".import") ? "assets" : [".tres", ".res", ".material", ".mesh", ".shader", ".gdshader", ".theme", ".anim"].includes(ext) ? "resources" : "other";
  return { want: file, kind };
}

const GODOT_ERROR = /^(SCRIPT ERROR|ERROR|USER ERROR|USER SCRIPT ERROR|Parse Error|Parser Error|handle_crash|CrashHandlerException)|Segmentation fault|Invalid call\.|Attempt to call function/;
function godotErrors(lines) {
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (!GODOT_ERROR.test(lines[i].trim())) continue;
    const at = /^\s*[Aa]t: /.test(lines[i + 1] || "") ? ` ${lines[i + 1].trim()}` : "";
    out.push(lines[i].trim() + at);
  }
  return out;
}
const norm = (l) => l.replace(/0x[0-9a-f]+/gi, "0x?").replace(/\b\d+(\.\d+)?\s*(ms|s)\b/g, "?").replace(/\s+/g, " ").trim().slice(0, 300);
const unseen = (errors, known) => { const k = new Set(known.map(norm)), seen = new Set(); return errors.filter((e) => { const n = norm(e); if (k.has(n) || seen.has(n)) return false; seen.add(n); return true; }); };

// The Godot binary for version v (GodotSteam when the game was built with it), downloaded on demand.
export async function godotEngine(ctx, v, steam) {
  const testrun = await import("./testrun.mjs");
  const bin = steam ? await testrun.godotSteamBinary(v, ctx.log) : await testrun.godotBinary(v, ctx.log);
  const env = { HOME: path.join(ctx.scratch, "home"), SDL_AUDIODRIVER: "dummy", ...(steam ? { LD_LIBRARY_PATH: path.dirname(bin) } : {}) };
  if (steam) { // GodotSteam is an editor build: it needs an X display even for --check-only
    if (!ctx.display) ctx.display = await (await import("./shots.mjs")).startDisplay(ctx.scratch, ctx.log);
    env.DISPLAY = ctx.display.display;
  }
  return { bin, env, v, label: `Godot ${v.version}${steam ? " (GodotSteam)" : ""}`,
    headless: v.major >= 4 && !steam ? ["--headless"] : [], audio: steam || v.major < 4 ? ["--audio-driver", "Dummy"] : [] };
}

// Compile every script of a staged project inside the running project: it starts normally (autoload
// singletons first, as in the game) with a checker scene as the main scene that load()s each script.
// Per-file --check-only can't do this: it reports every script touching mutually-referencing
// singletons as a cyclic reference. Returns broken scripts (res:// path → errors) and load warnings.
export async function godotCompileAll(ctx, eng, stage, files) {
  const { v, bin, env, headless, audio } = eng;
  await fsp.writeFile(path.join(stage, "__rocsy_list.txt"), files.map((f) => `res://${f}`).join("\n") + "\n");
  await fsp.writeFile(path.join(stage, "__rocsy_check.gd"), v.major >= 4
    ? `extends Node\nfunc _ready():\n\tvar f = FileAccess.open("res://__rocsy_list.txt", FileAccess.READ)\n\twhile not f.eof_reached():\n\t\tvar p = f.get_line().strip_edges()\n\t\tif p == "":\n\t\t\tcontinue\n\t\tprint("ROCSY_CHECK ", p)\n\t\tvar s = load(p)\n\t\tif s == null or not (s is Script) or not s.can_instantiate():\n\t\t\tprint("ROCSY_FAIL ", p)\n\tprint("ROCSY_DONE")\n\tget_tree().quit()\n`
    : `extends Node\nfunc _ready():\n\tvar f = File.new()\n\tf.open("res://__rocsy_list.txt", File.READ)\n\twhile not f.eof_reached():\n\t\tvar p = f.get_line().strip_edges()\n\t\tif p == "":\n\t\t\tcontinue\n\t\tprint("ROCSY_CHECK ", p)\n\t\tvar s = load(p)\n\t\tif s == null or not (s is Script) or not s.can_instance():\n\t\t\tprint("ROCSY_FAIL ", p)\n\tprint("ROCSY_DONE")\n\tget_tree().quit()\n`);
  await fsp.writeFile(path.join(stage, "__rocsy_check.tscn"), v.major >= 4
    ? `[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Script" path="res://__rocsy_check.gd" id="1"]\n\n[node name="RocsyCheck" type="Node"]\nscript = ExtResource("1")\n`
    : `[gd_scene load_steps=2 format=2]\n\n[ext_resource path="res://__rocsy_check.gd" type="Script" id=1]\n\n[node name="RocsyCheck" type="Node"]\nscript = ExtResource( 1 )\n`);
  ctx.log.write(`\n--- compile every script inside the running project (${stage}) ---\n`);
  const r = await exec(bin, [...headless, ...audio, "--path", stage, "res://__rocsy_check.tscn"], { cwd: stage, env, timeoutMs: 5 * 60000, log: ctx.log });
  const broken = new Map(), reached = new Set(), failed = new Set();
  let cur = "";
  for (let i = 0; i < r.lines.length; i++) {
    const line = r.lines[i].trim();
    if (line.startsWith("ROCSY_CHECK ")) { cur = line.slice(12); reached.add(cur); continue; }
    if (line.startsWith("ROCSY_FAIL ")) { failed.add(line.slice(11)); if (!broken.has(line.slice(11))) broken.set(line.slice(11), []); continue; }
    if (cur && GODOT_ERROR.test(line)) {
      const at = /^\s*[Aa]t: /.test(r.lines[i + 1] || "") ? ` ${r.lines[i + 1].trim()}` : "";
      (broken.get(cur) || broken.set(cur, []).get(cur)).push(line + at);
    }
  }
  // Errors while loading a script that still compiled (e.g. a missing texture it preloads) are kept as
  // warnings; only scripts that failed to load count as broken.
  const notes = [];
  for (const [f, errors] of broken) if (!failed.has(f)) { notes.push(...errors.map((e) => `${f}: ${e}`)); broken.delete(f); }
  // Scripts the checker never reached (the project quit or crashed first): compile those one by one.
  const missed = files.filter((f) => !reached.has(`res://${f}`));
  if (missed.length) {
    ctx.log.write(`\n${plural(missed.length, "script")} not reached by the checker scene${r.timedOut ? " (timed out)" : ""}: compiling them one by one with --check-only\n`);
    await fsp.writeFile(path.join(stage, "__rocsy_probe.gd"), "extends Node\n");
    const compile = (f) => exec(bin, [...headless, "--path", stage, "--check-only", "-s", `res://${f}`], { cwd: stage, env, timeoutMs: 60000 });
    const probe = await compile("__rocsy_probe.gd");
    const noise = godotErrors(probe.lines);
    await pool(missed, Math.max(1, Math.min(4, os.cpus().length >> 1)), async (f) => {
      const one = await compile(f);
      const errors = unseen(godotErrors(one.lines), noise);
      if (errors.length || one.timedOut || (one.code !== 0 && one.code !== probe.code)) broken.set(`res://${f}`, errors.length ? errors : [one.timedOut ? "timed out" : `exit code ${one.code}`]);
    });
  }
  return { broken, notes, missed: missed.length };
}

const brokenList = (broken) => [...broken].sort((a, b) => a[0].localeCompare(b[0]))
  .flatMap(([f, errors]) => (errors.length ? errors : ["failed to load"]).slice(0, 5).map((e) => `${f}: ${e}`));

// A bounded run; what it printed, loaded and whether it quit early.
async function godotPlay(ctx, eng, cwd, args, label) {
  ctx.log.write(`\n--- ${label} ---\n`);
  const r = await exec(eng.bin, [...eng.headless, ...eng.audio, "--verbose", ...args], { cwd, env: eng.env, timeoutMs: RUN_MS, log: ctx.log });
  return { early: !r.timedOut, code: r.code, ms: r.ms, errors: godotErrors(r.lines),
    steamQuit: r.lines.some((l) => /Failed to initialize Steam|SteamAPI_IsSteamRunning\(\) did not locate/.test(l)),
    loaded: new Set(r.lines.map((l) => /^Loading resource: (res:\/\/\S+)/.exec(l)?.[1]).filter(Boolean)) };
}

// Run `args` (the recovered project / the modded pack) next to the original pack and compare.
async function godotCompare(ctx, eng, cwd, args, originalPack, what) {
  const mine = await godotPlay(ctx, eng, cwd, args, what);
  const theirs = await godotPlay(ctx, eng, cwd, ["--main-pack", originalPack], "original game");
  const newErrors = unseen(mine.errors, theirs.errors);
  const notLoaded = [...theirs.loaded].filter((r) => !mine.loaded.has(r) && !mine.loaded.has(r.replace(/\.(gdc|gde)$/, ".gd")));
  const loadText = `${theirs.loaded.size - notLoaded.length}/${theirs.loaded.size} resources the original loaded also loaded from the ${what}`;
  const both = mine.early && theirs.early;
  return { mine, theirs, run: check(newErrors.length || (mine.early && !theirs.early) ? "fail" : notLoaded.length || both ? "warn" : "pass",
    newErrors.length ? `The ${what} prints ${plural(newErrors.length, "error")} the original game doesn't; ${loadText}.`
      : mine.early && !theirs.early ? `The ${what} exited after ${Math.round(mine.ms / 1000)}s (code ${mine.code}) while the original kept running; ${loadText}.`
      : both && mine.steamQuit && theirs.steamQuit ? `Both stop at Steam initialization (no Steam in the REA container), so only startup could be compared: ${loadText}, no new errors. Gameplay needs a real launch (Mods → Play).`
      : both ? `Both exited early (${what} after ${Math.round(mine.ms / 1000)}s, original after ${Math.round(theirs.ms / 1000)}s) with no new errors; ${loadText}.`
      : `Ran ${RUN_MS / 1000}s like the original with no new errors; ${loadText}.`,
    { errors: newErrors.slice(0, MAX_LIST), missing: notLoaded.slice(0, MAX_LIST).map((r) => `not loaded: ${r}`) }) };
}

// The recovered project's GDRE report: failed conversions, scripts it couldn't decompile, GodotSteam.
async function gdreReport(recovered) {
  const report = await fsp.readFile(path.join(recovered, "gdre_export.log"), "utf8").catch(() => "");
  return { steam: /Godot editor version .*Steam edition/i.test(report),
    failedConv: report.split(/\nFailed conversions:\s*\n/)[1]?.split(/\n\s*\n/)[0]?.split("\n").filter((l) => /^\* res:\/\//.test(l)).map((l) => l.slice(2).trim()) || [],
    notDecompiled: Number(report.match(/Scripts not decompiled:\s*(\d+)/)?.[1] || 0) };
}

// ------------------------------------------------------------------------------ .NET / misc --
const CS_ERROR = /^(.+?)\((\d+),(\d+)\): error (CS\d+): (.+?)(?: \[[^\]]+\])?$/;
// dotnet build of projFile (already writable, not a hard link into stored output). ILSpy lists only
// directly used assemblies; facades the compiler asks for (CS0012, e.g. UnityEngine) are added from
// `managed` and the build is retried.
async function dotnetBuild(ctx, projFile, managed) {
  const work = path.dirname(projFile);
  const text = await fsp.readFile(projFile, "utf8");
  const extra = new Set();
  let r;
  for (let pass = 0; pass < 4; pass++) {
    const refs = [...extra].map((n) => `<Reference Include="${n}"><HintPath>${path.join(managed, `${n}.dll`)}</HintPath></Reference>`).join("");
    if (pass) await fsp.writeFile(projFile, refs ? text.replace("</Project>", `<ItemGroup>${refs}</ItemGroup></Project>`) : text);
    r = await exec("dotnet", ["build", projFile, "-nologo", "-clp:ErrorsOnly", "-o", path.join(ctx.scratch, "dotnet-out", path.basename(projFile)), "-p:TreatWarningsAsErrors=false"],
      { cwd: work, timeoutMs: 15 * 60000, log: ctx.log });
    const wanted = managed ? new Set(r.lines.map((l) => / error CS0012: .*reference to assembly '([^',]+)/.exec(l)?.[1])
      .filter((n) => n && !extra.has(n) && exists(path.join(managed, `${n}.dll`)))) : new Set();
    if (!wanted.size) break;
    for (const n of wanted) extra.add(n);
    ctx.log.write(`(added missing references: ${[...wanted].join(", ")}; building again)\n`);
  }
  const errors = [...new Set(r.lines.map((l) => CS_ERROR.exec(l.trim())).filter(Boolean)
    .map(([, file, line, col, code, msg]) => `${path.relative(work, path.resolve(work, file))}:${line}:${col} ${code} ${msg}`))];
  const codes = {};
  for (const e of errors) { const c = e.split(" ")[1]; codes[c] = (codes[c] || 0) + 1; }
  const ok = r.code === 0 && !errors.length;
  return { ok, errors, timedOut: r.timedOut,
    summary: r.timedOut ? "dotnet build timed out after 15 minutes."
      : errors.length ? `${plural(errors.length, "compile error")} (${Object.entries(codes).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([c, n]) => `${c} ×${n}`).join(", ")})`
      : ok ? "" : `dotnet build failed (exit ${r.code}) without compiler errors: see the log.` };
}

// Syntax of plain source files (JS/Lua/Python/GDScript structure…) through validate.mjs.
async function syntaxCheck(ctx, root, files) {
  const errors = [];
  let parsed = 0;
  await pool(files, 4, async (f) => {
    const full = path.join(root, f);
    const st = await fsp.stat(full).catch(() => null);
    if (!st || st.size > 2 * 1024 * 1024) return;
    const r = await validateSource(f, await fsp.readFile(full, "utf8"));
    if (r.parser.startsWith("unavailable")) return;
    parsed++;
    for (const e of r.errors) errors.push(`${f}:${e.line}:${e.column} ${e.message}`);
  });
  return check(!parsed ? "skip" : errors.length ? "fail" : "pass",
    parsed ? `${parsed - new Set(errors.map((e) => e.split(":")[0])).size}/${parsed} files parse.` : "No parser available for these files.",
    { checked: parsed, failed: errors.length, errors: errors.sort().slice(0, MAX_LIST) });
}

// ------------------------------------------------------------------- decompiled (Decompiled tab) --
async function godotUnit(ctx, bin, pckRel) {
  const { key, gamePath } = ctx;
  const unit = { rel: pckRel, label: "Godot game pack", tool: "gdre" };
  const recovered = outDir(key, pckRel, "gdre");
  const t = bin?.tools?.gdre;
  if (!t || !exists(path.join(recovered, "project.godot"))) {
    unit.complete = check("fail", "The pack hasn't been recovered yet: run the decompile step of a mod project (Mods tab) or Continue decompiling.");
    unit.compile = skipped("Nothing to compile."); unit.run = skipped("Nothing to run.");
    return unit;
  }
  // 1. Completeness: every script, scene, resource and asset in the pack has a recovered file.
  ctx.step(`${pckRel}: comparing the pack's file list with the recovered project`);
  const { godotPackFiles } = await import("./mods.mjs");
  const groups = {};
  const gamePack = await (await import("./mods.mjs")).originalFile({ gamePath, pckRel });
  for (const p of await godotPackFiles(gamePack)) {
    const e = godotExpected(p);
    if (!e) continue;
    const g = (groups[e.kind] ||= { expected: 0, recovered: 0, missing: [] });
    g.expected++;
    if (exists(path.join(recovered, e.want))) g.recovered++;
    else if (g.missing.length < MAX_LIST) g.missing.push(e.want);
  }
  const { steam, failedConv, notDecompiled } = await gdreReport(recovered);
  const totals = Object.values(groups).reduce((a, g) => [a[0] + g.expected, a[1] + g.recovered], [0, 0]);
  const scripts = groups.scripts || { expected: 0, recovered: 0, missing: [] };
  const missing = Object.entries(groups).flatMap(([k, g]) => g.missing.map((m) => `${k}: res://${m}`));
  const stale = ctx.state(bin) !== "ok";
  unit.complete = check(stale || scripts.recovered < scripts.expected || notDecompiled ? "fail" : missing.length || failedConv.length || t.status !== "complete" ? "warn" : "pass",
    `${stale ? "STALE: the game changed since it was recovered. " : ""}${scripts.recovered}/${scripts.expected} scripts, ` +
    Object.entries(groups).filter(([k]) => k !== "scripts").map(([k, g]) => `${g.recovered}/${g.expected} ${k}`).join(", ") +
    ` recovered (${Math.round(100 * totals[1] / Math.max(1, totals[0]))}% of ${plural(totals[0], "file")})` +
    `${failedConv.length ? `; ${plural(failedConv.length, "failed resource conversion")}` : ""}${notDecompiled ? `; ${notDecompiled} scripts GDRE couldn't decompile` : ""}.`,
    { groups: Object.fromEntries(Object.entries(groups).map(([k, g]) => [k, { expected: g.expected, recovered: g.recovered }])),
      missing: [...failedConv.map((f) => `failed conversion: ${f}`), ...missing.filter((x) => !failedConv.includes(x.replace(/^\w+: /, "")))].slice(0, MAX_LIST) });

  // 2. Compile every recovered GDScript with the engine the game ships with.
  let eng;
  try { eng = await godotEngine(ctx, await (await import("./testrun.mjs")).pckVersion(gamePack), steam); }
  catch (error) { unit.compile = check("warn", `No engine to compile with: ${error.message}. Engines: Manager → Core Backend → REA Engines.`); unit.run = skipped("No engine."); return unit; }
  const stage = path.join(ctx.scratch, "godot");
  await (await import("./testrun.mjs")).stageGame(recovered, null, stage);
  const files = await walk(recovered, (n) => n.endsWith(".gd"));
  ctx.step(`${pckRel}: compiling ${plural(files.length, "GDScript file")} with ${eng.label}`);
  const { broken, notes, missed } = await godotCompileAll(ctx, eng, stage, files);
  unit.compile = check(!files.length || broken.size ? "fail" : "pass",
    files.length ? `${files.length - broken.size}/${files.length} GDScript files compile with ${eng.label}` +
      `${missed ? ` (${missed} checked one by one: the project quit before the checker reached them)` : ""}` +
      `${notes.length ? `; ${plural(notes.length, "load warning")}` : ""}.` : "No GDScript files were recovered.",
    { checked: files.length, failed: broken.size, errors: [...brokenList(broken), ...notes.map((n) => `warning: ${n}`)].slice(0, MAX_LIST) });

  // 3. Run the recovered project next to the original pack and compare what each prints and loads.
  ctx.step(`${pckRel}: running the recovered project and the original game for ${RUN_MS / 1000}s each`);
  unit.run = (await godotCompare(ctx, eng, stage, ["--path", stage], gamePack, "recovered project")).run;
  return unit;
}

async function unityUnit(ctx, bin, rel, managed) {
  const unit = { rel, label: `.NET assembly ${path.basename(rel)}`, tool: "ilspy" };
  const out = outDir(ctx.key, rel, "ilspy");
  const t = bin?.tools?.ilspy;
  const csproj = exists(out) ? (await fsp.readdir(out)).find((f) => f.endsWith(".csproj")) : "";
  const sources = exists(out) ? await walk(out, (n) => n.endsWith(".cs")) : [];
  if (!t || !sources.length) {
    unit.complete = check("fail", "Not decompiled yet (ILSpy runs in a Unity mod project's decompile step, or Continue decompiling).");
    unit.compile = skipped("Nothing to compile."); unit.run = skipped();
    return unit;
  }
  const stale = ctx.state(bin) !== "ok";
  unit.complete = check(stale || t.status === "failed" ? "fail" : t.status !== "complete" || !csproj ? "warn" : "pass",
    `${stale ? "STALE: the game changed since it was decompiled. " : ""}ILSpy ${t.status}: ${plural(sources.length, "C# file")}${csproj ? ` + ${csproj}` : ", no project file"}.`);
  if (!csproj) { unit.compile = check("warn", "No .csproj in the ILSpy output, so it can't be built."); unit.run = skipped(); return unit; }
  // Build a hard-linked copy (sources untouched) with the references pointed at the game's assemblies.
  ctx.step(`${rel}: building the decompiled C# project with dotnet`);
  const work = path.join(ctx.scratch, "cs", path.basename(rel, ".dll"));
  await linkTree(out, work);
  const projFile = path.join(work, csproj);
  const text = (await fsp.readFile(projFile, "utf8")).replace(/<HintPath>([^<]*)<\/HintPath>/g, (m, hint) => {
    const name = hint.replace(/\\/g, "/").split("/").pop();
    return exists(path.join(managed, name)) ? `<HintPath>${path.join(managed, name)}</HintPath>` : m;
  });
  await fsp.rm(projFile); // a hard link: never write through it into the knowledge base
  await fsp.writeFile(projFile, text);
  const b = await dotnetBuild(ctx, projFile, managed);
  unit.compile = check(b.ok ? "pass" : "fail", b.ok ? `Compiles: ${plural(sources.length, "file")} build into ${path.basename(rel)} with dotnet.`
    : b.errors.length ? `${b.summary} in the decompiled C#.` : b.summary, { checked: sources.length, failed: b.errors.length, errors: b.errors.slice(0, MAX_LIST) });
  unit.run = skipped("Not run: swapping a rebuilt assembly into the game isn't automated yet. A clean compile is the strongest check here.");
  return unit;
}

// Any other recorded output: completeness from the record, syntax through the available parsers.
async function genericUnit(ctx, bin, tool) {
  const unit = { rel: bin.rel, label: `${bin.rel} (${tool})`, tool };
  const t = bin.tools[tool];
  const state = ctx.state(bin);
  if (tool === "ghidra" || tool === "peres") {
    unit.complete = check(state !== "ok" ? "fail" : t.status === "complete" ? "pass" : t.status === "failed" ? "fail" : "warn",
      `${state === "stale" ? "STALE: the game changed. " : state === "missing" ? "The file is gone from the game folder. " : ""}${tool === "ghidra"
        ? `${(t.done || 0).toLocaleString()}${t.total ? ` of ${t.total.toLocaleString()}` : ""} functions have pseudocode (${t.status})`
        : `embedded resources ${t.status}`}.`);
    unit.compile = skipped(tool === "ghidra" ? "Ghidra pseudocode is a reading aid: native code can't be recompiled from it." : "Resources, nothing to compile.");
    unit.run = skipped();
    return unit;
  }
  const out = outDir(ctx.key, bin.rel, tool);
  const files = exists(out) ? await walk(out, (n) => Boolean(languageOf(n)) && !/\.(tscn|tres|cs|h|hpp)$/i.test(n), [], out, 5000) : [];
  let detail = `${tool} ${t.status}: ${plural(files.length, "source file")}`;
  let status = state !== "ok" || t.status === "failed" ? "fail" : t.status === "complete" ? "pass" : "warn";
  if (tool === "unrpyc") {
    const compiled = await walk(path.join(ctx.gamePath, bin.rel), (n) => n.endsWith(".rpyc"), [], path.join(ctx.gamePath, bin.rel));
    const missing = compiled.filter((f) => !exists(path.join(out, f.replace(/\.rpyc$/, ".rpy"))) && !exists(path.join(ctx.gamePath, bin.rel, f.replace(/\.rpyc$/, ".rpy"))));
    detail = `${compiled.length - missing.length}/${compiled.length} .rpyc scripts have source`;
    if (missing.length) status = "fail";
    unit.complete = check(status, detail + ".", { missing: missing.slice(0, MAX_LIST) });
  } else unit.complete = check(status, `${state === "stale" ? "STALE: the game changed. " : ""}${detail}.`);
  if (!files.length) { unit.compile = skipped("No source files with an available parser."); unit.run = skipped(); return unit; }
  ctx.step(`${bin.rel}: syntax-checking ${plural(files.length, "file")}`);
  unit.compile = await syntaxCheck(ctx, out, files);
  unit.run = skipped("Not run: only Godot projects are run by this check (mods have Test run).");
  return unit;
}

async function decompiledUnits(ctx, m, r) {
  const bins = await progress(ctx.key);
  const states = new Map(await Promise.all(Object.values(bins).map(async (b) => [b.rel, await current(m.path, b)])));
  ctx.state = (b) => (b && states.get(b.rel)) || "ok";
  const { detectGame, SKIP_ASSEMBLY } = await import("./mods.mjs");
  const game = await detectGame(m.path);
  r.engine = game.label || game.engine;
  const done = new Set();
  const units = [];
  if (game.engine === "godot") {
    units.push(await godotUnit(ctx, bins[game.pckRel], game.pckRel));
    done.add(`${game.pckRel}:gdre`);
  }
  if (game.engine === "unity-mono") {
    const managedRel = path.relative(m.path, game.managed);
    const dlls = (await fsp.readdir(game.managed)).filter((f) => f.endsWith(".dll") && !SKIP_ASSEMBLY.test(f))
      .sort((a, b) => (b.startsWith("Assembly-CSharp") - a.startsWith("Assembly-CSharp")) || a.localeCompare(b));
    // The game's own assemblies; third-party ones count only once something decompiled them.
    for (const dll of dlls) {
      const rel = `${managedRel}/${dll}`;
      if (!dll.startsWith("Assembly-CSharp") && !bins[rel]?.tools?.ilspy) continue;
      units.push(await unityUnit(ctx, bins[rel], rel, game.managed));
      done.add(`${rel}:ilspy`);
    }
  }
  if (game.engine === "unity-il2cpp") units.push({ rel: "GameAssembly", label: "IL2CPP native code", tool: "",
    complete: check("warn", "IL2CPP compiles the game's C# to native code: method bodies can only be recovered as Ghidra pseudocode, never as compilable C#."),
    compile: skipped("Not recompilable."), run: skipped() });
  for (const bin of Object.values(bins)) for (const tool of Object.keys(bin.tools)) {
    if (done.has(`${bin.rel}:${tool}`)) continue;
    if (tool === "ilspy") units.push(await unityUnit(ctx, bin, bin.rel, path.dirname(path.join(m.path, bin.rel))));
    else if (tool === "gdre") units.push(await godotUnit(ctx, bin, bin.rel));
    else units.push(await genericUnit(ctx, bin, tool));
  }
  return units;
}

// ------------------------------------------------------------------------------------ mods --
// The latest Test run, if it ran the current build.
function testRunCheck(p) {
  const t = p.testRun;
  if (!t || !p.steps?.build?.finished || t.buildFinished !== p.steps.build.finished)
    return skipped("No Test run of the current build yet: run one from Steps → Play & refine (it builds, starts the game headless and compares with the unmodified game).");
  const status = t.state === "passed" ? "pass" : t.state === "failed" ? "fail" : t.state === "running" ? "skip" : "warn";
  return check(status, `Test run ${t.id} of this build: ${t.state}${t.summary ? ` (${t.summary})` : ""}.`);
}

async function modUnits(ctx, p, r) {
  const dir = path.join(MODS, p.id);
  const files = path.join(dir, "src", "files");
  const install = path.join(dir, "install");
  const engine = p.engine || "unity-mono";
  r.engine = p.label || engine;
  const unit = { rel: p.name, label: `${p.name} (${p.label || engine})`, tool: engine };
  // Build current? Newer than every source file.
  const build = p.steps?.build;
  const srcNewest = await newestMtime(path.join(dir, "src"));
  const builtOk = build?.state === "done" && build.finished;
  const fresh = builtOk && build.finished >= srcNewest - 1000;
  const buildText = !build ? "Not built yet." : build.state === "failed" ? `The last build failed: ${build.error || "see build.log"}.`
    : build.state === "running" ? "A build is running." : fresh ? "" : "The build is older than the source: rebuild it.";

  if (engine === "godot") {
    const srcList = exists(files) ? await walk(files, () => true, [], files, 100000) : [];
    const own = srcList.filter((f) => !f.startsWith(".import/") && !f.startsWith(".godot/"));
    const built = path.join(install, p.pckRel || "");
    let missing = [];
    if (builtOk && exists(built)) {
      ctx.step("listing the built pack");
      const { godotPackFiles } = await import("./mods.mjs");
      const pack = await godotPackFiles(built);
      missing = own.filter((f) => !pack.has(`res://${f}`) && !pack.has(`res://${f}.remap`) && !pack.has(`res://${f.replace(/\.gd$/, ".gdc")}`));
    }
    unit.complete = check(!builtOk || !exists(built) || missing.length ? "fail" : !fresh ? "warn" : "pass",
      buildText || (exists(built) ? `All ${plural(own.length, "source file")} under src/files are packed into install/${p.pckRel}${missing.length ? `, except ${missing.length}` : ""}.`
        : `install/${p.pckRel} is missing: build the mod.`),
      { missing: missing.slice(0, MAX_LIST).map((f) => `not packed: res://${f}`) });
    // Compile the whole patched game (recovered project + the mod's files), against the same compile
    // of the unmodified recovered project, so only what the mod breaks counts.
    let recovered = path.join(dir, "decompiled", "project");
    if (!exists(path.join(recovered, "project.godot"))) recovered = (await lookupDecomp(p.gamePath, p.pckRel, "gdre").catch(() => null))?.dir || "";
    if (!recovered || !exists(path.join(recovered, "project.godot"))) {
      unit.compile = check("fail", "No recovered project to compile against: run the decompile step first.");
      unit.run = skipped(); return [unit];
    }
    const testrun = await import("./testrun.mjs");
    let eng;
    const gamePack = await (await import("./mods.mjs")).originalFile(p);
    try { eng = await godotEngine(ctx, await testrun.pckVersion(gamePack), (await gdreReport(recovered)).steam); }
    catch (error) { unit.compile = check("warn", `No engine to compile with: ${error.message}.`); unit.run = testRunCheck(p); return [unit]; }
    const stage = path.join(ctx.scratch, "modded"), base = path.join(ctx.scratch, "original");
    await testrun.stageGame(recovered, files, stage);
    await testrun.stageGame(recovered, null, base);
    // The stage is mostly symlinked folders: list the scripts from the recovered project + the mod.
    const modScripts = new Set(own.filter((f) => f.endsWith(".gd")));
    const scripts = [...new Set([...await walk(recovered, (n) => n.endsWith(".gd"), [], recovered, 50000, (d) => d === ".import" || d === ".godot"), ...modScripts])].sort();
    ctx.step(`compiling ${plural(scripts.length, "script")} of the patched game (${modScripts.size} from the mod) with ${eng.label}`);
    const mine = await godotCompileAll(ctx, eng, stage, scripts);
    ctx.step("compiling the unmodified recovered project for comparison");
    const theirs = await godotCompileAll(ctx, eng, base, scripts.filter((f) => exists(path.join(recovered, f))));
    const known = new Set([...theirs.broken.keys()]);
    const broken = new Map([...mine.broken].filter(([f]) => !known.has(f)));
    const fromMod = [...broken.keys()].filter((f) => modScripts.has(f.replace(/^res:\/\//, ""))).length;
    unit.compile = check(broken.size ? "fail" : "pass",
      `${scripts.length - mine.broken.size}/${scripts.length} scripts of the patched game compile with ${eng.label}` +
      (broken.size ? `; ${plural(broken.size, "script")} broken by the mod (${fromMod} of its own files, ${broken.size - fromMod} game scripts that use what it changed)` : "; the mod breaks none") +
      (known.size ? ` (${known.size} already fail in the unmodified recovery, ignored)` : "") + ".",
      { checked: scripts.length, failed: broken.size, errors: [...brokenList(broken), ...mine.notes.filter((n) => modScripts.has(/^res:\/\/(.+?): /.exec(n)?.[1])).map((n) => `warning: ${n}`)].slice(0, MAX_LIST) });
    if (!builtOk || !exists(built)) { unit.run = skipped("Not run: there is no build."); return [unit]; }
    ctx.step(`running the modded game and the original for ${RUN_MS / 1000}s each`);
    const cmp = await godotCompare(ctx, eng, ctx.scratch, ["--main-pack", built], gamePack, "modded game");
    const used = [...modScripts].filter((f) => cmp.mine.loaded.has(`res://${f}`));
    unit.run = { ...cmp.run, detail: `${cmp.run.detail} ${modScripts.size ? `${used.length}/${modScripts.size} of the mod's scripts loaded during the run.` : ""}`.trim() };
    if (unit.run.status === "pass" && modScripts.size && !used.length) unit.run.status = "warn";
    return [unit];
  }

  if (engine === "unity-mono" || engine === "unity-il2cpp") {
    const dll = path.join(install, "BepInEx", "plugins", p.className || "", `${p.className}.dll`);
    unit.complete = check(!builtOk || !exists(dll) ? "fail" : !fresh ? "warn" : "pass",
      buildText || (exists(dll) ? `install/ has the BepInEx loader and BepInEx/plugins/${p.className}/${p.className}.dll.` : `The plugin DLL is missing from install/: build the mod.`));
    const proj = (await fsp.readdir(path.join(dir, "src")).catch(() => [])).find((f) => f.endsWith(".csproj"));
    if (!proj) unit.compile = check("fail", "No .csproj under src/.");
    else {
      ctx.step("building the mod's C# project with dotnet");
      const work = path.join(ctx.scratch, "src");
      await fsp.cp(path.join(dir, "src"), work, { recursive: true, filter: (s) => !/\/(bin|obj)(\/|$)/.test(s) });
      const b = await dotnetBuild(ctx, path.join(work, proj), "");
      unit.compile = check(b.ok ? "pass" : "fail", b.ok ? `${proj} compiles with dotnet.` : `${b.summary || "Build failed"} in the mod's C#.`, { failed: b.errors.length, errors: b.errors.slice(0, MAX_LIST) });
    }
    unit.run = testRunCheck(p);
    return [unit];
  }

  // Overlay engines (RPG Maker, Ren'Py, Electron, Unreal Lua, generic): files under install/, syntax.
  const srcList = exists(files) ? await walk(files, () => true, [], files, 100000) : [];
  const inInstall = exists(install) ? await walk(install, () => true, [], install, 100000) : [];
  unit.complete = check(!builtOk || !inInstall.length ? "fail" : !fresh ? "warn" : "pass",
    buildText || `install/ holds ${plural(inInstall.length, "file")} built from ${plural(srcList.length, "source file")}.`);
  const parsable = (exists(path.join(dir, "src")) ? await walk(path.join(dir, "src"), (n) => Boolean(languageOf(n)), [], path.join(dir, "src"), 5000) : []);
  ctx.step(`syntax-checking ${plural(parsable.length, "mod file")}`);
  unit.compile = parsable.length ? await syntaxCheck(ctx, path.join(dir, "src"), parsable) : skipped("No source files with an available parser.");
  unit.run = testRunCheck(p);
  return [unit];
}

// -------------------------------------------------------------------------------- rewrites --
// Newest Godot engine REA already has for a major version, else a known release (downloaded on demand).
async function godotVersionFor(projectFile) {
  const text = await fsp.readFile(projectFile, "utf8").catch(() => "");
  const major = Number(text.match(/config_version\s*=\s*(\d+)/)?.[1] || 4) >= 5 ? 4 : 3;
  const feature = text.match(/config\/features\s*=\s*PackedStringArray\("(\d+\.\d+)/)?.[1];
  let version = feature;
  if (!version) {
    const have = [];
    for (const d of [ENGINES, path.join(ENGINES, "official")]) for (const n of await fsp.readdir(d).catch(() => [])) {
      const m = /^godot-(\d+)\.(\d+)(?:\.(\d+))?$/.exec(n);
      if (m && Number(m[1]) === major) have.push(n.slice(6));
    }
    have.sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
    version = have[0] || (major === 3 ? "3.5.2" : "4.3");
  }
  const [a, b, c] = version.split(".").map(Number);
  return { major: a, minor: b, patch: c || 0, version };
}

async function rewriteUnits(ctx, p, r) {
  const dir = path.join(REWRITES, p.id);
  const src = path.join(dir, "src");
  r.engine = [p.language, p.engine].filter(Boolean).join(" · ");
  const unit = { rel: p.name, label: `${p.name} (${r.engine || "rewrite"})`, tool: "rewrite" };
  let parity = [];
  try { parity = JSON.parse(await fsp.readFile(path.join(dir, "PARITY.json"), "utf8")); } catch {}
  if (!Array.isArray(parity)) parity = [];
  const passed = parity.filter((x) => x?.status === "passed" && x.evidence);
  const failed = parity.filter((x) => x?.status === "failed");
  const all = await walk(src, () => true, [], src, 100000, (d) => d === ".import" || d === ".godot" || d === "node_modules" || d === "bin" || d === "obj");
  unit.complete = check(!all.length || failed.length ? "fail" : !parity.length || passed.length < parity.length ? "warn" : "pass",
    !all.length ? "src/ is empty: nothing has been written yet."
      : `${plural(all.length, "source file")}; parity ${passed.length}/${parity.length} behaviors passed with evidence` +
        `${failed.length ? `, ${failed.length} failed` : ""}${!parity.length ? " (PARITY.json has no checklist yet)" : ""}.`,
    { missing: parity.filter((x) => x && !(x.status === "passed" && x.evidence)).slice(0, MAX_LIST).map((x) => `${x.status || "unverified"}: ${x.name}`) });
  const godotProject = all.find((f) => f.endsWith("project.godot") && f.split("/").length <= 3);
  if (godotProject) {
    const root = path.join(src, path.dirname(godotProject));
    const v = await godotVersionFor(path.join(src, godotProject));
    let eng;
    try { eng = await godotEngine(ctx, v, false); }
    catch (error) { unit.compile = check("warn", `Godot ${v.version} isn't available: ${error.message}.`); unit.run = skipped(); return [unit]; }
    const stage = path.join(ctx.scratch, "godot");
    await (await import("./testrun.mjs")).stageGame(root, null, stage);
    const scripts = await walk(root, (n) => n.endsWith(".gd"), [], root, 50000, (d) => d === ".import" || d === ".godot");
    ctx.step(`compiling ${plural(scripts.length, "GDScript file")} with ${eng.label}`);
    if (v.major >= 4 && !exists(path.join(root, ".godot"))) {
      // A Godot 4 project nobody opened in the editor has no import cache; build it once.
      ctx.log.write("\n--- import (Godot 4 projects need a .godot/ cache) ---\n");
      await exec(eng.bin, ["--headless", "--path", stage, "--import"], { cwd: stage, env: eng.env, timeoutMs: 5 * 60000, log: ctx.log });
    }
    const { broken, notes } = await godotCompileAll(ctx, eng, stage, scripts);
    unit.compile = check(!scripts.length ? "skip" : broken.size ? "fail" : "pass",
      scripts.length ? `${scripts.length - broken.size}/${scripts.length} GDScript files compile with ${eng.label}.` : "No GDScript files.",
      { checked: scripts.length, failed: broken.size, errors: [...brokenList(broken), ...notes.map((n) => `warning: ${n}`)].slice(0, MAX_LIST) });
    ctx.step(`running the rewrite for ${RUN_MS / 1000}s`);
    const run = await godotPlay(ctx, eng, stage, ["--path", stage], "rewrite");
    const errors = [...new Set(run.errors)];
    unit.run = check(errors.length || (run.early && run.code !== 0) ? "fail" : "pass",
      errors.length ? `Prints ${plural(errors.length, "error")} while running.` : run.early ? (run.code === 0 ? `Quit by itself after ${Math.round(run.ms / 1000)}s with no errors.` : `Exited after ${Math.round(run.ms / 1000)}s with code ${run.code}.`)
        : `Ran ${RUN_MS / 1000}s with no errors. Behavior parity is what PARITY.json tracks.`,
      { errors: errors.slice(0, MAX_LIST) });
    return [unit];
  }
  const csproj = all.find((f) => f.endsWith(".csproj")) || all.find((f) => f.endsWith(".sln"));
  if (csproj) {
    ctx.step(`building ${csproj} with dotnet`);
    const work = path.join(ctx.scratch, "src");
    await fsp.cp(src, work, { recursive: true, filter: (s) => !/\/(bin|obj|node_modules)(\/|$)/.test(s) });
    const b = await dotnetBuild(ctx, path.join(work, csproj), "");
    unit.compile = check(b.ok ? "pass" : "fail", b.ok ? `${csproj} compiles with dotnet.` : b.summary, { failed: b.errors.length, errors: b.errors.slice(0, MAX_LIST) });
    unit.run = skipped("Not run: REA has no generic runner for .NET games. Record behavior checks in PARITY.json.");
    return [unit];
  }
  const java = all.filter((f) => f.endsWith(".java"));
  if (java.length) {
    ctx.step(`compiling ${plural(java.length, "Java file")} with javac`);
    const res = await exec("javac", ["-d", path.join(ctx.scratch, "classes"), "-nowarn", ...java], { cwd: src, timeoutMs: 10 * 60000, log: ctx.log });
    const errors = res.lines.filter((l) => /^\S+\.java:\d+: error:/.test(l)).slice(0, MAX_LIST);
    unit.compile = check(res.code === 0 ? "pass" : "fail", res.code === 0 ? `${plural(java.length, "Java file")} compile with javac.` : `${plural(errors.length, "compile error")} from javac (dependencies outside src/ aren't on the classpath).`, { errors });
    unit.run = skipped("Not run: REA has no generic runner for Java games.");
    return [unit];
  }
  const parsable = all.filter((f) => languageOf(f) && !/\.(cs|h|hpp|tscn|tres)$/i.test(f));
  if (parsable.length) {
    ctx.step(`syntax-checking ${plural(parsable.length, "file")}`);
    unit.compile = await syntaxCheck(ctx, src, parsable);
    unit.run = skipped("Not run: REA has no generic runner for this language. Record behavior checks in PARITY.json.");
    return [unit];
  }
  unit.compile = all.length ? skipped(`No compiler for ${p.language || "this language"} in the REA container (it has Godot, dotnet, javac, node, python and luac).`) : skipped("Nothing to compile.");
  unit.run = skipped();
  return [unit];
}

// ------------------------------------------------------------------------------- the job --
function verdict(units, kind) {
  const all = units.flatMap((u) => [u.complete, u.compile, u.run]);
  const subject = { decompiled: "the decompiled build", mod: "the mod", rewrite: "the rewrite" }[kind];
  if (!units.length) return ["fail", "Nothing has been decompiled for this game yet."];
  const fails = units.filter((u) => [u.complete, u.compile, u.run].some((c) => c.status === "fail"));
  if (fails.length) return ["fail", kind === "decompiled"
    ? `${plural(fails.length, "part")} of the decompiled build ${fails.length === 1 ? "has" : "have"} problems: ${fails.map((u) => u.rel).join(", ")}.`
    : `${subject[0].toUpperCase()}${subject.slice(1)} has problems: ${["complete", "compile", "run"].filter((k) => units.some((u) => u[k].status === "fail")).map((k) => ({ complete: "incomplete", compile: "doesn't compile", run: "fails when run" })[k]).join(", ")}.`];
  if (all.some((c) => c.status === "warn")) return ["partial", "No hard failures, but some checks are incomplete or inconclusive (see the warnings)."];
  if (!units.some((u) => u.compile.status === "pass")) return ["partial", `Complete as far as REA can tell, but nothing could be compiled to prove ${subject} works.`];
  return ["pass", { decompiled: "Fully decompiled: everything recovered, compiles, and runs as far as REA can test it.",
    mod: "The mod is fully built, the patched game compiles, and it runs as far as REA can test it.",
    rewrite: "The rewrite is complete per its parity checklist, compiles, and runs as far as REA can test it." }[kind]];
}

async function execute(job, r) {
  const scratch = await fsp.mkdtemp(path.join(os.tmpdir(), "rea-buildcheck-"));
  const log = fs.createWriteStream(job.log);
  const ctx = { key: job.key, gamePath: job.gamePath, scratch, log, display: null,
    step: (s) => { r.step = s; log.write(`\n== ${s}\n`); }, state: () => "ok" };
  try {
    r.units = await job.run(ctx, r);
    [r.verdict, r.headline] = verdict(r.units, r.kind);
    r.state = "done";
  } catch (error) {
    r.state = "failed"; r.error = error.message; r.verdict = "fail"; r.headline = `The check itself failed: ${error.message}`;
    log.write(`\nFAILED: ${error.stack || error.message}\n`);
  } finally {
    ctx.display?.stop();
    r.finished = Date.now();
    delete r.step;
    log.write(`\n${r.verdict?.toUpperCase()}: ${r.headline}\n`);
    log.end();
    await fsp.writeFile(job.report, JSON.stringify(r, null, 1)).catch(() => {});
    await fsp.rm(scratch, { recursive: true, force: true }).catch(() => {});
    running.delete(job.id);
    (await import("./mods.mjs")).releaseJob();
  }
}

// GET → the running or last report; POST → start one. job = {id, kind, title, dir, key?, gamePath, run}.
async function handle(job, { method, res, send, httpError }) {
  job.report = path.join(job.dir, "build-check.json");
  job.log = path.join(job.dir, "build-check.log");
  if (method === "GET") {
    let report = running.get(job.id) || null;
    if (!report) try { report = JSON.parse(await fsp.readFile(job.report, "utf8")); } catch {}
    send(res, 200, { report, log: exists(job.log) ? job.log : "" });
    return true;
  }
  if (method !== "POST") return false;
  if (running.has(job.id)) { send(res, 200, { report: running.get(job.id) }); return true; }
  const { claimJob } = await import("./mods.mjs");
  try { claimJob({ id: job.id, name: job.title }, `${job.kind} build check`); }
  catch (error) { throw httpError(409, error.message); }
  const r = { id: job.id, kind: job.kind, ...(job.key ? { key: job.key } : {}), game: job.title, state: "running", started: Date.now(), step: "starting", units: [] };
  running.set(job.id, r);
  await fsp.mkdir(job.dir, { recursive: true });
  execute(job, r).catch((error) => console.error(`build check ${job.id}: ${error.stack || error.message}`));
  send(res, 202, { report: r });
  return true;
}

// /kb/<key>/build-check (m = kb.json)
export function handleBuildCheck({ key, m, method, res, send, httpError }) {
  return handle({ id: `kb-${key}`, kind: "decompiled", key, title: m.game || path.basename(m.path), gamePath: m.path,
    dir: path.join(kbDir(key), "decompiled"), run: (ctx, r) => decompiledUnits(ctx, m, r) }, { method, res, send, httpError });
}

// /mods/<id>/build-check and /mods/rewrites/<id>/build-check
export async function handleProjectCheck({ api, method, res, send, httpError }) {
  const match = api.match(/^\/mods\/(?:(rewrites)\/)?([0-9a-f]{12})\/build-check$/);
  if (!match || !["GET", "POST"].includes(method)) return false;
  const rewrite = Boolean(match[1]);
  const dir = path.join(rewrite ? REWRITES : MODS, match[2]);
  let p;
  try { p = JSON.parse(await fsp.readFile(path.join(dir, "project.json"), "utf8")); }
  catch { throw httpError(404, rewrite ? "Unknown rewrite project" : "Unknown mod project"); }
  return handle({ id: `${rewrite ? "rewrite" : "mod"}-${p.id}`, kind: rewrite ? "rewrite" : "mod", title: p.name, gamePath: p.gamePath, dir,
    // Read the project again when the job starts: a build may have just finished.
    run: async (ctx, r) => { const fresh = JSON.parse(await fsp.readFile(path.join(dir, "project.json"), "utf8")); return rewrite ? rewriteUnits(ctx, fresh, r) : modUnits(ctx, fresh, r); } },
    { method, res, send, httpError });
}
