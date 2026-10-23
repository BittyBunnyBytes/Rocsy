// Mod projects: guided game modding, one adapter per engine family.
//   unity-mono    BepInEx 5 + Harmony plugin (C#, net472)      decompile: ILSpy on Managed/
//   unity-il2cpp  BepInEx 6 IL2CPP plugin (C#, net6.0)         loader + first run, then ILSpy on BepInEx/interop/
//   unreal        UE4SS Lua mod (Windows/Proton)               loader + in-game dumps, then index them
//   godot         patched .pck (GDScript files)                GDRE project recovery
//   rpgmaker      MV/MZ JS plugin + plugins.js entry           copy js/ + data/
//   renpy         .rpy drop-ins                                copy .rpy, unrpyc for .rpyc
//   electron      patched app.asar / app/ overlay              asar extract
//   generic       file overlay                                 none (the agent uses REA/Ghidra)
// A project lives in the agent's workspace (rea-mods/<id>/) so the Master Agent can read and edit
// it. Extracting and building run here (only this container has the toolchains and the read-only
// game mount). install/ mirrors the game folder; the host helper (hostctl/reamods.py) copies it in
// when the user clicks Install or an authorized agent uses the host play-test helper, with backups.
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { StringDecoder } from "node:string_decoder";
import { handleRewrites } from "./rewrites.mjs";
import { projectFile } from "./projectfiles.mjs";
import { record as recordDecomp, lookup as lookupDecomp, linkTree, outDir as decompDir, ensureKbForGame, containedFile } from "./decomp.mjs";
import { detectEngineHints, isEngineMarker } from "./engine-detection.mjs";
import { engineVersion, versionBrief } from "./engine-version.mjs";
import { rulesBrief, pickAssemblies } from "./rules.mjs";
import { readGoals } from "./goals.mjs";

export const MODS = "/opt/data/rocsy-workspace/rea-mods";
const RECIPES = path.join(path.dirname(new URL(import.meta.url).pathname), "recipes");
const TOOLS = "/opt/rea-tools";
export const SKIP_ASSEMBLY = /^(UnityEngine|Unity\.|System|Mono\.|mscorlib|netstandard|Microsoft\.|Newtonsoft|DOTween|0Harmony|BepInEx|Il2Cpp(mscorlib|System|Interop)|Il2CppMono)/i;
let job = null; // one extract/build at a time: they are CPU heavy

const exists = (p) => fs.existsSync(p);

// The game's ORIGINAL copy of a file. While a mod is installed, hostctl has replaced the file in the
// game folder (e.g. webfishing.exe) and keeps the original in its backup/, mounted read-only here.
// Building, recovering or baselining from the installed copy would layer the new build on top of the
// old one (2026-10-16: a rebuild after a play test patched the already-modded exe).
const INSTALLS = process.env.REA_INSTALLS || "/home/node/rea-installs";
export async function originalFile(p, rel = p.pckRel) {
  const live = path.join(p.gamePath, rel);
  const key = rel.split(path.sep).join("/");
  let names = [];
  try { names = (await fsp.readdir(INSTALLS)).filter((f) => f.endsWith(".json")); } catch { return live; }
  for (const name of names) {
    let state;
    try { state = JSON.parse(await fsp.readFile(path.join(INSTALLS, name), "utf8")); } catch { continue; }
    if ((state.gamePath || state.game) !== p.gamePath) continue;
    const entry = state.files?.[key];
    if (!entry) return live;
    if (entry.backup) {
      const at = String(entry.backup).indexOf("/rea-mod-installs/");
      const backup = at >= 0 ? path.join(INSTALLS, String(entry.backup).slice(at + "/rea-mod-installs/".length)) : "";
      if (backup && exists(backup)) return backup;
    }
    throw new Error(`${rel} in the game folder is an installed mod build${entry.created ? " the game never had" : " whose original backup is missing"}; uninstall the mod (REA page) or verify the game files in Steam, then try again`);
  }
  return live;
}
// Every file REA's installer put into this game folder (any mod): rel -> backup path of the original, or
// "" when the install created it. Test runs stage the game without them: the installed build is older
// than the one under test (a Unity game: the user's own installed mod DLL ran in every test).
export async function installedFiles(gamePath) {
  const out = new Map();
  let names = [];
  try { names = (await fsp.readdir(INSTALLS)).filter((f) => f.endsWith(".json")); } catch { return out; }
  for (const name of names) {
    let state;
    try { state = JSON.parse(await fsp.readFile(path.join(INSTALLS, name), "utf8")); } catch { continue; }
    if ((state.gamePath || state.game) !== gamePath) continue;
    for (const [rel, entry] of Object.entries(state.files || {})) {
      const at = String(entry.backup || "").indexOf("/rea-mod-installs/");
      const backup = at >= 0 ? path.join(INSTALLS, String(entry.backup).slice(at + "/rea-mod-installs/".length)) : "";
      out.set(rel, entry.created ? "" : backup && exists(backup) ? backup : null);
    }
  }
  return out;
}
const now = () => Date.now();
const rel = (from, to) => path.relative(from, to).split(path.sep).join("/");

async function readProject(id) {
  if (!/^[0-9a-f]{12}$/.test(id)) return null;
  try { return JSON.parse(await fsp.readFile(path.join(MODS, id, "project.json"), "utf8")); } catch { return null; }
}

async function saveProject(project) {
  const file = path.join(MODS, project.id, "project.json");
  await fsp.writeFile(file + ".tmp", JSON.stringify(project, null, 1));
  await fsp.rename(file + ".tmp", file);
}

// Recovery, packaging and a successful play test are different claims. Keep the last
// milestone tied to the exact build so a rebuild cannot inherit an old pass.
export async function modReadiness(project) {
  const dir = path.join(MODS, project.id);
  const steps = project.steps || {};
  const source = await findFiles(path.join(dir, project.engine === "godot" ? "src/files" : "src"),
    (f) => !f.endsWith(".csproj"), 12);
  const recovery = project.engine === "godot"
    ? await fsp.readFile(path.join(dir, "decompiled/project/gdre_export.log"), "utf8").catch(() => "") : "";
  const failedConversions = Number(recovery.match(/Failed conversions:\s*(\d+)/)?.[1] || 0);
  const missedScripts = Number(recovery.match(/Scripts not decompiled:\s*(\d+)/)?.[1] || 0);
  const recoveryPartial = failedConversions > 0 || missedScripts > 0;
  const hasPlan = Boolean((await fsp.readFile(path.join(dir, "PLAN.md"), "utf8").catch(() => "")).trim());
  const godotIssues = project.engine === "godot" && source.length
    ? [...await inspectGodotSource(project, source), ...await autoloadGaps(project, source), ...await missingResPaths(project, source)] : [];
  const looseFiles = project.engine === "godot" ? (await fsp.readdir(dir).catch(() => []))
    .filter((f) => /\.(gd|tscn|tres)$/.test(f)) : [];
  const extracted = steps.decompile?.state === "done" || steps.decompile?.state === "partial";
  const artifact = path.join(installDir(project), project.pckRel || "");
  const artifactHead = project.engine === "godot" ? await readHead(artifact, 4) : null;
  const built = steps.build?.state === "done" && exists(installDir(project)) &&
    (project.engine !== "godot" || (artifactHead.length === 4 &&
      (/\.exe$/i.test(project.pckRel || "") ? artifactHead.subarray(0, 2).toString() === "MZ" : artifactHead.toString() === "GDPC")));
  const marker = await fsp.stat(path.join(dir, ".installed")).catch(() => null);
  const installed = Boolean(marker && built && marker.mtimeMs >= (steps.build.finished || 0));
  const verified = project.verification?.state === "passed" && built && installed
    && project.verification.buildFinished === steps.build.finished;
  const issues = [];
  if (!extracted) issues.push("Recover or inspect the game before claiming the mod is ready.");
  if (recoveryPartial) issues.push(`Recovery is partial: ${failedConversions} resource conversion(s) failed and ${missedScripts} script(s) were missed.`);
  issues.push(...godotIssues);
  if (looseFiles.length) issues.push(`${looseFiles.length} draft Godot file(s) sit outside src/files/ and will not be packaged.`);
  if (!source.length) issues.push("No mod source exists in src/files/ yet.");
  const missingRelevant = (await relevantFiles(project)).filter((f) => !f.exists).map((f) => f.path);
  if (missingRelevant.length) issues.push(`RELEVANT.md lists files that don't exist (guessed names?): ${missingRelevant.join(", ")}. Find the real files in decompiled/ (ls/grep) and fix the list.`);
  if (!built) issues.push(project.engine === "godot" && steps.build?.state === "done" ?
    "Packaged artifact has the wrong format; rebuild before installation." :
    steps.build?.state === "failed" ? `Build failed: ${steps.build.error || "see build.log"}` : "No successful packaged build yet.");
  if (!installed) issues.push(marker ? "Installed copy is older than the current build; reinstall it." : "Build has not been installed into the game.");
  if (project.verification?.state === "failed") issues.push(`Play test failed: ${project.verification.evidence}`);
  if (!verified) issues.push("No passing play test for the current installed build.");
  const run = project.testRun?.buildFinished && project.testRun.buildFinished === steps.build?.finished ? project.testRun : null;
  if (run?.state === "failed") issues.push(`Headless test run failed: ${run.summary || "see the Logs tab"}`);
  else if (built && !headlessSupport(project) && run?.state !== "passed") issues.push("No passing headless test run for the current build (use Test run).");
  const percent = (extracted ? (recoveryPartial ? 10 : 15) : 0)
    + (hasPlan && (project.stage === "approved" || project.stage === "refining") ? 10 : 0)
    + (source.length && !godotIssues.length ? 15 : 0) + (built ? 25 : 0) + (installed ? 10 : 0) + (verified ? 25 : 0);
  return { percent, state: verified && !recoveryPartial ? "verified" : steps.build?.state === "failed" || project.verification?.state === "failed" ? "blocked" : "incomplete",
    label: verified ? (recoveryPartial ? "Play test passed; recovery still partial" : "Running build verified")
      : recoveryPartial ? "Recovery partial; build unverified" : "Build unverified",
    issues, failedConversions, missedScripts, verified };
}

