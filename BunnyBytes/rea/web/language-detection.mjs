// Report languages supported by game-code evidence. A native executable, JAR,
// .NET DLL, or Ghidra's generated C pseudocode does not reveal the source language.
const EXTENSIONS = new Map(Object.entries({
  agc: "AGK BASIC", byc: "AGK BASIC", c: "C", cc: "C++", cpp: "C++", cxx: "C++",
  cs: "C#", fs: "F#", gd: "GDScript", go: "Go", hx: "Haxe", java: "Java",
  js: "JavaScript", cjs: "JavaScript", mjs: "JavaScript", kt: "Kotlin", kts: "Kotlin",
  lua: "Lua", py: "Python", rpy: "Ren'Py Script", rpyc: "Ren'Py Script",
  rs: "Rust", as: "ActionScript", swift: "Swift", ts: "TypeScript", tsx: "TypeScript",
  uc: "UnrealScript", vb: "Visual Basic",
}));
const BUNDLED = /(^|\/)(node_modules|vendor|third_party|third-party|redist|__redist|jre|jdk)(\/|$)/i;
const PSEUDOCODE_TOOLS = new Set(["ghidra", "rizin", "ida", "binary-ninja", "ue4ss-dump", "peres"]);

function languageOf(rel) {
  if (BUNDLED.test(rel) || /\.d\.ts$/i.test(rel)) return null;
  const ext = rel.includes(".") ? rel.slice(rel.lastIndexOf(".") + 1).toLowerCase() : "";
  return EXTENSIONS.get(ext) || null;
}

export function detectGameLanguages(files, recovered = [], engines = []) {
  const found = new Map();
  const priority = { "Unity script assembly": 1, bytecode: 2, "source file": 3, recovered: 4 };
  const primaryTop = engines[0]?.evidence?.[0]?.split("/")[0];
  const rootExecutable = files.some((f) => !f.rel.includes("/") && f.kind === "executable");
  const secondaryRoots = new Set();
  if (rootExecutable) for (const f of files) {
    if (f.kind === "executable" && f.rel.includes("/")) {
      const top = f.rel.split("/")[0];
      if (top !== primaryTop) secondaryRoots.add(top);
    }
  }
  for (const engine of engines.slice(1)) for (const evidence of engine.evidence || []) {
    if (!evidence.includes("/")) continue;
    const top = evidence.split("/")[0];
    if (top !== primaryTop) secondaryRoots.add(top);
  }
  const inSecondaryTool = (rel) => secondaryRoots.has(rel.split("/")[0]);
  const add = (name, rel, source) => {
    let item = found.get(name);
    if (!item) found.set(name, item = { name, source, evidence: [] });
    if (priority[source] > priority[item.source]) {
      item.source = source;
      item.evidence = [];
    }
    if (item.source === source && item.evidence.length < 3 && !item.evidence.includes(rel)) item.evidence.push(rel);
  };

  for (const file of files) {
    const rel = file.rel;
    if (inSecondaryTool(rel)) continue;
    const name = languageOf(rel);
    if (name) add(name, rel, /\.byc$|\.rpyc$/i.test(rel) ? "bytecode" : "source file");
    // Unity's own script assembly is a useful early hint before ILSpy runs.
    if (/(^|\/)[^/]+_Data\/Managed\/Assembly-CSharp(?:-firstpass)?\.dll$/i.test(rel))
      add("C#", rel, "Unity script assembly");
  }
  for (const file of recovered) {
    if (PSEUDOCODE_TOOLS.has(file.tool) || inSecondaryTool(file.rel)) continue;
    const name = languageOf(file.rel);
    if ((name === "C" || name === "C++") && !["copy", "asar", "gdre"].includes(file.tool)) continue;
    if (name) add(name, file.rel, "recovered");
  }
  const languages = [...found.values()].sort((a, b) => a.name.localeCompare(b.name));
  return languages.length ? languages : [{ name: "Lang Unkown", source: "unknown", evidence: [] }];
}
