// Downloads REA's file-based prerequisites from versions.json at image build time.
// Every archive is checked against its pinned SHA-256 before it is unpacked.
import { createHash } from "node:crypto";
import { createWriteStream, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync,
  symlinkSync, writeFileSync, chmodSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { join } from "node:path";

const [versionsPath, root] = process.argv.slice(2);
const pins = JSON.parse(readFileSync(versionsPath, "utf8"));
const tmp = join(root, ".download");
const bin = join(root, "bin");
mkdirSync(tmp, { recursive: true });
mkdirSync(bin, { recursive: true });

// Pins on a rolling GitHub release tag: upstream replaces the asset, so an older pin 404s.
// Then take the tag's current asset, checked against the SHA-256 GitHub publishes for it.
const ROLLING = {
  ue4ss: { repo: "UE4SS-RE/RE-UE4SS", tag: "experimental-latest", asset: /^UE4SS_v([0-9.]+-[0-9]+-g[0-9a-f]+)\.zip$/ },
};

async function repin(name) {
  const { repo, tag, asset } = ROLLING[name];
  const response = await fetch(`https://api.github.com/repos/${repo}/releases/tags/${tag}`,
    { headers: { accept: "application/vnd.github+json" } });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status} looking up ${repo}@${tag}`);
  const hit = (await response.json()).assets.find((a) => asset.test(a.name) && /^sha256:[0-9a-f]{64}$/.test(a.digest || ""));
  if (!hit) throw new Error(`${name}: no verifiable asset in ${repo}@${tag}`);
  const pin = { version: asset.exec(hit.name)[1], url: hit.browser_download_url, sha256: hit.digest.slice(7) };
  console.log(`${name}: pinned ${pins[name].version} is gone from ${tag}, using ${pin.version}`);
  pins[name] = pin;
}

async function download(name) {
  if (ROLLING[name]) {
    const probe = await fetch(pins[name].url, { method: "HEAD", redirect: "follow" });
    if (probe.status === 404) await repin(name);
  }
  const { url, sha256, sha512 } = pins[name];
  const algorithm = sha512 ? "sha512" : "sha256";
  const expected = sha512 || sha256;
  if (!/^https:\/\/(github\.com|builds\.dotnet\.microsoft\.com|builds\.bepinex\.dev)\//.test(url) || !/^[0-9a-f]{64}([0-9a-f]{64})?$/.test(expected))
    throw new Error(`${name}: bad pin`);
  const file = join(tmp, url.split("/").pop());
  const hash = createHash(algorithm);
  const response = await fetch(url, { redirect: "follow" });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status} for ${url}`);
  const body = Readable.fromWeb(response.body);
  body.on("data", (chunk) => hash.update(chunk));
  await pipeline(body, createWriteStream(file));
  const actual = hash.digest("hex");
  if (actual !== expected) throw new Error(`${name}: ${algorithm.toUpperCase()} mismatch (${actual}, expected ${expected})`);
  console.log(`${name} ${pins[name].version}: ${algorithm === "sha512" ? "SHA-512" : "SHA-256"} verified`);
  return file;
}

// Unpack into a scratch folder and move its single top-level directory to dest.
function unpackSingle(archive, dest, command) {
  const scratch = `${dest}.unpack`;
  rmSync(scratch, { recursive: true, force: true });
  mkdirSync(scratch);
  execFileSync(command[0], [...command.slice(1), archive, ...(command[0] === "unzip" ? ["-d", scratch] : ["-C", scratch])],
    { stdio: "inherit" });
  const entries = readdirSync(scratch);
  rmSync(dest, { recursive: true, force: true });
  if (entries.length === 1) { renameSync(join(scratch, entries[0]), dest); rmSync(scratch, { recursive: true }); }
  else renameSync(scratch, dest);
}

function findFile(dir, name) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) { const hit = findFile(path, name); if (hit) return hit; }
    else if (entry.name === name) return path;
  }
  return null;
}