// Godot 4 API written into a Godot 3 game parses or runs into errors only when that line runs
// (a Godot 3 game: JSON.stringify broke saving after hours of edits). Each entry: what, Godot 3 way.
const GODOT4_IN_3 = [
  [/\bJSON\.stringify\s*\(/, "JSON.stringify()", "to_json(value) or JSON.print(value)"],
  [/\bJSON\.parse_string\s*\(/, "JSON.parse_string()", "parse_json(text) or JSON.parse(text).result"],
  [/\bFileAccess\./, "FileAccess", "var f = File.new(); f.open(path, File.READ)"],
  [/\bDirAccess\./, "DirAccess", "var d = Directory.new(); d.open(path)"],
  [/^\s*@export\b/, "@export", "export var name = value"],
  [/^\s*@onready\b/, "@onready", "onready var name = $Node"],
  [/^\s*@tool\b/, "@tool", "tool (first line)"],
  [/\bawait\b/, "await", "yield(object, \"signal\")"],
  [/\.instantiate\s*\(/, ".instantiate()", ".instance()"],
  [/\bsuper\s*[.(]/, "super", ".method_name() calls the parent's method"],
  [/\bCallable\s*\(/, "Callable()", "connect(\"signal\", target, \"method\") / funcref()"],
  [/\bPacked(?:String|Byte|Int32|Int64|Float32|Float64|Vector2|Vector3|Color)Array\b/, "Packed*Array", "Pool*Array (PoolStringArray, PoolByteArray, ...)"],
  [/(?<![.\w])rand[fi]_range\s*\(/, "global randf_range()/randi_range()", "rand_range(a, b) (RandomNumberGenerator.randf_range is fine)"],
  [/\b(?:str_to_var|var_to_str|deg_to_rad|rad_to_deg|bytes_to_var|var_to_bytes)\s*\(/, "*_to_* conversions", "str2var, var2str, deg2rad, rad2deg, bytes2var, var2bytes"],
  [/\bchange_scene_to_(?:file|packed)\s*\(/, "change_scene_to_file()", "get_tree().change_scene(path) / change_scene_to(packed)"],
  [/\.emit\s*\(/, "signal.emit()", "emit_signal(\"signal_name\", args...)"],
  [/\bis_empty\s*\(/, ".is_empty()", ".empty()"],
];

async function godotMajor(project) {
  const config = await fsp.readFile(path.join(MODS, project.id, "decompiled/project/project.godot"), "utf8").catch(() => "");
  const v = Number(config.match(/^config_version\s*=\s*(\d+)/m)?.[1]);
  return v === 4 ? 3 : v >= 5 ? 4 : 0;
}

function godot4Calls(file, body) {
  const found = [];
  body.split("\n").forEach((raw, n) => {
    const line = raw.replace(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '""').replace(/#.*$/, "");
    for (const [re, what, instead] of GODOT4_IN_3)
      if (re.test(line)) found.push(`${file}:${n + 1}: ${what} is Godot 4; this game is Godot 3: use ${instead}.`);
  });
  return found;
}

// The Godot version from the pack header (a .pck, or a pack embedded at the end of an executable).
export async function pckVersion(file) {
  const fd = await fsp.open(file, "r");
  try {
    const { size } = await fd.stat();
    let start = 0;
    const tail = Buffer.alloc(12);
    await fd.read(tail, 0, 12, size - 12);
    if (tail.subarray(8).toString() === "GDPC") start = size - 12 - Number(tail.readBigUInt64LE(0));
    const head = Buffer.alloc(20);
    await fd.read(head, 0, 20, start);
    if (head.subarray(0, 4).toString() !== "GDPC") throw new Error("No Godot pack header found");
    const [major, minor, patch] = [head.readUInt32LE(8), head.readUInt32LE(12), head.readUInt32LE(16)];
    return { major, minor, patch, version: `${major}.${minor}${patch ? `.${patch}` : ""}` };
  } finally { await fd.close(); }
}

// The engine version up front in every agent brief: models write Godot 4 by habit, and in a Godot 3
// game the 3D renames (.position on a Spatial) neither fail the build nor log at runtime in a release
// export: one game's players and props all stayed at the origin.
const GODOT3_RENAMES = [
  "3D nodes (Spatial, MeshInstance, KinematicBody, the game's players/NPCs): .position → .translation, " +
    ".global_position → .global_transform.origin. In Godot 3 `.position` exists only on 2D nodes; on a 3D node it does NOTHING, with no error",
  "3D classes: Node3D → Spatial, MeshInstance3D → MeshInstance, CharacterBody3D → KinematicBody, StaticBody3D → StaticBody, " +
    "Area3D → Area, Camera3D → Camera, StandardMaterial3D → SpatialMaterial, TorusMesh doesn't exist",
  ".instantiate() → .instance(); await obj.signal → yield(obj, \"signal\"); sig.emit(a) → emit_signal(\"sig\", a); " +
    "connect(\"s\", callable) → connect(\"s\", self, \"method\"); super.f() → .f()",
  "@export / @onready / @tool → export var / onready var / tool; JSON.stringify / JSON.parse_string → to_json / parse_json; " +
    "FileAccess / DirAccess → File.new() / Directory.new()",
  "change_scene_to_file → change_scene; .is_empty() → .empty(); Packed*Array → Pool*Array; deg_to_rad → deg2rad; randf_range → rand_range",
];
const GODOT4_RENAMES = [
  ".translation → .position (3D too); Spatial → Node3D, MeshInstance → MeshInstance3D, KinematicBody → CharacterBody3D, SpatialMaterial → StandardMaterial3D",
  ".instance() → .instantiate(); yield(obj, \"signal\") → await obj.signal; emit_signal(\"s\") → s.emit(); export var / onready var / tool → @export / @onready / @tool",
  "to_json / parse_json → JSON.stringify / JSON.parse_string; File / Directory → FileAccess / DirAccess; .empty() → .is_empty(); Pool*Array → Packed*Array",
];

// ENGINE VERSION block + the Rules tab's hot files for this engine.
export async function engineBrief(project) {
  return [await versionPart(project), rulesBrief(project.engine || "unity-mono")].filter(Boolean).join("\n").trim();
}
async function versionPart(project) {
  if (project.engine !== "godot") return versionBrief(await engineVersion(project.gamePath).catch(() => null));
  const major = await godotMajor(project);
  let version = "";
  try { const v = await pckVersion(await originalFile(project)); version = v.version; if (!major && v.major) return briefFor(v.major, version); }
  catch {}
  return major ? briefFor(major, version) : "";
}
function briefFor(major, version) {
  const name = `Godot ${version || `${major}.x`}`;
  const rules = major === 3 ? GODOT3_RENAMES : GODOT4_RENAMES;
  return `ENGINE VERSION: this game runs ${name}. Write Godot ${major} GDScript only; most examples you know are ` +
    `${major === 3 ? "Godot 4" : "Godot 3"}, so check every class, property and function name against this list and the game's own decompiled scripts:\n` +
    rules.map((r) => `- ${r}`).join("\n") +
    (major === 3 ? "\nThe build refuses some Godot 4 calls, but not the 3D property/class renames; a wrong name there simply does nothing in the shipped game. " +
      "When a 3D object doesn't move or appear, look for Godot 4 names first." : "");
}

// A mod that repoints or replaces an autoload singleton must keep what the rest of the game uses
// (a Godot 3 game: PlayerData → a 1-member script while 196 game scripts used 110 members, 793 times).
const OBJECT_MEMBERS = new Set(("connect disconnect is_connected emit_signal has_signal call call_deferred callv set get " +
  "set_deferred has_method get_class is_class get_meta set_meta has_meta remove_meta notification free get_script set_script " +
  "get_property_list get_method_list get_signal_list get_signal_connection_list property_list_changed_notify tr " +
  "get_node get_node_or_null has_node get_tree get_parent get_children get_child get_child_count add_child remove_child " +
  "queue_free is_inside_tree is_a_parent_of get_viewport get_path name owner filename pause_mode set_process " +
  "set_physics_process set_process_input set_process_unhandled_input is_processing get_index move_child " +
  "add_to_group remove_from_group is_in_group get_groups propagate_call print_tree raise duplicate replace_by " +
  "set_name get_name find_node get_owner set_owner get_process_delta_time get_physics_process_delta_time " +
  "rpc rpc_id rset rset_id is_network_master get_network_master set_network_master visible show hide " +
  "position global_position rotation scale transform global_transform modulate self_modulate " +
  "rect_position rect_size rect_global_position margin_left margin_right margin_top margin_bottom " +
  "anchor_left anchor_right anchor_top anchor_bottom grab_focus has_focus release_focus get_rect " +
  "play stop playing seek stream volume_db pitch_scale bus start time_left wait_time one_shot autostart is_stopped " +
  "translation global_translation rotation_degrees get_global_mouse_position").split(" "));

const autoloads = (config) => {
  const out = {};
  const section = config.match(/^\[autoload\]\s*\n([\s\S]*?)(?=^\[|$(?![\s\S]))/m)?.[1] || "";
  for (const [, name, file] of section.matchAll(/^(\w+)\s*=\s*"\*?res:\/\/([^"]+)"/gm)) out[name] = file;
  return out;
};
const stripCode = (line) => line.replace(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '""').replace(/#.*$/, "");

async function godotMembers(read, file, seen = new Set()) {
  if (seen.has(file) || seen.size > 6) return null;
  seen.add(file);
  const body = await read(file);
  if (body == null) return null;
  const names = new Set();
  for (const raw of body.split("\n")) {
    const line = stripCode(raw);
    const m = line.match(/^\s*(?:(?:export(?:\([^)]*\))?|onready|static|puppet|remote|master|remotesync|puppetsync|mastersync)\s+)*(?:var|const|func|signal|enum|class)\s+(\w+)/);
    if (m) names.add(m[1]);
    for (const [, list] of line.matchAll(/^\s*enum\s*(?:\w+\s*)?\{([^}]*)\}/g)) for (const v of list.split(",")) if (v.trim()) names.add(v.trim().split(/[\s=]/)[0]);
  }
  const parent = body.match(/^extends\s+"res:\/\/([^"]+)"/m)?.[1];
  if (parent) {
    const inherited = await godotMembers(read, parent, seen);
    if (!inherited) return null;          // can't see the parent: don't guess
    for (const n of inherited) names.add(n);
  } else if (/^extends\s+(?!Node\b|Node2D\b|Spatial\b|Control\b|Reference\b|Object\b|Resource\b|CanvasLayer\b)\w+/m.test(body)) return null;
  return names;
}

// res:// paths written in the mod's own scripts that exist nowhere (a Godot 3 game: Start went to a
// res://rocsy/game_main.tscn that was never made). A warning: a partial recovery can miss real files.
async function missingResPaths(project, files) {
  const root = path.join(MODS, project.id);
  const overlay = path.join(root, "src/files"), recovered = path.join(root, "decompiled/project");
  const out = [];
  for (const file of files.filter((f) => f.endsWith(".gd"))) {
    const body = await fsp.readFile(path.join(overlay, file), "utf8").catch(() => "");
    body.split("\n").forEach((raw, n) => {
      const code = raw.replace(/#.*$/, "");
      for (const [, ref] of code.matchAll(/"res:\/\/([^"]+\.(?:tscn|scn|gd|tres|res|png|jpg|webp|ogg|wav|mp3|ttf|otf|json|glb|obj|material|shader))"/g))
        if (!exists(path.join(overlay, ref)) && !exists(path.join(recovered, ref))) out.push(`${file}:${n + 1}: res://${ref} does not exist in src/files or the game.`);
    });
  }
  return out.slice(0, 20);
}

async function autoloadGaps(project, files) {
  const root = path.join(MODS, project.id);
  const overlay = path.join(root, "src/files"), recovered = path.join(root, "decompiled/project");
  const read = async (rel) => (await fsp.readFile(path.join(overlay, rel), "utf8").catch(() => null)) ?? (await fsp.readFile(path.join(recovered, rel), "utf8").catch(() => null));
  const original = autoloads(await fsp.readFile(path.join(recovered, "project.godot"), "utf8").catch(() => ""));
  const mine = files.includes("project.godot") ? autoloads(await fsp.readFile(path.join(overlay, "project.godot"), "utf8")) : original;
  const changed = Object.entries(mine).filter(([name, file]) => file.endsWith(".gd") && (original[name] !== file || files.includes(file)));
  if (!changed.length) return [];
  const scripts = new Set([...files.filter((f) => f.endsWith(".gd")), ...await findFiles(recovered, (r) => r.endsWith(".gd"), 14)]);
  const issues = [];
  const readOriginal = async (rel) => fsp.readFile(path.join(recovered, rel), "utf8").catch(() => null);
  for (const [name, file] of changed) {
    const declared = await godotMembers(read, file);
    if (!declared) continue;
    // Only what the game's own singleton had: some shipped scripts already use members that don't
    // exist (dead legacy code), and those are not the mod's doing.
    const before = original[name]?.endsWith(".gd") ? await godotMembers(readOriginal, original[name]) : null;
    const uses = new Map();
    for (const s of scripts) {
      const body = await read(s);
      if (body == null || s === file) continue;
      body.split("\n").forEach((raw, n) => {
        for (const [, member] of stripCode(raw).matchAll(new RegExp(`(?<![\\w.])${name}\\.([A-Za-z_]\\w*)`, "g")))
          if (!declared.has(member) && !OBJECT_MEMBERS.has(member) && (!before || before.has(member))) {
            const u = uses.get(member) || { count: 0, at: `${s}:${n + 1}` };
            u.count++; uses.set(member, u);
          }
      });
    }
    if (!uses.size) continue;
    const top = [...uses].sort((a, b) => b[1].count - a[1].count);
    const total = top.reduce((sum, [, u]) => sum + u.count, 0);
    issues.push(`Autoload ${name} → res://${file} lacks ${uses.size} member(s)${before ? ` the game's original ${name} had and its scripts still use` : " that scripts use"} (${total} uses): ` +
      top.slice(0, 12).map(([m, u]) => `${m} (${u.count}×, e.g. ${u.at})`).join(", ") + (top.length > 12 ? ", …" : "") +
      `. Keep the original script's members (copy res://${original[name] || file} and add to it, or extends "res://${original[name] || file}").`);
  }
  return issues;
}

async function inspectGodotSource(project, files) {
  const root = path.join(MODS, project.id);
  const overlay = path.join(root, "src/files");
  const recovered = path.join(root, "decompiled/project");
  const issues = [];
  const major = await godotMajor(project);
  const existing = files.filter((f) => exists(path.join(recovered, f)));
  if (!existing.length) issues.push("No existing game file is patched to load the new Godot files.");
  for (const file of files) {
    if (!/\.(gd|tscn|tres)$/.test(file)) continue;
    const body = await fsp.readFile(path.join(overlay, file), "utf8");
    if (/\.gd$/.test(file) && /^(?:print\(|[A-Z][A-Z_]+\s*=)/m.test(body))
      issues.push(`${file}: executable statements or bare assignments at script scope.`);
    if (/\.gd$/.test(file)) {
      // GDScript comments are `#` in every Godot version; `//` lines fail to parse ("Unexpected token: '/'").
      const slash = body.split("\n").flatMap((l, i) => /^\s*\/\//.test(l) ? [i + 1] : []);
      if (slash.length) issues.push(`${file}: \`//\` is not a GDScript comment, use # (line${slash.length > 1 ? "s" : ""} ${slash.slice(0, 30).join(", ")}).`);
      // A call at column 0 (add_child(...), get_tree()...) is a statement outside any func.
      const KEYWORDS = /^(?:func|static|var|const|onready|export|signal|enum|class|class_name|extends|tool|remote|master|puppet|remotesync|mastersync|puppetsync|setget|if|elif|else|for|while|match|return|pass)\b/;
      const top = body.split("\n").findIndex((l) => /^[a-z_$][\w.$]*(?:\([^)]*\))?(?:\.[\w]+)*\s*\(/.test(l) && !KEYWORDS.test(l));
      if (top >= 0) issues.push(`${file}:${top + 1}: a call outside any function (script scope only allows var/const/func/signal…); move it into _ready().`);
    }
    if (/\.gd$/.test(file) && major === 3) issues.push(...godot4Calls(file, body).slice(0, 20));
    if (/\.(tscn|tres)$/.test(file)) {
      if (!/^\[gd_(?:scene|resource)\b/.test(body)) issues.push(`${file}: missing Godot resource header.`);
      for (const line of body.match(/^\[ext_resource[^\]]*\]$/gm) || []) {
        if (!/\bid=\d+/.test(line)) issues.push(`${file}: external resource has no id.`);
        const ref = line.match(/\bpath="res:\/\/([^"]+)"/)?.[1];
        if (ref && !exists(path.join(overlay, ref)) && !exists(path.join(recovered, ref)))
          issues.push(`${file}: missing res://${ref}.`);
      }
      if (major !== 4 && /\btype="Progress"/.test(body)) issues.push(`${file}: Progress is not a Godot 3 node type.`);
      // format=2 is Godot 3: resource references are bare numbers; quoted ids are Godot 4 syntax and fail
      // at load time ("Expected number (sub-resource index)"), which earlier mods only found by running.
      if (/^\[gd_(?:scene|resource)[^\]]*\bformat=2\b/.test(body)) {
        const quoted = body.match(/\b(?:Ext|Sub)Resource\(\s*"[^"]*"\s*\)/);
        if (quoted) issues.push(`${file}: ${quoted[0]} is Godot 4 syntax; this format=2 (Godot 3) file needs a bare number, e.g. ExtResource(1).`);
        if (/^\[(?:ext|sub)_resource[^\]]*\bid="[^"]*"/m.test(body)) issues.push(`${file}: quoted resource ids are Godot 4 syntax; Godot 3 uses id=1.`);
      }
      if (major !== 4 && project.engine === "godot" && /\.tscn$/.test(file) && /^anchors_(?:left|right|top|bottom)\s*=/m.test(body))
        issues.push(`${file}: Godot 3 Control uses anchor_left/right/top/bottom, not anchors_*.`);
      if (major !== 4 && project.engine === "godot" && /\.tscn$/.test(file) && /^offset_(?:left|right|top|bottom)\s*=/m.test(body))
        issues.push(`${file}: Godot 3 Control uses margin_left/right/top/bottom, not offset_*.`);
      const ids = new Set([...body.matchAll(/\[ext_resource[^\]]*\bid=(\d+)[^\]]*\]/g)].map((m) => m[1]));
      for (const [, id] of body.matchAll(/ExtResource\("?(\d+)"?\)/g))
        if (!ids.has(id)) issues.push(`${file}: ExtResource(${id}) has no declaration.`);
    }
  }
  const config = files.includes("project.godot") ? await fsp.readFile(path.join(overlay, "project.godot"), "utf8") : "";
  const main = config.match(/^run\/main_scene="res:\/\/([^"]+)"/m)?.[1];
  if (main && !exists(path.join(overlay, main)) && !exists(path.join(recovered, main)))
    issues.push(`project.godot: main scene res://${main} does not exist.`);
  if (main && exists(path.join(overlay, main))) {
    const scene = await fsp.readFile(path.join(overlay, main), "utf8");
    const script = scene.match(/\[ext_resource[^\]]*path="res:\/\/([^"]+\.gd)"/)?.[1];
    if (script && exists(path.join(overlay, script))) {
      const code = await fsp.readFile(path.join(overlay, script), "utf8");
      if (/TODO:\s*(?:Load|Start|Implement).*game/i.test(code)) issues.push(`${script}: main scene still has a game-start TODO.`);
    }
  }
  return issues;
}

// The res:// paths inside a Godot pack (a .pck, or an executable with an embedded pack).
export function godotPackFiles(file) {
  return new Promise((resolve, reject) => {
    const child = spawn("gdre_tools", ["--headless", `--list-files=${file}`], { env: process.env });
    let out = "";
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.resume();
    child.on("error", reject);
    child.on("close", (code) => code === 0
      ? resolve(new Set(out.split("\n").map((l) => l.trim()).filter((l) => l.startsWith("res://"))))
      : reject(new Error(`gdre_tools --list-files exited with ${code}`)));
  });
}

// Exported games load what a `.remap` points at (X.gd.remap → X.gdc, X.tscn.remap → a binary .scn)
// and read project.binary before project.godot, so a patched X.gd / X.tscn / project.godot is
// silently ignored unless those go. Earlier mods hand-zeroed pack entries to work around this.
async function godotPatchPlan(p, list, packFiles, log) {
  const excludes = [], extra = [], notes = [];
  for (const f of list) {
    const remap = `res://${f}.remap`;
    if (!packFiles.has(remap)) continue;
    if (/[*?[\]]/.test(remap)) {
      // Not safely expressible as an --exclude glob: point the remap at the new file instead.
      const self = path.join(MODS, p.id, "build-remaps", `${f}.remap`);
      await fsp.mkdir(path.dirname(self), { recursive: true });
      await fsp.writeFile(self, `[remap]\n\npath="res://${f}"\n`);
      extra.push(`--patch-file=${self}=${remap}`);
    } else excludes.push(remap);
    notes.push(`res://${f} replaces the game's compiled copy (dropped ${remap})`);
  }
  if (list.includes("project.godot") && packFiles.has("res://project.binary")) {
    excludes.push("res://project.binary");
    notes.push("project.godot replaces the game's project.binary (exported games read the binary first)");
  }
  for (const n of notes) log.write(`• ${n}\n`);
  return { excludes, extra, notes };
}

// The packaged pack really holds the mod's files and none of the entries it had to drop.
async function verifyGodotPack(file, list, plan, log) {
  const files = await godotPackFiles(file);
  const missing = list.filter((f) => !files.has(`res://${f}`));
  const stale = plan.excludes.filter((x) => files.has(x));
  if (missing.length) throw new Error(`The packaged pack is missing ${missing.length} mod file(s): ${missing.slice(0, 5).map((f) => `res://${f}`).join(", ")}`);
  if (stale.length) throw new Error(`The packaged pack still has ${stale.slice(0, 5).join(", ")}, so the game would keep loading its original files`);
  log.write(`Verified the packaged pack: ${list.length} mod file(s) present${plan.excludes.length ? `, ${plan.excludes.length} superseded entr${plan.excludes.length === 1 ? "y" : "ies"} removed` : ""}\n`);
}

async function readHead(file, bytes = 1024, fromEnd = false) {
  const fd = await fsp.open(file, "r").catch(() => null);
  if (!fd) return Buffer.alloc(0);
  try {
    const { size } = await fd.stat();
    const n = Math.min(bytes, size);
    const buf = Buffer.alloc(n);
    await fd.read(buf, 0, n, fromEnd ? size - n : 0);
    return buf;
  } finally { await fd.close(); }
}

async function peMachine(file) {
  const head = await readHead(file);
  if (head.length < 64 || head.subarray(0, 2).toString() !== "MZ") return null;
  const pe = head.readUInt32LE(0x3c);
  return pe + 6 < head.length ? head.readUInt16LE(pe + 4) : null;
}

// Every match by default: a fixed cap of ~50 once made builds, syntax checks and the test-run
// fingerprint see only the first files (a mod with 5,000 copied .import files had its real
// script edits ignored). Pass `limit` for quick existence probes of big game folders.
async function findFiles(dir, test, maxDepth = 4, limit = Infinity, base = dir, depth = 0, out = []) {
  if (depth > maxDepth || out.length >= limit) return out;
  for (const e of await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])) {
    if (out.length >= limit) break;
    const full = path.join(dir, e.name);
    if (e.isSymbolicLink()) continue;
    if (e.isDirectory()) await findFiles(full, test, maxDepth, limit, base, depth + 1, out);
    else if (test(rel(base, full), e.name)) out.push(rel(base, full));
  }
  return out;
}

// ------------------------------------------------------------------------- detection --
// What kind of game a folder holds and everything its adapter needs to know, plus the engine version.
export async function detectGame(gameDir) {
  const detected = await detectGameKind(gameDir);
  const v = await engineVersion(gameDir).catch(() => null);
  if (!v) return detected;
  // Drop words the label already says ("RPG Maker MV" + "MV 1.6.0" → "1.6.0").
  const said = new Set(detected.label.split(/[\s()]+/)), words = v.short.split(" ");
  while (words.length > 1 && said.has(words[0])) words.shift();
  const short = words.join(" ");
  return { ...detected, engineVersion: short, engineVersionLabel: v.label };
}
async function detectGameKind(gameDir) {
  const entries = await fsp.readdir(gameDir, { withFileTypes: true }).catch(() => []);
  const names = new Set(entries.map((e) => e.name));
  const files = entries.filter((e) => e.isFile()).map((e) => e.name);

  // Unity
  const dataDir = entries.find((e) => e.isDirectory() && /_Data$/.test(e.name))?.name;
  if (dataDir) {
    const managed = path.join(gameDir, dataDir, "Managed");
    const linux = names.has("UnityPlayer.so") || files.some((f) => /\.x86_64$/.test(f));
    if (names.has("GameAssembly.dll") || names.has("GameAssembly.so")) {
      const platform = names.has("GameAssembly.so") && !names.has("GameAssembly.dll") ? "linux-x64"
        : (await peMachine(path.join(gameDir, "GameAssembly.dll"))) === 0x14c ? "win-x86" : "win-x64";
      return { engine: "unity-il2cpp", label: "Unity (IL2CPP)", platform, dataDir,
        supported: platform !== "win-x86", reason: platform === "win-x86" ? "32-bit IL2CPP games aren't supported by the bundled BepInEx 6" : "" };
    }
    if (exists(path.join(managed, "Assembly-CSharp.dll"))) {
      let platform = linux && !names.has("UnityPlayer.dll") ? "linux-x64" : "win-x64";
      if (names.has("UnityPlayer.dll") && (await peMachine(path.join(gameDir, "UnityPlayer.dll"))) === 0x14c) platform = "win-x86";
      return { engine: "unity-mono", label: "Unity (Mono)", platform, dataDir, managed, supported: true };
    }
  }

  // Unreal: <Project>/Binaries/Win64/<Project>-Win64-Shipping.exe (or WinGDK)
  const shipping = await findFiles(gameDir, (r, n) => /\/Binaries\/Win64\/[^/]+-(Win64|WinGDK)-Shipping\.exe$/i.test("/" + r), 3, 50);
  if (shipping.length) {
    const exe = shipping.sort((a, b) => a.length - b.length)[0];
    return { engine: "unreal", label: "Unreal Engine", platform: "win-x64", supported: true,
      binRel: path.posix.dirname(exe), exeRel: exe, project: exe.split("/")[0] };
  }
  if ((await findFiles(gameDir, (r) => /\/Binaries\/Linux\/[^/]+-Shipping$/i.test("/" + r), 3, 1)).length)
    return { engine: "unreal", label: "Unreal Engine (Linux build)", supported: false,
      reason: "UE4SS only supports the Windows build. Install the Windows version through Proton to mod it." };

  // Godot: a .pck next to the program, or one embedded at the end of the program (ends with GDPC).
  const pck = files.find((f) => /\.pck$/i.test(f));
  if (pck) return { engine: "godot", label: "Godot", supported: true, pckRel: pck };
  for (const f of files.filter((f) => /\.(exe|x86_64|x86|arm64)$/i.test(f) || !f.includes("."))) {
    const tail = await readHead(path.join(gameDir, f), 4, true);
    if (tail.toString() === "GDPC") return { engine: "godot", label: "Godot (embedded pack)", supported: true, pckRel: f };
  }

  // RPG Maker MV / MZ
  for (const [jsDir, core, version] of [["www/js", "rpg_core.js", "MV"], ["js", "rpg_core.js", "MV"], ["js", "rmmz_core.js", "MZ"]])
    if (exists(path.join(gameDir, jsDir, core)) && exists(path.join(gameDir, jsDir, "plugins.js")))
      return { engine: "rpgmaker", label: `RPG Maker ${version}`, supported: true, jsDir, version };

  // Ren'Py
  if (names.has("renpy") && names.has("game")) return { engine: "renpy", label: "Ren'Py", supported: true };

  // Electron / NW.js
  if (exists(path.join(gameDir, "resources/app.asar"))) return { engine: "electron", label: "Electron", supported: true, asarRel: "resources/app.asar" };
  if (exists(path.join(gameDir, "resources/app/package.json"))) return { engine: "electron", label: "Electron (unpacked)", supported: true, appRel: "resources/app" };
  if (names.has("package.nw") || (names.has("package.json") && names.has("nw.dll")))
    return { engine: "electron", label: "NW.js", supported: true, ...(names.has("package.nw") ? { asarRel: null, nwRel: "package.nw" } : { appRel: "." }) };

  // Keep the Files and Mods views in agreement for engines without a guided
  // adapter. The generic adapter still handles their file overlays.
  const markers = await findFiles(gameDir, (rel) => isEngineMarker(rel), 6);
  let hint = detectEngineHints(markers.map((rel) => ({ rel })))[0];
  if (!hint) {
    const assets = await findFiles(gameDir, (rel) => /\.(png|jpe?g|webp|dds|ogg|wav|mp3|flac|glb|gltf|obj|fbx|ttf|otf|pack|dat)$/i.test(rel), 4, 1);
    const jars = await findFiles(gameDir, (rel) => /\.jar$/i.test(rel), 4, 1);
    const executables = files.filter((rel) => /\.(exe|x86_64|x86|arm64)$/i.test(rel) || !rel.includes("."));
    const candidates = await Promise.all(executables.map(async (rel) => {
      const head = await readHead(path.join(gameDir, rel), 4);
      const format = head.subarray(0, 2).toString() === "MZ" ? "PE EXE"
        : head.toString("hex") === "7f454c46" ? "ELF" : "";
      return { rel, kind: format ? "executable" : "other", format };
    }));
    hint = detectEngineHints([...candidates, ...jars.map((rel) => ({ rel })), ...assets.map((rel) => ({ rel }))])[0];
  }
  return { engine: "generic", label: hint?.name || "Unknown engine", supported: true,
    ...(hint ? { evidence: hint.evidence } : {}),
    reason: "No guided mod loader is available: use file overlays and REA's analysis tools to inspect the game." };
}

// --------------------------------------------------------------------------- adapters --
const csClass = (name) => name.replace(/[^A-Za-z0-9]+/g, " ").trim().split(/\s+/).filter(Boolean)
  .map((w) => w[0].toUpperCase() + w.slice(1)).join("").replace(/^[0-9]+/, "") || "RocsyMod";

const proton = (dll) => `Windows game (Proton): in Steam open the game's Properties → Launch Options and set\nWINEDLLOVERRIDES="${dll}=n,b" %command%\nso the mod loader (${dll}.dll) runs.`;

// Each adapter: label/hints for the page, recipes, template(project), extract(project) → job steps,
// build(project) → job steps that end with install/ filled, loader(project) → install/ with only
// the loader (engines whose code only exists after a first run), launch(project) text.
const ADAPTERS = {
  "unity-mono": {
    recipes: ["bullet-time", "spawn-menu", "blank"],
    extractLabel: "Decompile the game", extractHint: "ILSpy turns the game's scripts back into C# so you and the agent can read how it works.",
    buildHint: "Compiles src/ into a BepInEx 5 plugin.",
    launch: (p) => p.platform.startsWith("win") ? proton("winhttp") : "Linux game: set the Steam launch option ./run_bepinex.sh %command%",
    template: (p) => csTemplate(p, "unity-mono", "net472", [
      `${TOOLS}/bepinex/${p.platform}/BepInEx/core/BepInEx.dll`, `${TOOLS}/bepinex/${p.platform}/BepInEx/core/0Harmony.dll`,
      `${p.managed}/UnityEngine*.dll`, `${p.managed}/Unity.*.dll`, `${p.managed}/Assembly-CSharp*.dll`], true),
    extract: (p) => ilspySteps(p, p.managed),
    build: (p) => csBuild(p, `${TOOLS}/bepinex/${p.platform}`),
  },
  "unity-il2cpp": {
    recipes: ["bullet-time", "spawn-menu", "blank"], needsLoader: true,
    loaderHint: "IL2CPP games have no C# to decompile until BepInEx 6 runs once: install the loader, start the game, wait for the main menu (the first start takes a few minutes while it generates the interop assemblies), then close it.",
    extractLabel: "Decompile the interop assemblies", extractHint: "ILSpy reads the wrappers BepInEx generated: every class, field and method signature (bodies stay native; the agent reads them with Ghidra in GameAssembly).",
    buildHint: "Compiles src/ into a BepInEx 6 IL2CPP plugin against this game's interop assemblies.",
    launch: (p) => p.platform.startsWith("win") ? proton("winhttp") : "Linux game: set the Steam launch option ./run_bepinex.sh %command%",
    ready: (p) => exists(path.join(p.gamePath, "BepInEx/interop/Assembly-CSharp.dll")),
    notReady: "Install the loader and start the game once first (step 1): BepInEx/interop/ doesn't exist yet.",
    template: (p) => csTemplate(p, "unity-il2cpp", "net6.0", [
      ...["BepInEx.Core", "BepInEx.Unity.IL2CPP", "BepInEx.Unity.Common", "Il2CppInterop.Runtime", "Il2CppInterop.Common", "0Harmony"]
        .map((n) => `${TOOLS}/bepinex6/${p.platform}/BepInEx/core/${n}.dll`),
      `${p.gamePath}/BepInEx/interop/*.dll`], false),
    extract: (p) => ilspySteps(p, path.join(p.gamePath, "BepInEx/interop")),
    build: (p) => csBuild(p, `${TOOLS}/bepinex6/${p.platform}`),
    loader: (p) => [async (log) => { await fresh(installDir(p)); await copyTree(`${TOOLS}/bepinex6/${p.platform}`, installDir(p)); log.write("install/ = BepInEx 6 loader only\n"); }],
  },
  unreal: {
    recipes: ["bullet-time", "spawn-menu", "blank"], needsLoader: true,
    loaderHint: "Install UE4SS (the loader, with its GUI console on), start the game, and in the UE4SS window's Dumpers tab press “Dump CXX Headers” and “Generate Lua Types”, then close the game. That gives the agent every class and function of this game.",
    extractLabel: "Index the UE4SS dumps", extractHint: "Copies the class/function dumps UE4SS wrote into the game folder into decompiled/ so you and the agent can search them.",
    buildHint: "Checks the Lua and packages it as a UE4SS mod.",
    launch: () => proton("dwmapi"),
    template: async (p) => {
      const src = path.join(MODS, p.id, "src");
      await fsp.mkdir(path.join(src, "Scripts"), { recursive: true });
      await writeRecipe(p, "unreal", p.recipe, path.join(src, "Scripts/main.lua"));
      if (p.recipe === "spawn-menu") await fsp.writeFile(path.join(src, "classes.txt"),
        "# One class path per line, e.g. /Game/Props/BP_Crate.BP_Crate_C\n# Empty = every loaded Blueprint class whose name starts with BP_\n");
    },
    extract: (p) => {
      const ue4ss = path.join(p.gamePath, p.binRel, "ue4ss");
      const out = path.join(MODS, p.id, "decompiled");
      return [async (log) => {
        await fresh(out);
        let found = 0;
        for (const part of ["CXXHeaderDump", "Mods/shared/types", "UE4SS_ObjectDump.txt"]) {
          const from = path.join(ue4ss, part);
          if (!exists(from)) continue;
          await fsp.cp(from, path.join(out, "ue4ss", part), { recursive: true });
          found++;
          log.write(`copied ue4ss/${part}\n`);
        }
        const paks = await findFiles(p.gamePath, (r) => /\.(pak|utoc|ucas)$/i.test(r), 5);
        await fsp.writeFile(path.join(out, "paks.txt"), paks.join("\n") + "\n");
        if (!found) throw new Error("No UE4SS dumps in the game folder yet: do step 1 (install the loader, start the game, use the Dumpers tab)");
      }];
    },
    build: (p) => {
      const src = path.join(MODS, p.id, "src");
      return [
        async (log) => {
          const lua = await findFiles(src, (r) => r.endsWith(".lua"), 6);
          if (!lua.length) throw new Error("src/Scripts/main.lua is missing");
          for (const f of lua) await run(["luac5.4", "-p", path.join(src, f)], log);
          await fresh(installDir(p));
          await unrealLoader(p);
          const modDir = path.join(installDir(p), p.binRel, "ue4ss/Mods", p.className);
          await fsp.cp(src, modDir, { recursive: true });
          await fsp.writeFile(path.join(modDir, "enabled.txt"), "");
          log.write(`Packaged ${p.binRel}/ue4ss/Mods/${p.className}/ (UE4SS + this mod)\n`);
        }];
    },
    loader: (p) => [async (log) => { await fresh(installDir(p)); await unrealLoader(p); log.write("install/ = UE4SS loader only (GUI console on)\n"); }],
  },
  godot: {
    recipes: ["bullet-time", "blank"],
    extractLabel: "Recover the Godot project", extractHint: "GDRE Tools recovers the game's scripts (.gd), scenes and project settings from its pack.",
    buildHint: "Writes a copy of the game's pack with every file in src/files/ added or replaced (src/files/scripts/x.gd → res://scripts/x.gd).",
    launch: () => "No launch option needed: Install replaces the game's pack (the original is backed up).",
    template: async (p) => {
      const files = path.join(MODS, p.id, "src/files");
      await fsp.mkdir(path.join(files, "rocsy"), { recursive: true });
      await writeRecipe(p, "godot", p.recipe, path.join(files, "rocsy", `${p.className}.gd`));
    },
    extract: (p) => [async () => fresh(path.join(MODS, p.id, "decompiled")),
      async (log) => run(["gdre_tools", `--recover=${await originalFile(p)}`, `--output=${path.join(MODS, p.id, "decompiled/project")}`], log, path.join(MODS, p.id))],
    build: (p) => [async (log) => {
      const files = path.join(MODS, p.id, "src/files");
      const all = await findFiles(files, () => true, 12);
      // .import/.import/ is an import folder copied into itself: the engine never reads it.
      const list = all.filter((f) => !/^\.import\/\.import\//.test(f));
      if (list.length < all.length) log.write(`• Left out ${all.length - list.length} file(s) under src/files/.import/.import/ (an import folder copied into itself; delete it)\n`);
      if (!list.length) throw new Error("src/files/ is empty: nothing to patch");
      const problems = await inspectGodotSource(p, list);
      if (problems.length) throw new Error(`Godot source validation failed: ${problems.join(" ")}`);
      // Not fatal (a total conversion may never load those scripts), but the usual cause of runtime errors.
      for (const gap of [...await autoloadGaps(p, list), ...await missingResPaths(p, list)]) log.write(`⚠ ${gap}\n`);
      await logUnchangedCopies(p, list, log);
      const scratch = path.join(MODS, p.id, `build-patched-${randomBytes(4).toString("hex")}${path.extname(p.pckRel) || ".pck"}`);
      const original = await originalFile(p);
      if (original !== path.join(p.gamePath, p.pckRel)) log.write(`• A mod is installed in the game: building from the original ${p.pckRel} kept in the install backup\n`);
      const out = path.join(installDir(p), p.pckRel);
      const originalHead = await readHead(original, 4);
      const embedded = originalHead.toString() !== "GDPC";
      try {
        logPatchedFiles(list, log);
        const plan = await godotPatchPlan(p, list, await godotPackFiles(original), log);
        await run(["gdre_tools", `--pck-patch=${original}`,
          ...list.map((f) => `--patch-file=${path.join(files, f)}=res://${f}`), ...plan.extra,
          ...plan.excludes.map((x) => `--exclude=${x}`),
          ...(embedded ? [`--embed=${original}`] : []), `--output=${scratch}`], log);
        const head = await readHead(scratch, 4);
        const tail = await readHead(scratch, 12, true);
        if (embedded && (head.subarray(0, 2).toString() !== originalHead.subarray(0, 2).toString()
          || tail.subarray(8).toString() !== "GDPC"))
          throw new Error("GDRE did not preserve the executable and embedded pack; refusing to install the output");
        if (!embedded && head.toString() !== "GDPC") throw new Error("GDRE did not produce a valid standalone PCK");
        await verifyGodotPack(scratch, list, plan, log);
        await fresh(installDir(p));
        await fsp.mkdir(path.dirname(out), { recursive: true });
        await fsp.rename(scratch, out);
        log.write(`Packaged install/${p.pckRel} (${list.length} files patched; ${embedded ? "original executable preserved with replacement embedded pack" : "PCK"}; ${(await fsp.stat(out)).size} bytes)\n`);
      } finally { await fsp.rm(scratch, { force: true }); }
    }],
  },
  rpgmaker: {
    recipes: ["bullet-time", "blank"],
    extractLabel: "Copy the game's scripts", extractHint: "RPG Maker games ship plain JavaScript: copies js/ and the data files into decompiled/ for searching.",
    buildHint: "Checks the plugin JavaScript and registers it in plugins.js.",
    launch: () => "No launch option needed: the plugin is registered in js/plugins.js (the original is backed up).",
    template: async (p) => {
      const dir = path.join(MODS, p.id, "src/files", p.jsDir, "plugins");
      await fsp.mkdir(dir, { recursive: true });
      await writeRecipe(p, "rpgmaker", p.recipe, path.join(dir, `${p.className}.js`));
    },
    extract: (p) => [async (log) => {
      const out = path.join(MODS, p.id, "decompiled");
      await fresh(out);
      await fsp.cp(path.join(p.gamePath, p.jsDir), path.join(out, p.jsDir), { recursive: true });
      const data = path.join(path.dirname(path.join(p.gamePath, p.jsDir)), "data");
      if (exists(data)) await fsp.cp(data, path.join(out, rel(p.gamePath, data)), { recursive: true });
      log.write(`copied ${p.jsDir}/ and data/\n`);
    }],
    build: (p) => [async (log) => {
      const files = path.join(MODS, p.id, "src/files");
      const js = await findFiles(files, (r) => r.endsWith(".js"), 12);
      for (const f of js) await run(["node", "--check", path.join(files, f)], log);
      await fresh(installDir(p));
      await fsp.cp(files, installDir(p), { recursive: true });
      // Register every plugin of this mod in a copy of the game's plugins.js.
      const pluginsJs = path.join(p.gamePath, p.jsDir, "plugins.js");
      const text = await fsp.readFile(pluginsJs, "utf8");
      const match = text.match(/\$plugins\s*=\s*(\[[\s\S]*\])\s*;?\s*$/);
      if (!match) throw new Error("Couldn't read the game's plugins.js");
      const list = JSON.parse(match[1]);
      for (const f of js.filter((f) => f.startsWith(`${p.jsDir}/plugins/`))) {
        const name = path.basename(f, ".js");
        if (!list.some((x) => x.name === name)) list.push({ name, status: true, description: `${p.name} (Rocsy REA mod)`, parameters: {} });
      }
      const out = path.join(installDir(p), p.jsDir, "plugins.js");
      await fsp.mkdir(path.dirname(out), { recursive: true });
      await fsp.writeFile(out, `// Generated by RPG Maker, edited by Rocsy REA (${p.name}).\nvar $plugins =\n${JSON.stringify(list, null, 0).replace(/},{/g, "},\n{")};\n`);
      log.write(`Packaged ${js.length} plugin file(s) and ${p.jsDir}/plugins.js\n`);
    }],
  },
  renpy: {
    recipes: ["blank"],
    extractLabel: "Extract the game's scripts", extractHint: "Copies game/*.rpy and turns compiled .rpyc scripts back into .rpy with unrpyc. (Scripts packed inside .rpa archives aren't extracted.)",
    buildHint: "Copies src/files/ (e.g. game/zz_mymod.rpy) into the package.",
    launch: () => "No launch option needed: Ren'Py loads every .rpy in game/.",
    template: async (p) => {
      const dir = path.join(MODS, p.id, "src/files/game");
      await fsp.mkdir(dir, { recursive: true });
      await writeRecipe(p, "renpy", p.recipe, path.join(dir, `zz_${p.className.toLowerCase()}.rpy`));
    },
    extract: (p) => [async (log) => {
      const out = path.join(MODS, p.id, "decompiled");
      await fresh(out);
      const scripts = await findFiles(path.join(p.gamePath, "game"), (r) => /\.rpyc?$/.test(r), 10);
      for (const f of scripts) {
        const to = path.join(out, "game", f);
        await fsp.mkdir(path.dirname(to), { recursive: true });
        await fsp.copyFile(path.join(p.gamePath, "game", f), to);
      }
      const compiled = scripts.filter((f) => f.endsWith(".rpyc") && !scripts.includes(f.slice(0, -1)));
      if (compiled.length) await run(["unrpyc", ...compiled.map((f) => path.join(out, "game", f))], log);
      log.write(`${scripts.length} script files, ${compiled.length} decompiled\n`);
    }],
    build: (p) => overlayBuild(p),
  },
  electron: {
    recipes: ["blank"],
    extractLabel: "Extract the app", extractHint: "Unpacks the app's JavaScript (app.asar) into decompiled/ for searching.",
    buildHint: "Applies src/files/ on top of the app and repacks it (app.asar), or copies the files for unpacked apps.",
    launch: () => "No launch option needed: Install replaces the app files (originals are backed up).",
    template: async (p) => { await fsp.mkdir(path.join(MODS, p.id, "src/files"), { recursive: true }); await writeRecipe(p, "generic", "blank", path.join(MODS, p.id, "src/files/README-ROCSY.txt")); },
    extract: (p) => [async () => fresh(path.join(MODS, p.id, "decompiled")),
      p.asarRel ? ["asar", "extract", path.join(p.gamePath, p.asarRel), path.join(MODS, p.id, "decompiled/app")]
        : async () => fsp.cp(path.join(p.gamePath, p.appRel || p.nwRel), path.join(MODS, p.id, "decompiled/app"), { recursive: true })],
    build: (p) => p.asarRel ? [async (log) => {
      const files = path.join(MODS, p.id, "src/files");
      const work = path.join(MODS, p.id, "build/app");
      if (!exists(path.join(MODS, p.id, "decompiled/app"))) throw new Error("Extract the app first (step 1)");
      await fresh(path.join(MODS, p.id, "build"));
      await fsp.cp(path.join(MODS, p.id, "decompiled/app"), work, { recursive: true });
      if (exists(files)) await fsp.cp(files, work, { recursive: true, filter: (s) => !s.endsWith("README-ROCSY.txt") });
      await fresh(installDir(p));
      await fsp.mkdir(path.join(installDir(p), path.dirname(p.asarRel)), { recursive: true });
      await run(["asar", "pack", work, path.join(installDir(p), p.asarRel)], log);
      log.write(`Packaged install/${p.asarRel}\n`);
    }] : overlayBuild(p, p.appRel || "."),
  },
  generic: {
    recipes: ["blank"],
    extractLabel: "Analyse the game", extractHint: "No extractor for this engine: use the Files tab (Ghidra) and the Architecture tab, or ask the agent to map it. This step just records that.",
    buildHint: "Copies src/files/ (paths relative to the game folder) into the package.",
    launch: () => "No launch option needed: Install copies the files into the game folder (originals are backed up).",
    template: async (p) => { await fsp.mkdir(path.join(MODS, p.id, "src/files"), { recursive: true }); await writeRecipe(p, "generic", "blank", path.join(MODS, p.id, "src/README-ROCSY.txt")); },
    extract: (p) => [async (log) => { await fsp.mkdir(path.join(MODS, p.id, "decompiled"), { recursive: true }); log.write("Nothing to extract for this engine\n"); }],
    build: (p) => overlayBuild(p),
  },
};

const installDir = (p) => path.join(MODS, p.id, "install");

async function fresh(dir) {
  await fsp.rm(dir, { recursive: true, force: true });
  await fsp.mkdir(dir, { recursive: true });
}

async function copyTree(from, to) {
  await fsp.cp(from, to, { recursive: true, filter: (src) => !/changelog\.txt$/i.test(src) });
}

// UE4SS goes next to the game's Shipping exe; its GUI console gives the user the Dumpers tab.
async function unrealLoader(p) {
  const dest = path.join(installDir(p), p.binRel);
  await fsp.mkdir(dest, { recursive: true });
  await copyTree(`${TOOLS}/ue4ss`, dest);
  const ini = path.join(dest, "ue4ss/UE4SS-settings.ini");
  if (exists(ini)) {
    const text = (await fsp.readFile(ini, "utf8"))
      .replace(/^(\s*ConsoleEnabled\s*=\s*)\d/m, "$11").replace(/^(\s*GuiConsoleEnabled\s*=\s*)\d/m, "$11")
      .replace(/^(\s*GuiConsoleVisible\s*=\s*)\d/m, "$11");
    await fsp.writeFile(ini, text);
  }
}

async function writeRecipe(p, engine, recipe, to) {
  const file = path.join(RECIPES, engine, `${recipe}.${{ unreal: "lua", godot: "gd", rpgmaker: "js", renpy: "rpy", generic: "txt" }[engine] || "cs"}`);
  const text = await fsp.readFile(file, "utf8");
  await fsp.writeFile(to, text.replaceAll("{{GUID}}", `rocsy.mods.${p.className.toLowerCase()}`)
    .replaceAll("{{NAME}}", p.name.replace(/"/g, "'")).replaceAll("{{CLASS}}", p.className));
}

async function csTemplate(p, engine, framework, references, netfxRefs) {
  const src = path.join(MODS, p.id, "src");
  await fsp.mkdir(src, { recursive: true });
  await fsp.writeFile(path.join(src, `${p.className}.csproj`), `<Project Sdk="Microsoft.NET.Sdk">
  <!-- Rocsy REA mod (${engine}). Build it on the REA page or POST /REA/api/mods/${p.id}/build. -->
  <PropertyGroup>
    <TargetFramework>${framework}</TargetFramework>
    <AssemblyName>${p.className}</AssemblyName>
    <RootNamespace>${p.className}</RootNamespace>
    <LangVersion>latest</LangVersion>
    <Nullable>disable</Nullable>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <CopyLocalLockFileAssemblies>false</CopyLocalLockFileAssemblies>
  </PropertyGroup>
${netfxRefs ? `  <ItemGroup>
    <PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies" Version="1.0.3" PrivateAssets="all" />
  </ItemGroup>
` : ""}  <ItemGroup>
${references.map((r) => `    <Reference Include="${r}" Private="false" />`).join("\n")}
  </ItemGroup>
</Project>
`);
  await writeRecipe(p, engine, p.recipe, path.join(src, "Plugin.cs"));
}

// Each assembly is decompiled once per game version: a still-current copy in the game's
// knowledge base (decomp.mjs) is linked in instead of running ILSpy again, and fresh output is
// recorded there for the next project and the agent.
function ilspySteps(p, dir) {
  const out = path.join(MODS, p.id, "decompiled");
  return [async () => fresh(out), async (log) => {
    if (!exists(dir)) throw new Error(`${dir} doesn't exist`);
    const dlls = (await fsp.readdir(dir)).filter((f) => f.endsWith(".dll") && !SKIP_ASSEMBLY.test(f));
    dlls.sort((a, b) => (b.startsWith("Assembly-CSharp") - a.startsWith("Assembly-CSharp")) || a.localeCompare(b));
    // Only the Rules tab's hot files (Assembly-CSharp + firstpass): SDKs like GalaxyCSharp sent agents chasing noise.
    const relOf = (d) => rel(p.gamePath, path.join(dir, d));
    const keep = new Set(pickAssemblies(p.engine, dlls.map(relOf)));
    const left = dlls.filter((d) => !keep.has(relOf(d)));
    if (left.length) log.write(`Not decompiled (Rules tab): ${left.join(", ")}\n`);
    for (const dll of dlls.filter((d) => keep.has(relOf(d))).slice(0, 12)) {
      const dest = path.join(out, dll.replace(/\.dll$/, ""));
      const relDll = rel(p.gamePath, path.join(dir, dll));
      const done = await lookupDecomp(p.gamePath, relDll, "ilspy").catch(() => null);
      if (done) {
        await linkTree(done.dir, dest);
        log.write(`${dll}: already decompiled (${done.tool.files ?? "?"} files, ${new Date(done.tool.updated).toISOString()}), reused from the knowledge base\n`);
        await recordDecomp(p.gamePath, { rel: relDll, tool: "ilspy", by: "mods", project: p.id, action: "reused" }).catch(() => {});
        continue;
      }
      await recordDecomp(p.gamePath, { rel: relDll, tool: "ilspy", by: "mods", project: p.id, status: "partial", action: "started" }).catch(() => {});
      try {
        await run(["ilspycmd", "-p", "-o", dest, "-r", dir, path.join(dir, dll)], log);
      } catch (error) {
        await recordDecomp(p.gamePath, { rel: relDll, tool: "ilspy", by: "mods", project: p.id, status: "failed", note: error.message, action: "failed" }).catch(() => {});
        throw error;
      }
      await saveDecomp(p, relDll, "ilspy", dest, log);
    }
  }];
}

// Copy (hard link) a finished output into the knowledge base and mark it complete.
async function saveDecomp(p, relSrc, tool, from, log) {
  try {
    const key = await ensureKbForGame(p.gamePath);
    const to = decompDir(key, relSrc, tool);
    await fsp.rm(to, { recursive: true, force: true });
    await linkTree(from, to);
    await recordDecomp(p.gamePath, { rel: relSrc, tool, by: "mods", project: p.id, status: "complete", files: await countFiles(to) });
  } catch (error) { log.write(`(couldn't record the decompile progress: ${error.message})\n`); }
}

// What an engine's extract step decompiles, for the progress record (Unity's ILSpy records per assembly).
function extractUnit(p) {
  if (p.engine === "godot") return { rel: p.pckRel, tool: "gdre", out: "project" };
  if (p.engine === "electron") return { rel: p.asarRel || p.appRel || p.nwRel, tool: p.asarRel ? "asar" : "copy", out: "app" };
  if (p.engine === "renpy") return { rel: "game", tool: "unrpyc", out: "game" };
  if (p.engine === "rpgmaker") return { rel: p.jsDir, tool: "copy", out: "" };
  if (p.engine === "unreal") return { rel: p.binRel, tool: "ue4ss-dump", out: "" };
  return null;
}

// Before dotnet: a mod with no plugin entry point compiles fine and then never loads (a Unity
// session's Plugin.cs lost its [BepInPlugin] class and 8 test runs "passed"), and classes nothing creates
// never run (its CSMatch/CSEconomy/... were never instantiated). The first fails the build, the second warns.
async function csSourceCheck(p, log) {
  const src = path.join(MODS, p.id, "src");
  const files = (await findFiles(src, (f) => f.endsWith(".cs"), 8)).filter((f) => !/^(obj|bin)\//.test(f));
  const code = new Map();
  for (const f of files) code.set(f, (await fsp.readFile(path.join(src, f), "utf8")).replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, ""));
  const all = [...code.values()].join("\n");
  const il2cpp = (p.engine || "").includes("il2cpp");
  if (!/\[\s*BepInPlugin\s*\(/.test(all) || !(il2cpp ? /:\s*(BasePlugin|BaseUnityPlugin)\b/ : /:\s*BaseUnityPlugin\b/).test(all))
    throw new Error(`No plugin entry point in src/: BepInEx only loads a class with [BepInPlugin("id", "name", "1.0.0")] that ` +
      `extends ${il2cpp ? "BasePlugin" : "BaseUnityPlugin"} (with Awake() calling new Harmony("id").PatchAll()). Without it the DLL builds but never loads.`);
  await patchTargetCheck(p, all, log);
  const unused = [];
  for (const [f, text] of code) for (const m of text.matchAll(/\b(?:class|struct)\s+([A-Za-z_]\w*)/g)) {
    const name = m[1], uses = all.match(new RegExp(`\\b${name}\\b`, "g"))?.length || 0;
    const patched = new RegExp(`\\[\\s*HarmonyPatch[^\\]]*\\][\\s\\S]{0,200}class\\s+${name}\\b`).test(text);
    if (uses <= 1 && !patched && !new RegExp(`class\\s+${name}\\s*:\\s*BaseUnityPlugin|class\\s+${name}\\s*:\\s*BasePlugin`).test(text)) unused.push(`${name} (${f})`);
  }
  if (unused.length) log.write(`WARNING: declared but never used anywhere in src/, so this code never runs: ${unused.join(", ")}. ` +
    `Create it from the plugin or a patched game method (AddComponent, new, a static call).\n`);
}

// Harmony/reflection targets must exist: a guessed `AccessTools.Method(typeof(LoadLevel), "loadLevel")`
// built fine and then failed at runtime with "Null method for <guid>" (seen in a Unity mod run).
// Checks every typeof(T), "M" pair against the decompiled game type's declared methods.
async function patchTargetCheck(p, all, log) {
  const root = path.join(MODS, p.id, "decompiled");
  const pairs = new Set();
  for (const m of all.matchAll(/typeof\(\s*([A-Za-z_][\w.]*)\s*\)\s*,\s*"([A-Za-z_]\w*)"/g)) pairs.add(`${m[1]}|${m[2]}`);
  for (const m of all.matchAll(/typeof\(\s*([A-Za-z_][\w.]*)\s*\)\s*\.\s*GetMethod\(\s*"([A-Za-z_]\w*)"/g)) pairs.add(`${m[1]}|${m[2]}`);
  if (!pairs.size) return;
  const files = await findFiles(root, (f) => f.endsWith(".cs"), 6);
  const missing = [];
  for (const pair of pairs) {
    const [type, method] = pair.split("|");
    const short = type.split(".").pop();
    const file = files.find((f) => path.basename(f) === `${short}.cs` && /^Assembly-CSharp/.test(f));
    if (!file) continue;                                    // a Unity/BepInEx type, or not decompiled: can't tell
    const text = await fsp.readFile(path.join(root, file), "utf8").catch(() => "");
    const declared = new Set([...text.matchAll(/^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract|sealed|async|new|unsafe|extern)\s+)+[\w<>\[\],.? ]+?\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(/gm)].map((m) => m[1]));
    if (!declared.size || declared.has(method)) continue;
    const near = [...declared].sort((a, b) => (b.toLowerCase().includes(method.toLowerCase().slice(0, 4)) - a.toLowerCase().includes(method.toLowerCase().slice(0, 4))) || a.localeCompare(b)).slice(0, 25);
    missing.push(`${type}.${method} (decompiled/${file} declares no method "${method}"; methods there include: ${near.join(", ")})`);
  }
  if (missing.length) throw new Error(`Patch/reflection target(s) that don't exist in the game: ${missing.join("; ")}. ` +
    `Harmony fails at runtime with "Null method" for these. Use a real method name from the decompiled file (names are case-sensitive).`);
}

function csBuild(p, loader) {
  const dir = path.join(MODS, p.id);
  const outDir = path.join(dir, "build");
  return [
    async (log) => csSourceCheck(p, log),
    async () => fsp.rm(outDir, { recursive: true, force: true }),
    ["dotnet", "build", path.join(dir, "src"), "-c", "Release", "-o", outDir, "-nologo", "-clp:NoSummary;ErrorsOnly"],
    async (log) => {
      await fresh(installDir(p));
      await copyTree(loader, installDir(p));
      const plugin = path.join(installDir(p), "BepInEx", "plugins", p.className);
      await fsp.mkdir(plugin, { recursive: true });
      await fsp.copyFile(path.join(outDir, `${p.className}.dll`), path.join(plugin, `${p.className}.dll`));
      log.write(`Packaged install/ (loader + BepInEx/plugins/${p.className}/${p.className}.dll)\n`);
    },
  ];
}

function overlayBuild(p, base = ".") {
  return [async (log) => {
    const files = path.join(MODS, p.id, "src/files");
    const list = await findFiles(files, () => true, 12);
    if (!list.length) throw new Error("src/files/ is empty: put the files to add or replace there, with the same paths as in the game folder");
    for (const f of list.filter((f) => /\.(js|mjs|cjs)$/.test(f))) await run(["node", "--check", path.join(files, f)], log);
    await fresh(installDir(p));
    await fsp.cp(files, path.join(installDir(p), base), { recursive: true });
    log.write(`Packaged ${list.length} file(s)\n`);
  }];
}

// ------------------------------------------------------------------------------- jobs --
// The logged command line: a run of 5+ arguments with the same --flag= (GDRE gets one
// --patch-file per mod file) shows its first 3 and a count; the full list made 1 MB one-line logs.
function shownCommand(cmd) {
  const out = [];
  for (let i = 0; i < cmd.length;) {
    const flag = /^(--[\w-]+=)/.exec(cmd[i])?.[1];
    let j = i + 1;
    while (flag && j < cmd.length && cmd[j].startsWith(flag)) j++;
    if (flag && j - i >= 5) { out.push(...cmd.slice(i, i + 3), `… (+${j - i - 3} more ${flag.slice(0, -1)})`); i = j; }
    else out.push(cmd[i++]);
  }
  return out.join(" ");
}

// Tool output as plain lines: keep what a \r progress bar finally shows, drop ANSI escapes.
function cleanOutput(log) {
  let rest = "";
  const decoder = new StringDecoder("utf8");
  const line = (l) => l.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").split("\r").map((x) => x.trimEnd()).filter(Boolean).pop() || "";
  return {
    write(chunk) {
      const lines = (rest + decoder.write(chunk)).split("\n");
      rest = lines.pop();
      for (const l of lines) { const s = line(l); if (s || !l.trim()) log.write(s + "\n"); }
    },
    end() { const s = line(rest + decoder.end()); if (s) log.write(s + "\n"); rest = ""; },
  };
}

function run(cmd, log, cwd) {
  log.write(`$ ${shownCommand(cmd)}\n`);
  return new Promise((resolve, reject) => {
    const child = spawn("nice", ["-n", "10", ...cmd], { cwd, env: process.env });
    const out = cleanOutput(log);
    child.stdout.on("data", (d) => out.write(d));
    child.stderr.on("data", (d) => out.write(d));
    child.on("error", (e) => reject(e));
    child.on("close", (code) => { out.end(); code === 0 ? resolve() : reject(new Error(`${path.basename(cmd[0])} exited with ${code}`)); });
  });
}

// Build log: name the files actually patched (an agent grepping a 5,000-file list missed its own
// edit), count copied engine import data separately, and flag an accidental nested copy.
function logPatchedFiles(list, log) {
  const imported = list.filter((f) => /^(\.import|\.godot)\//.test(f));
  const own = list.filter((f) => !/^(\.import|\.godot)\//.test(f));
  log.write(`• Patching ${list.length} file(s): ${own.length} mod file(s)` + (imported.length ? `, ${imported.length} under .import/ or .godot/` : "") + "\n");
  for (const f of own.slice(0, 300)) log.write(`    res://${f}\n`);
  if (own.length > 300) log.write(`    … and ${own.length - 300} more\n`);
  if (imported.length > 200) log.write(`⚠ ${imported.length} import files are in src/files/: only put files you changed or added in src/files/ (the game pack already has its own imports).\n`);
}

// Byte-identical copies of game files patch nothing (an agent chasing harness noise once copied 23
// untouched particles/materials/textures into a mod): name them so they get deleted.
async function logUnchangedCopies(project, list, log) {
  const recovered = path.join(MODS, project.id, "decompiled/project"), files = path.join(MODS, project.id, "src/files");
  const same = [];
  for (const f of list) {
    if (/^(\.import|\.godot)\//.test(f) || f === "project.godot") continue;
    const a = await fsp.stat(path.join(files, f)).catch(() => null), b = await fsp.stat(path.join(recovered, f)).catch(() => null);
    if (!a?.isFile() || !b?.isFile() || a.size !== b.size || a.size > 16 * 1024 * 1024) continue;
    if ((await fsp.readFile(path.join(files, f))).equals(await fsp.readFile(path.join(recovered, f)))) same.push(f);
  }
  if (!same.length) return;
  log.write(`⚠ ${same.length} file(s) in src/files/ are unchanged copies of the game's own files (the game pack already has them; delete them from src/files/):\n`);
  for (const f of same.slice(0, 40)) log.write(`    ${f}\n`);
}

// What a wrong `file?path=` should have asked for.
async function fileHint(project, wanted) {
  const dir = path.join(MODS, project.id);
  for (const base of ["src/files", "src"]) {
    const guess = path.join(base, wanted);
    if (await fsp.stat(path.join(dir, guess)).then((s) => s.isFile(), () => false)) return `Did you mean path=${guess}? Paths are relative to the mod folder.`;
  }
  const logs = [];
  for (const name of ["decompile.log", "build.log", "loader.log"])
    if (await fsp.stat(path.join(dir, name)).then(() => true, () => false)) logs.push(name);
  const runs = (await fsp.readdir(path.join(dir, "runs")).catch(() => [])).sort().reverse().slice(0, 3);
  const files = [];
  for (const r of runs) files.push(`runs/${r}/{${(await fsp.readdir(path.join(dir, "runs", r)).catch(() => [])).filter((f) => f !== "run.json").join(",")}}`);
  return `The current build log is ${logs.includes("build.log") ? "build.log" : "(none yet)"}${logs.length ? ` (job logs: ${logs.join(", ")})` : ""}. ` +
    (runs.length ? `Newest test runs: ${files.join("; ")}. Take run ids from GET /REA/api/mods/${project.id}/runs; never guess one.` : "No test runs yet.");
}

// The one CPU-heavy job slot, shared with test runs (testrun.mjs).
export function claimJob(project, kind) {
  if (job) throw Object.assign(new Error(`Busy: ${job.kind} of "${job.name}" is running`), { status: 409 });
  job = { id: project.id, kind, name: project.name, started: now() };
  return job;
}
export function releaseJob() { job = null; }
export const modTools = { readProject, saveProject, installDir, run, findFiles, adapter: (p) => ADAPTERS[p.engine || "unity-mono"] };

// Whether REA can run this game headless for a test run ("" = yes), else why not.
export function headlessSupport(p) {
  const engine = p.engine || "unity-mono";
  if (engine === "godot") return "";
  if (engine === "rpgmaker") return "";
  if (engine === "unity-mono" || engine === "unity-il2cpp")
    return p.platform === "linux-x64" ? "" : "Windows Unity builds need Wine to run headless, and REA doesn't include it: the test run builds and checks the mod but can't start the game.";
  if (engine === "renpy") return "";
  if (engine === "unreal") return "Unreal games are Windows-only here and need Wine to run headless: the test run builds and syntax-checks the Lua but can't start the game.";
  if (engine === "electron") return "Electron/NW.js apps need a display to run: the test run builds and syntax-checks the JavaScript but can't start the app.";
  return "No headless runner for this engine: the test run only builds and checks the mod.";
}

async function godotPartial(dir) {
  const report = await fsp.readFile(path.join(dir, "decompiled/project/gdre_export.log"), "utf8").catch(() => "");
  return Number(report.match(/Failed conversions:\s*(\d+)/)?.[1] || 0) > 0
    || Number(report.match(/Scripts not decompiled:\s*(\d+)/)?.[1] || 0) > 0;
}

// Godot validation tells patched game files from new ones by the recovered project next to the mod.
// A mod made without the Recover step (a Godot 3 mod: every build failed with "No existing
// game file is patched") links the knowledge base's earlier recovery in first.
const QUIET = { write() {} };
async function ensureDecompiled(project, log) {
  const unit = extractUnit(project);
  if (!unit?.rel || !["gdre", "asar"].includes(unit.tool)) return;
  const dir = path.join(MODS, project.id);
  const target = path.join(dir, "decompiled", unit.out);
  if (exists(target) && (await fsp.readdir(target)).length) return;
  const started = now();
  const done = await lookupDecomp(project.gamePath, unit.rel, unit.tool).catch(() => null);
  if (!done) {
    if (project.engine !== "godot") return;
    throw new Error(`decompiled/${unit.out}/ is missing, so patched game files can't be told from new ones: run "${ADAPTERS.godot.extractLabel}" first (POST /REA/api/mods/${project.id}/decompile), then build again`);
  }
  await linkTree(done.dir, target);
  log.write(`${unit.rel}: this mod had no decompiled/${unit.out}/; linked the ${unit.tool} recovery from the knowledge base (${new Date(done.tool.updated).toISOString()})\n`);
  await recordDecomp(project.gamePath, { rel: unit.rel, tool: unit.tool, by: "mods", project: project.id, action: "reused" }).catch(() => {});
  const partial = project.engine === "godot" && await godotPartial(dir);
  const step = { state: partial ? "partial" : "done", started, finished: now(), error: "", files: await countFiles(path.join(dir, "decompiled")) };
  const updated = (await readProject(project.id)) || project;
  updated.steps.decompile = project.steps.decompile = step;
  await saveProject(updated);
}

function runJob(project, kind, steps) {
  if (job) throw Object.assign(new Error(`Busy: ${job.kind} of "${job.name}" is running`), { status: 409 });
  const dir = path.join(MODS, project.id);
  job = { id: project.id, kind, name: project.name, started: now() };
  project.steps[kind] = { state: "running", started: now() };
  saveProject(project).catch(() => {});
  (async () => {
    const log = fs.createWriteStream(path.join(dir, `${kind}.log`));
    let error = "";
    const unit = kind === "decompile" && project.gamePath ? extractUnit(project) : null;
    try {
      // A pack/asar this game version already had extracted: link the earlier output in.
      const done = unit && unit.rel && ["gdre", "asar"].includes(unit.tool) ? await lookupDecomp(project.gamePath, unit.rel, unit.tool).catch(() => null) : null;
      if (kind === "build") await ensureDecompiled(project, log);
      if (done) {
        await fresh(path.join(dir, "decompiled"));
        await linkTree(done.dir, path.join(dir, "decompiled", unit.out));
        log.write(`${unit.rel}: already extracted with ${unit.tool} (${new Date(done.tool.updated).toISOString()}), reused from the knowledge base\n`);
        await recordDecomp(project.gamePath, { rel: unit.rel, tool: unit.tool, by: "mods", project: project.id, action: "reused" }).catch(() => {});
      } else {
        for (const step of steps) {
          if (typeof step === "function") await step(log);
          else await run(step, log, dir);
        }
        if (unit?.rel) await saveDecomp(project, unit.rel, unit.tool, path.join(dir, "decompiled", unit.out), log);
      }
    } catch (e) { error = e.message; }
    const partial = kind === "decompile" && !error && project.engine === "godot" && await godotPartial(dir);
    log.write(error ? `\nFAILED: ${error}\n` : partial ? "\nPARTIAL: recovery reported missing scripts or failed resource conversions\n" : "\nDONE\n");
    log.end();
    const updated = (await readProject(project.id)) || project;
    updated.steps[kind] = { state: error ? "failed" : partial ? "partial" : "done", started: job.started, finished: now(), error,
      ...(kind === "decompile" && !error ? { files: await countFiles(path.join(dir, "decompiled")) } : {}) };
    await saveProject(updated);
    job = null;
  })();
}

const SOURCE_EXT = /\.(cs|lua|hpp|h|gd|js|rpy|py|ts|json|txt|tscn|tres|cfg|godot)$/i;
async function countFiles(dir) {
  return (await findFiles(dir, (r) => SOURCE_EXT.test(r), 30)).length;
}

// ------------------------------------------------------------------------- create/view --
export async function createMod({ folder, name, goal, recipe }, httpError) {
  const detected = await detectGame(folder.path);
  if (!detected.supported) throw httpError(400, detected.reason);
  const adapter = ADAPTERS[detected.engine];
  if (!adapter.recipes.includes(recipe)) throw httpError(400, `The ${recipe} recipe isn't available for ${detected.label}`);
  const id = randomBytes(6).toString("hex");
  const cleanName = String(name || "").trim().slice(0, 60) || "My mod";
  const project = { id, name: cleanName, className: csClass(cleanName), goal: String(goal || "").slice(0, 4000), recipe,
    folder: folder.id, gamePath: folder.path, gameName: folder.name, ...detected, created: now(), session: "", steps: {} };
  await fsp.mkdir(path.join(MODS, id), { recursive: true });
  await saveProject(project);
  await fsp.writeFile(path.join(MODS, id, "GOAL.md"), `# ${project.name}\n\nGame: ${project.gameName} (${detected.label}${detected.engineVersion ? ` ${detected.engineVersion}` : ""}${project.platform ? `, ${project.platform}` : ""})\n\n${project.goal}\n`);
  await fsp.writeFile(path.join(MODS, id, "ARCHITECTURE.md"), modArchTemplate(project));
  await adapter.template(project);
  // The game was already decompiled for another mod or on the REA page: link it in now.
  await ensureDecompiled(project, QUIET).catch(() => {});
  return project;
}

// The mod's own design map (its systems, files, which game hooks it uses). The game's
// ARCHITECTURE.md in the knowledge base stays about the game itself.
const MAX_ARCH = 200 * 1024;
function modArchTemplate(p) {
  return `# ${p.name}: mod architecture\n\n_Game: ${p.gameName} (${p.label || p.engine}). The Master Agent fills this in: the mod's systems, ` +
    `its files under src/, the game hooks it uses (with paths into the game's code) and how they connect. ` +
    `Facts about the game itself go in the game's Architecture tab._\n`;
}

async function listFiles(dir, base) {
  const out = [];
  for (const e of await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) { if (!["bin", "obj"].includes(e.name)) out.push(...await listFiles(full, base)); }
    else out.push(rel(base, full));
  }
  return out;
}

async function tail(file, bytes = 16000) {
  const text = await fsp.readFile(file, "utf8").catch(() => "");
  return text.length > bytes ? "…" + text.slice(-bytes) : text;
}

const GAME_LOGS = { "unity-mono": "BepInEx/LogOutput.log", "unity-il2cpp": "BepInEx/LogOutput.log" };
function gameLogPath(p) {
  if (GAME_LOGS[p.engine]) return path.join(p.gamePath, GAME_LOGS[p.engine]);
  if (p.engine === "unreal") return path.join(p.gamePath, p.binRel, "ue4ss/UE4SS.log");
  return null;
}

export function adapterInfo(engine) {
  const a = ADAPTERS[engine] || ADAPTERS.generic;
  return { recipes: a.recipes, needsLoader: Boolean(a.needsLoader), loaderHint: a.loaderHint || "", extractLabel: a.extractLabel,
    extractHint: a.extractHint, buildHint: a.buildHint };
}

// Unity mods decompiled before the Rules tab existed keep every Managed DLL in decompiled/ (Streets of
// Rogue: 3,370 of 6,390 files were BouncyCastle, Steamworks, Galaxy, Mirror...), so every grep and listing
// the agent ran was half library noise. Move the folders the Rules tab skips to decompiled-libs/ (still
// readable for a library's API, out of the way of searches). Hot files are never moved.
async function tidyDecompiled(project) {
  if (!String(project.engine || "unity-mono").startsWith("unity")) return;
  const root = path.join(MODS, project.id, "decompiled"), libs = path.join(MODS, project.id, "decompiled-libs");
  const names = (await fsp.readdir(root, { withFileTypes: true }).catch(() => [])).filter((e) => e.isDirectory() || e.isSymbolicLink()).map((e) => e.name);
  if (names.length < 2) return;
  const keep = new Set(pickAssemblies(project.engine || "unity-mono", names.map((n) => `${n}.dll`)).map((d) => d.replace(/\.dll$/, "")));
  const move = names.filter((n) => !keep.has(n));
  if (!move.length || move.length === names.length) return;
  await fsp.mkdir(libs, { recursive: true });
  for (const n of move) await fsp.rename(path.join(root, n), path.join(libs, n)).catch(() => {});
  await fsp.writeFile(path.join(libs, "README.md"), `Libraries the Rules tab says to leave alone, moved out of decompiled/ so searches stay on the game's own code.\n` +
    `Read one only for its API when a type in decompiled/ calls into it.\n`).catch(() => {});
}

// RELEVANT.md: the decompiled files the agent picked while planning as the ones this mod reads,
// hooks or patches. One "- `decompiled/…` — why" line per file; BUILD/REFINE/test-fix briefs start from them.
export async function relevantFiles(project) {
  const dir = path.join(MODS, project.id);
  const text = await fsp.readFile(path.join(dir, "RELEVANT.md"), "utf8").catch(() => "");
  const out = [], seen = new Set();
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(?:\*\*)?(?:`([^`]+)`|(\S+))(?:\*\*)?\s*(?:[—–:-]+\s*)?(.*)$/);
    if (!m) continue;
    let file = (m[1] || m[2]).replace(/^\.?\//, "").replace(/^.*?rea-mods\/[^/]+\//, "").replace(/[,;:]+$/, "").replace(/:\d+$/, "");
    if (!file.startsWith("decompiled/")) file = `decompiled/${file}`;
    const full = path.resolve(dir, file);
    if (!full.startsWith(path.join(dir, "decompiled") + path.sep) || seen.has(file)) continue;
    seen.add(file);
    out.push({ path: file, why: m[3].trim().slice(0, 300), exists: exists(full) });
    if (out.length >= 40) break;
  }
  return out;
}

// One brief line for later steps: read these first.
export function relevantBrief(project, files) {
  if (!files?.length) return "";
  return `Most relevant game files for this mod (RELEVANT.md in /opt/data/rocsy-workspace/rea-mods/${project.id}/, picked while planning): ` +
    files.map((f) => `${f.path}${f.why ? ` (${f.why})` : ""}${f.exists ? "" : " [MISSING: fix the path]"}`).join("; ") +
    `. Read these first; if you need other game files, add them to RELEVANT.md.`;
}

async function view(project) {
  const dir = path.join(MODS, project.id);
  const adapter = ADAPTERS[project.engine || "unity-mono"];
  const read = (name) => fsp.readFile(path.join(dir, name), "utf8").catch(() => "");
  const gameLog = gameLogPath(project);
  return { ...project, engine: project.engine || "unity-mono", adapter: adapterInfo(project.engine || "unity-mono"), projectPath: dir,
    readiness: await modReadiness(project), engineBrief: await engineBrief(project),
      engineVersion: (await engineVersion(project.gamePath).catch(() => null))?.short || project.engineVersion || "",
    running: job?.id === project.id ? job.kind : "", launch: adapter.launch(project), headless: headlessSupport(project),
    ready: adapter.ready ? adapter.ready(project) : true,
    plan: await read("PLAN.md"), notes: await read("NOTES.md"), architecture: await read("ARCHITECTURE.md"),
    ...await relevantFiles(project).then((relevant) => ({ relevant, relevantBrief: relevantBrief(project, relevant) })),
    goals: await readGoals(dir),
    architectureUpdated: (await fsp.stat(path.join(dir, "ARCHITECTURE.md")).catch(() => null))?.mtimeMs || 0,
    src: (await listFiles(path.join(dir, "src"), dir)).filter((f) => !f.endsWith(".csproj")),
    built: Boolean(project.steps?.build?.state === "done" && exists(installDir(project))),
    loaderBuilt: project.steps?.loader?.state === "done",
    decompileLog: await tail(path.join(dir, "decompile.log"), 4000), loaderLog: await tail(path.join(dir, "loader.log"), 2000),
    buildLog: await tail(path.join(dir, "build.log")),
    gameLog: gameLog && exists(gameLog) ? await tail(gameLog, 12000) : null };
}

// Routes under /REA/api/mods. Returns true when it handled the request.
export async function handleMods({ method, api, url, req, res, send, readJson, httpError, targets }) {
  if (await handleRewrites({ method, api, url, req, res, send, readJson, httpError, targets })) return true;
  if (api === "/mods" && method === "GET") {
    const folder = url.searchParams.get("folder");
    const ids = await fsp.readdir(MODS).catch(() => []);
    const projects = (await Promise.all(ids.map(readProject))).filter((p) => p && (!folder || p.folder === folder));
    send(res, 200, { mods: await Promise.all(projects.sort((a, b) => b.created - a.created)
      .map(async (p) => ({ ...p, readiness: await modReadiness(p) }))), job });
    return true;
  }
  if (api === "/mods/detect" && method === "GET") {
    const folder = targets[url.searchParams.get("folder")];
    if (!folder || folder.type !== "folder") throw httpError(404, "Unknown folder");
    const detected = await detectGame(folder.path);
    send(res, 200, { ...detected, adapter: adapterInfo(detected.engine) });
    return true;
  }
  if (api === "/mods" && method === "POST") {
    const body = await readJson(req);
    const folder = targets[String(body.folder)];
    if (!folder || folder.type !== "folder") throw httpError(404, "Pick a game folder first");
    send(res, 201, await view(await createMod({ folder, name: body.name, goal: body.goal, recipe: body.recipe }, httpError)));
    return true;
  }
  const match = api.match(/^\/mods\/([0-9a-f]{12})(?:\/(decompile|build|loader|search|file|tree|raw|session|verify|architecture))?$/);
  if (!match) return false;
  const project = await readProject(match[1]);
  if (!project) throw httpError(404, "Unknown mod");
  project.engine ||= "unity-mono";
  const adapter = ADAPTERS[project.engine];
  const action = match[2];
  if (await projectFile({ root: path.join(MODS, project.id), action, method, url, req, res, send, httpError })) return true;
  if (method === "GET" && !action) {
    // Mods made before builds linked the recovery in (or without the Recover step) pick it up on open.
    if (!job) await ensureDecompiled(project, QUIET).catch(() => {});
    if (!job) await tidyDecompiled(project).catch(() => {});
    send(res, 200, await view(project)); return true;
  }
  if (method === "POST" && ["decompile", "build", "loader"].includes(action)) {
    if (!exists(project.gamePath)) throw httpError(410, "The game folder is not mounted any more");
    if (action === "loader" && !adapter.loader) throw httpError(400, "This engine doesn't need a separate loader step");
    if (action !== "loader" && adapter.ready && !adapter.ready(project)) throw httpError(409, adapter.notReady);
    runJob(project, action, action === "decompile" ? adapter.extract(project) : action === "build" ? adapter.build(project) : adapter.loader(project));
    send(res, 202, await view(project));
    return true;
  }
  if (method === "GET" && action === "search") {
    const q = String(url.searchParams.get("q") || "").trim();
    if (q.length < 2) throw httpError(400, "Search for at least 2 characters");
    send(res, 200, await search(project, q)); return true;
  }
  if (method === "GET" && action === "file") {
    const dir = path.join(MODS, project.id);
    const file = await containedFile(dir, url.searchParams.get("path"));
    if (!file) throw httpError(404, "Not found");
    const stat = await fsp.stat(file).catch(() => null);
    if (!stat?.isFile()) throw httpError(404, `No such file: ${rel(dir, file)}. ${await fileHint(project, rel(dir, file))}`);
    const max = 2 * 1024 * 1024;
    if (stat.size <= max) { send(res, 200, { path: rel(dir, file), text: await fsp.readFile(file, "utf8") }); return true; }
    const handle = await fsp.open(file, "r");
    try {
      const tail = Buffer.alloc(max);
      const { bytesRead } = await handle.read(tail, 0, max, stat.size - max);
      const text = tail.subarray(0, bytesRead).toString("utf8").replace(/^[^\n]*\n/, "");
      send(res, 200, { path: rel(dir, file), text, truncated: true, size: stat.size,
        note: `The file is ${(stat.size / 1048576).toFixed(1)} MB: this is its last 2 MB.` });
    } finally { await handle.close(); }
    return true;
  }
  if (action === "architecture" && method === "GET") {
    send(res, 200, { text: await fsp.readFile(path.join(MODS, project.id, "ARCHITECTURE.md"), "utf8").catch(() => "") });
    return true;
  }
  if (action === "architecture" && method === "PUT") {
    const body = await readJson(req, MAX_ARCH + 4096);
    const text = String(body.text || "");
    if (!text.trim()) throw httpError(400, "The architecture text is empty, nothing was saved");
    if (text.length > MAX_ARCH) throw httpError(413, "Too long (max 200 KB)");
    await fsp.writeFile(path.join(MODS, project.id, "ARCHITECTURE.md"), text.endsWith("\n") ? text : text + "\n");
    send(res, 200, { ok: true, chars: text.length });
    return true;
  }
  if (method === "POST" && action === "session") {
    const body = await readJson(req);
    if (body.session !== undefined) project.session = String(body.session || "").slice(0, 80);
    if (["planning", "approved", "refining"].includes(body.stage)) project.stage = body.stage;
    await saveProject(project);
    send(res, 200, await view(project)); return true;
  }
  if (method === "POST" && action === "verify") {
    const body = await readJson(req);
    if (!["passed", "failed"].includes(body.state)) throw httpError(400, "Choose passed or failed");
    const evidence = String(body.evidence || "").trim().slice(0, 4000);
    if (evidence.length < 12) throw httpError(400, "Describe what happened in the game (at least 12 characters)");
    const marker = await fsp.stat(path.join(MODS, project.id, ".installed")).catch(() => null);
    if (body.state === "passed" && (project.steps?.build?.state !== "done" || !marker
      || marker.mtimeMs < project.steps.build.finished))
      throw httpError(409, "Build and install the current mod before recording a passing play test");
    project.verification = { state: body.state, evidence, tested: now(), buildFinished: project.steps?.build?.finished || 0 };
    await saveProject(project);
    send(res, 200, await view(project)); return true;
  }
  if (method === "DELETE" && !action) {
    if (job?.id === project.id) throw httpError(409, "A job is running for this mod");
    if (exists(path.join(MODS, project.id, ".installed"))) throw httpError(409, "Uninstall the mod from the game first");
    await fsp.rm(path.join(MODS, project.id), { recursive: true, force: true });
    send(res, 200, { ok: true }); return true;
  }
  return false;
}

async function search(project, query) {
  const root = path.join(MODS, project.id, "decompiled");
  const needle = query.toLowerCase();
  const hits = [];
  const stack = [root];
  let files = 0;
  while (stack.length && hits.length < 300 && files < 20000) {
    const d = stack.pop();
    for (const e of await fsp.readdir(d, { withFileTypes: true }).catch(() => [])) {
      const full = path.join(d, e.name);
      if (e.isDirectory()) { stack.push(full); continue; }
      if (!SOURCE_EXT.test(e.name)) continue;
      const stat = await fsp.stat(full).catch(() => null);
      if (!stat || stat.size > 4 * 1024 * 1024) continue;
      files++;
      const text = await fsp.readFile(full, "utf8").catch(() => "");
      if (!text.toLowerCase().includes(needle)) continue;
      const lines = text.split("\n");
      for (let i = 0; i < lines.length && hits.length < 300; i++)
        if (lines[i].toLowerCase().includes(needle))
          hits.push({ path: rel(path.join(MODS, project.id), full), line: i + 1, text: lines[i].trim().slice(0, 240) });
    }
  }
  return { hits, truncated: hits.length >= 300 };
}
