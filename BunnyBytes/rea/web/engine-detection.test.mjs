import assert from "node:assert/strict";
import test from "node:test";
import { detectEngineHints } from "./engine-detection.mjs";

const detect = (...paths) => detectEngineHints(paths.map((rel) => ({ rel })));

test("Valve generations have separate signatures", () => {
  assert.equal(detect("game/csgo/gameinfo.gi", "game/csgo/pak01_dir.vpk")[0].name, "Source 2");
  assert.equal(detect("hl2/gameinfo.txt", "hl2/hl2_textures_dir.vpk")[0].name, "Source");
  assert.equal(detect("valve/liblist.gam")[0].name, "GoldSrc");
  assert.equal(detect("pak01_dir.vpk")[0].name, "Source family (version unknown)");
});

test("AAA archive families use distinctive filenames or paired markers", () => {
  for (const [name, paths] of [
    ["CryEngine", ["Bin64/CrySystem.dll"]],
    ["Frostbite", ["Data/Win32/Levels/level.toc", "Data/Win32/Levels/level.sb"]],
    ["RE Engine", ["re_chunk_000.pak.patch_001.pak"]],
    ["Creation Engine", ["Data/Fallout4 - Main.ba2"]],
    ["REDengine", ["archive/pc/content/basegame_1.archive"]],
    ["REDengine", ["content/content0/bundles/blob.bundle"]],
    ["Rockstar RAGE", ["x64a.rpf"]],
    ["id Tech 4", ["base/pak000.pk4"]],
    ["id Tech (modern)", ["base/gameresources.resources"]],
    ["id Tech 3", ["baseq3/pak0.pk3"]],
  ]) assert.equal(detect(...paths)[0].name, name);
  assert.equal(detect("Data/Win32/level.toc").length, 0);
  assert.equal(detect("pak0.pak").length, 0);
});

test("the main AGK game outranks a bundled Unity uploader", () => {
  const hints = detect("SteamWorkshopUploader/UnityPlayer.dll", "media/bytecode.byc");
  assert.equal(hints[0].name, "AppGameKit (AGK)");
});

test("unnamed engines still need code and assets", () => {
  const files = [
    { rel: "game.exe", kind: "executable", format: "PE EXE" },
    { rel: "textures/player.png" },
  ];
  assert.equal(detectEngineHints(files)[0].name, "Unknown/custom native engine");
  assert.equal(detect("textures/player.png").length, 0);
});
