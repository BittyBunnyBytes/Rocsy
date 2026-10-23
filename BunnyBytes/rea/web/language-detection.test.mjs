import assert from "node:assert/strict";
import test from "node:test";
import { detectGameLanguages } from "./language-detection.mjs";

const files = (...rels) => rels.map((rel) => ({ rel }));

test("initial evidence identifies source and known bytecode", () => {
  assert.deepEqual(detectGameLanguages(files("media/bytecode.byc")).map((x) => x.name), ["AGK BASIC"]);
  assert.deepEqual(detectGameLanguages(files("Game_Data/Managed/Assembly-CSharp.dll")).map((x) => x.name), ["C#"]);
  assert.deepEqual(detectGameLanguages(files("src/game.cpp", "src/game.rs", "game/scripts/main.lua")).map((x) => x.name), ["C++", "Lua", "Rust"]);
});

test("compiled native and JVM files remain unknown without language evidence", () => {
  assert.deepEqual(detectGameLanguages(files("game.exe", "game.jar", "GameAssembly.dll", "assets.pack")).map((x) => x.name), ["Lang Unkown"]);
  assert.deepEqual(detectGameLanguages(files("node_modules/library.js", "vendor/tool.py")).map((x) => x.name), ["Lang Unkown"]);
});

test("decompile output updates language evidence but pseudocode does not", () => {
  const languages = detectGameLanguages(files("Game_Data/Managed/Assembly-CSharp.dll"), [
    { rel: "Game_Data/Managed/Assembly-CSharp.dll/Game.cs", tool: "ilspy" },
    { rel: "game.exe/FUN_1234.c", tool: "ghidra" },
    { rel: "game.exe/guess.cpp", tool: "agent" },
  ]);
  assert.deepEqual(languages.map((x) => x.name), ["C#"]);
  assert.equal(languages[0].source, "recovered");
  assert.deepEqual(detectGameLanguages([], [{ rel: "project/scripts/player.gd", tool: "gdre" }]).map((x) => x.name), ["GDScript"]);
});

test("bundled uploaders do not add their language to the main game", () => {
  const game = [
    { rel: "sh2.exe", kind: "executable" },
    { rel: "media/bytecode.byc" },
    { rel: "SteamWorkshopUploader/SteamWorkshopUploader.exe", kind: "executable" },
    { rel: "SteamWorkshopUploader/SteamWorkshopUploader_Data/Managed/Assembly-CSharp.dll" },
  ];
  const engines = [
    { name: "AppGameKit (AGK)", evidence: ["media/bytecode.byc"] },
    { name: "Unity", evidence: ["SteamWorkshopUploader/UnityPlayer.dll"] },
  ];
  assert.deepEqual(detectGameLanguages(game, [], engines).map((x) => x.name), ["AGK BASIC"]);
});
