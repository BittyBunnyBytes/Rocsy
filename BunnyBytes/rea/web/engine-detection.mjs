// Shared, filename-based engine hints for the Files and Mods views. A signature
// must identify a runtime or its distinctive game data, not merely a common SDK.
const SIGNATURES = [
  ["AppGameKit (AGK)", /(^|\/)bytecode\.byc$/i],
  // Valve's three engine generations have different game manifest filenames.
  ["Source 2", /(^|\/)(gameinfo\.gi|(?:lib)?engine2\.(?:dll|so|dylib))$/i],
  ["GoldSrc", /(^|\/)liblist\.gam$/i],
  ["Source", /(^|\/)gameinfo\.txt$/i],
  ["Unity", /(^|\/)(UnityPlayer\.(dll|so|dylib)|[^/]+_Data\/(globalgamemanagers|Managed\/UnityEngine\.dll))$/i],
  ["Unity (Mono scripts)", /_Data\/Managed\/Assembly-CSharp\.dll$/i],
  ["Unity (IL2CPP)", /(GameAssembly\.(dll|so)|il2cpp_data\/Metadata\/global-metadata\.dat)$/i],
  ["Unreal Engine", /(Content\/Paks\/[^/]+\.(pak|utoc)|Engine\/Binaries\/)/i],
  ["Godot", /\.pck$/i],
  ["CryEngine", /(^|\/)(CryEngine\.(dll|so|dylib)|CrySystem\.(dll|so|dylib)|CryGameSDK\.dll|game\.cryproject)$/i],
  ["RE Engine", /(^|\/)re_chunk_\d+\.pak(?:\..*)?$/i],
  ["Creation Engine", /(^|\/)Data\/[^/]+\.ba2$/i],
  ["REDengine", /(^|\/)(archive\/pc\/content\/[^/]+\.archive|content\/content\d+\/bundles\/[^/]+\.bundle)$/i],
  ["Rockstar RAGE", /(^|\/)(?:x64[a-z]\.rpf|packs_\d+\.rpf|update\/update\.rpf)$/i],
  ["id Tech 4", /(^|\/)(?:base\/)?pak\d+\.pk4$/i],
  ["id Tech (modern)", /(^|\/)base\/[^/]+\.(?:resources|pk5)$/i],
  ["id Tech 3", /(^|\/)baseq3\/pak\d+\.pk3$/i],
  ["id Tech 2", /(^|\/)baseq2\/pak\d+\.pak$/i],
  ["id Tech 1", /(^|\/)id1\/pak\d+\.pak$/i],
  ["Defold", /(^|\/)game\.arc[di]$/i],
  ["Cocos2d-x", /(^|\/)(libcocos2d[^/]*\.(so|dylib)|cocos2d[^/]*\.dll)$/i],
  ["MonoGame", /(^|\/)MonoGame\.Framework[^/]*\.dll$/i],
  ["FNA", /(^|\/)FNA\.dll$/i],
  ["libGDX", /(^|\/)gdx(?:-[^/]*)?\.jar$/i],
  ["jMonkeyEngine", /(^|\/)jme3-core(?:-[^/]*)?\.jar$/i],
  ["Adventure Game Studio", /(^|\/)(acwin\.exe|agsgame\.dat)$/i],
  ["Construct", /(^|\/)(c2runtime|c3runtime)\.js$/i],
  ["Electron", /(^|\/)resources\/app\.asar$|(^|\/)(electron|libnode)\.(so|dll)$/i],
  ["Ren'Py", /(^|\/)renpy\//i],
  ["RPG Maker", /(rpg_core|rmmz_core)\.js$/i],
  ["GameMaker", /(^|\/)(data\.win|game\.unx|game\.ios)$/i],
  ["LÖVE", /\.love$/i],
];

const FROSTBITE_INDEX = /(^|\/)Data\/Win32\/.+\.(?:toc|sbtoc)$/i;
const FROSTBITE_DATA = /(^|\/)Data\/Win32\/.+\.(?:sb|cas)$/i;
const VPK = /\.vpk$/i;

export function isEngineMarker(rel) {
  return SIGNATURES.some(([, test]) => test.test(rel)) ||
    FROSTBITE_INDEX.test(rel) || FROSTBITE_DATA.test(rel) || VPK.test(rel);
}

const asset = (p) => /\.(png|jpe?g|webp|dds|ogg|wav|mp3|flac|glb|gltf|obj|fbx|ttf|otf|pack|dat)$/i.test(p);
const native = (f) => f.kind === "executable" && /^(ELF|PE EXE|Mach-O)$/.test(f.format || "");

export function detectEngineHints(files) {
  const depth = (rel) => rel.split("/").length;
  const hints = SIGNATURES.map(([name, test]) => ({ name,
    evidence: files.filter((f) => test.test(f.rel)).map((f) => f.rel)
      .sort((a, b) => depth(a) - depth(b) || a.localeCompare(b)).slice(0, 3) }))
    .filter((hint) => hint.evidence.length);
  const frostbiteIndex = files.filter((f) => FROSTBITE_INDEX.test(f.rel));
  const frostbiteData = files.filter((f) => FROSTBITE_DATA.test(f.rel));
  if (frostbiteIndex.some((f) => /\.sbtoc$/i.test(f.rel)) || (frostbiteIndex.length && frostbiteData.length))
    hints.push({ name: "Frostbite", evidence: [frostbiteIndex[0].rel, frostbiteData[0]?.rel].filter(Boolean) });
  if (!hints.some((hint) => /^(Source 2|Source|GoldSrc)$/.test(hint.name))) {
    const vpk = files.find((f) => VPK.test(f.rel));
    if (vpk) hints.push({ name: "Source family (version unknown)", evidence: [vpk.rel] });
  }
  if (hints.length) return hints.sort((a, b) => depth(a.evidence[0]) - depth(b.evidence[0]));

  // A native program or JAR alone is not evidence of a game engine. Require
  // adjacent game assets, and avoid pretending to know the source language.
  const assets = files.filter((f) => asset(f.rel));
  if (!assets.length) return [];
  const jar = files.find((f) => /\.jar$/i.test(f.rel));
  if (jar) return [{ name: "Unknown/custom Java engine", evidence: [jar.rel, assets[0].rel] }];
  const exe = files.find(native);
  if (exe) return [{ name: "Unknown/custom native engine", evidence: [exe.rel, assets[0].rel] }];
  return [];
}