function wrapper(name, script) {
  writeFileSync(join(bin, name), `#!/bin/sh\n${script}\n`);
  chmodSync(join(bin, name), 0o755);
}

unpackSingle(await download("ghidra"), join(root, "ghidra"), ["unzip", "-q"]);

mkdirSync(join(root, "jadx"), { recursive: true });
renameSync(await download("jadx-headless-mcp"), join(root, "jadx/jadx-headless-mcp.jar"));

unpackSingle(await download("wabt"), join(root, "wabt"), ["tar", "-xzf"]);
if (!existsSync(join(root, "wabt/bin/wasm-validate"))) throw new Error("wabt: bin/wasm-validate missing");

mkdirSync(join(root, "apktool"), { recursive: true });
renameSync(await download("apktool"), join(root, "apktool/apktool.jar"));
wrapper("apktool", `exec java -jar ${join(root, "apktool/apktool.jar")} "$@"`);

unpackSingle(await download("rizin"), join(root, "rizin"), ["tar", "-xJf"]);
const rizin = findFile(join(root, "rizin"), "rizin");
if (!rizin) throw new Error("rizin: executable missing from the archive");
symlinkSync(rizin, join(bin, "rizin"));

// Modding toolchain: .NET SDK (builds BepInEx plugins, runs ILSpy) and the BepInEx 5 loaders.
unpackSingle(await download("dotnet-sdk"), join(root, "dotnet"), ["tar", "-xzf"]);
if (!existsSync(join(root, "dotnet/dotnet"))) throw new Error("dotnet-sdk: dotnet missing from the archive");
// BepInEx zips are laid out as they go into a game folder: keep that layout exactly.
for (const flavor of ["win-x64", "win-x86", "linux-x64"]) {
  const dest = join(root, "bepinex", flavor);
  mkdirSync(dest, { recursive: true });
  execFileSync("unzip", ["-q", await download(`bepinex-${flavor}`), "-d", dest], { stdio: "inherit" });
  if (!existsSync(join(dest, "BepInEx/core/BepInEx.dll"))) throw new Error(`bepinex-${flavor}: BepInEx/core/BepInEx.dll missing`);
}

// Mod loaders and extractors for the other engines (kept in the layout they install with).
function unzipTo(archive, dest) {
  mkdirSync(dest, { recursive: true });
  execFileSync("unzip", ["-q", archive, "-d", dest], { stdio: "inherit" });
}
for (const flavor of ["win-x64", "linux-x64"]) {
  const dest = join(root, "bepinex6", flavor);
  unzipTo(await download(`bepinex6-il2cpp-${flavor}`), dest);
  if (!existsSync(join(dest, "BepInEx/core/BepInEx.Unity.IL2CPP.dll"))) throw new Error(`bepinex6 ${flavor}: BepInEx.Unity.IL2CPP.dll missing`);
}
unzipTo(await download("ue4ss"), join(root, "ue4ss"));
if (!findFile(join(root, "ue4ss"), "UE4SS.dll")) throw new Error("ue4ss: UE4SS.dll missing");
unzipTo(await download("gdre"), join(root, "gdre"));
if (!existsSync(join(root, "gdre/gdre_tools.x86_64"))) throw new Error("gdre: gdre_tools.x86_64 missing");
chmodSync(join(root, "gdre/gdre_tools.x86_64"), 0o755);
wrapper("gdre_tools", `exec ${join(root, "gdre/gdre_tools.x86_64")} --headless "$@"`);
unpackSingle(await download("unrpyc"), join(root, "unrpyc"), ["tar", "-xzf"]);
if (!existsSync(join(root, "unrpyc/unrpyc.py"))) throw new Error("unrpyc: unrpyc.py missing");
wrapper("unrpyc", `exec python3 ${join(root, "unrpyc/unrpyc.py")} "$@"`);

rmSync(tmp, { recursive: true, force: true });
writeFileSync(join(root, "pins.json"), JSON.stringify(pins, null, 2));
