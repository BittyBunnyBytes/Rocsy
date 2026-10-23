// Syntax diagnostics for source recovered by REA. No downloaded parser or execution of game code.
import fsp from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { spawn } from "node:child_process";

const LANG = { ".gd": "GDScript", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
  ".lua": "Lua", ".py": "Python", ".rpy": "Ren'Py", ".cs": "C#", ".hpp": "C++", ".h": "C/C++",
  ".tscn": "Godot Resource", ".tres": "Godot Resource" };
export const languageOf = (name) => LANG[path.extname(name).toLowerCase()] || "";

function diagnostic(line, column, message) { return { line, column, message }; }

// GDScript 3's indentation and delimiter rules. This intentionally reports only structural
// errors that can be established without loading the project's Godot version and classes.
export function parseGdscript(source) {
  const errors = [], stack = [], indents = [0];
  let block = "", pending = false, continuation = false;
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  for (let n = 0; n < lines.length; n++) {
    const raw = lines[n], margin = raw.match(/^[ \t]*/)[0];
    const width = [...margin].reduce((v, c) => c === "\t" ? v + (4 - v % 4) : v + 1, 0);
    let code = "", quote = block, triple = Boolean(block), escape = false;
    for (let i = margin.length; i < raw.length; i++) {
      const c = raw[i];
      if (quote) {
        if (triple && raw.slice(i, i + 3) === quote.repeat(3)) { i += 2; quote = ""; triple = false; }
        else if (!triple && escape) escape = false;
        else if (!triple && c === "\\") escape = true;
        else if (!triple && c === quote) quote = "";
        code += " "; continue;
      }
      if (c === "#") break;
      if (c === "'" || c === '"') {
        quote = c; triple = raw.slice(i, i + 3) === c.repeat(3);
        if (triple) i += 2;
        code += " "; continue;
      }
      if ("([{".includes(c)) stack.push({ c, line: n + 1, column: i + 1 });
      else if (")]}".includes(c)) {
        const last = stack.pop();
        if (!last || { ")": "(", "]": "[", "}": "{" }[c] !== last.c) errors.push(diagnostic(n + 1, i + 1, `Unexpected '${c}'`));
      }
      code += c;
    }
    const trimmed = code.trim();
    const inBlock = Boolean(block);
    block = quote && triple ? quote : "";
    if (quote && !triple) errors.push(diagnostic(n + 1, raw.length, "Unterminated string"));
    if (!trimmed) continue;
    if (!continuation && !inBlock) {
      if (pending && width <= indents.at(-1)) errors.push(diagnostic(n + 1, margin.length + 1, "Expected an indented block"));
      if (width > indents.at(-1)) {
        if (!pending) errors.push(diagnostic(n + 1, margin.length + 1, "Unexpected indentation"));
        indents.push(width);
      } else if (width < indents.at(-1)) {
        while (indents.length > 1 && width < indents.at(-1)) indents.pop();
        if (width !== indents.at(-1)) errors.push(diagnostic(n + 1, margin.length + 1, "Indentation does not match an outer block"));
      }
      if (/^(?:func|if|elif|else|for|while|match|class)\b/.test(trimmed) && !/:/.test(trimmed) && stack.length === 0 && !/\\\s*$/.test(trimmed))
        errors.push(diagnostic(n + 1, margin.length + 1, "Expected ':' at end of statement"));
      if (width === 0 && /^(?:print\s*\(|[A-Za-z_]\w*\s*(?:=(?!=)|\+=|-=))/.test(trimmed))
        errors.push(diagnostic(n + 1, margin.length + 1, "Executable statement outside a function"));
    }
    if (!block && stack.length === 0) pending = /:\s*$/.test(trimmed);
    continuation = stack.length > 0 || /\\\s*$/.test(trimmed);
  }
  if (pending) errors.push(diagnostic(lines.length, 1, "Expected an indented block"));
  for (const open of stack) errors.push(diagnostic(open.line, open.column, `Unclosed '${open.c}'`));
  if (block) errors.push(diagnostic(lines.length, 1, "Unterminated multiline string"));
  return errors;
}

async function command(program, args, input) {
  return new Promise((resolve) => {
    const child = spawn(program, args, { stdio: ["pipe", "pipe", "pipe"], timeout: 10000 });
    let output = "";
    for (const stream of [child.stdout, child.stderr]) stream.on("data", (part) => { output += part.toString().slice(0, 4000); });
    child.on("error", (error) => resolve({ unavailable: error.code === "ENOENT", output: error.message, code: -1 }));
    child.on("close", (code) => resolve({ code, output }));
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

export async function validateSource(file, source) {
  const language = languageOf(file);
  if (source.includes("\0")) return { language, parser: "unavailable (binary resource)", errors: [] };
  if (language === "GDScript") return { language, parser: "GDScript structural parser", errors: parseGdscript(source) };
  if (language === "Godot Resource") {
    const lines = source.replace(/\r\n?/g, "\n").split("\n"), errors = [];
    const header = path.extname(file).toLowerCase() === ".tscn" ? "gd_scene" : "gd_resource";
    if (!new RegExp(`^\\[${header}\\b`).test(lines[0])) errors.push(diagnostic(1, 1, `Expected [${header} ...] header`));
    for (let i = 0; i < lines.length; i++) {
      if (/^\[ext_resource\b/.test(lines[i]) && !/\bid=(?:"[^"]+"|\d+)/.test(lines[i]))
        errors.push(diagnostic(i + 1, 1, "External resource is missing an id"));
      if (/\btype="Progress"/.test(lines[i])) errors.push(diagnostic(i + 1, 1, "Progress is not a Godot 3 node type"));
    }
    return { language, parser: "Godot resource validator", errors };
  }
  const runners = {
    JavaScript: ["node", ["--check", "-"]], Lua: ["luac", ["-p", "-"]],
    Python: ["python3", ["-c", "import ast,sys; ast.parse(sys.stdin.read(), filename=sys.argv[1])", file]],
  };
  const runner = runners[language];
  if (!runner) return { language, parser: "unavailable", errors: [] };
  let result;
  if (language === "JavaScript") {
    const temp = await fsp.mkdtemp(path.join(os.tmpdir(), "rea-syntax-"));
    try {
      const target = path.join(temp, `source${path.extname(file)}`);
      await fsp.writeFile(target, source);
      result = await command("node", ["--check", target], "");
    } finally { await fsp.rm(temp, { recursive: true, force: true }); }
  } else result = await command(...runner, source);
  if (result.unavailable) return { language, parser: "unavailable", errors: [] };
  const match = result.output.match(/(?:line |:)(\d+)(?::(\d+))?/);
  const message = result.output.match(/(?:SyntaxError|Error):[^\n]*/)?.[0] ||
    result.output.trim().split("\n").find((line) => /(?:error|near|expected)/i.test(line)) || "Syntax error";
  return { language, parser: runner[0], errors: result.code === 0 ? [] :
    [diagnostic(Number(match?.[1] || 1), Number(match?.[2] || 1), message)] };
}

async function filesUnder(root, limit = 10000) {
  const files = [], dirs = [root], visited = new Set();
  while (dirs.length && files.length < limit) {
    const dir = dirs.pop();
    const real = await fsp.realpath(dir).catch(() => "");
    if (!real || visited.has(real)) continue;
    visited.add(real);
    for (const entry of await fsp.readdir(dir, { withFileTypes: true }).catch(() => [])) {
      const full = path.join(dir, entry.name);
      const kind = entry.isSymbolicLink() ? await fsp.stat(full).catch(() => null) : entry;
      if (kind?.isDirectory()) dirs.push(full);
      else if (kind?.isFile() && languageOf(entry.name)) files.push(full);
    }
  }
  return files;
}

export async function validateProject(projectDir, kbDir = "") {
  const roots = [path.join(projectDir, "decompiled")];
  if (kbDir) roots.push(path.join(kbDir, "decompiled"));
  const project = JSON.parse(await fsp.readFile(path.join(projectDir, "project.json"), "utf8"));
  const diagnostics = [], languages = {}, parsers = {}, seen = new Set();
  let checked = 0, unsupported = 0;
  const check = async (rel, file, origin) => {
    if (seen.has(rel)) return;
    seen.add(rel);
    const stat = await fsp.stat(file);
    if (stat.size > 2 * 1024 * 1024) return;
    const source = await fsp.readFile(file, "utf8");
    const result = await validateSource(rel, source);
    checked++;
    languages[result.language] = (languages[result.language] || 0) + 1;
    parsers[result.parser] = (parsers[result.parser] || 0) + 1;
    if (result.parser.startsWith("unavailable")) unsupported++;
    for (const error of result.errors) diagnostics.push({ path: rel, source: origin,
      browserPath: project.engine === "godot" && origin === "decompiled"
        ? `${project.pckRel}/gdre/${rel.replace(/^project\//, "")}` : "", ...error });
  };
  for (const root of roots) for (const file of await filesUnder(root)) {
    const rel = path.relative(root, file).replaceAll(path.sep, "/");
    if (root !== roots[0] && project.engine === "godot" && rel.startsWith(`${project.pckRel}/gdre/`)) continue;
    const overlay = path.join(projectDir, "src/files", rel.replace(/^project\//, ""));
    const patched = await fsp.stat(overlay).catch(() => null);
    await check(rel, patched ? overlay : file, patched ? "overlay" : "decompiled");
  }
  const overlayRoot = path.join(projectDir, "src/files");
  for (const file of await filesUnder(overlayRoot)) {
    const rel = `project/${path.relative(overlayRoot, file).replaceAll(path.sep, "/")}`;
    await check(rel, file, "overlay");
  }
  return { project: project.id, engine: project.engine, checked, unsupported, languages, parsers,
    total: diagnostics.length, diagnostics: diagnostics.slice(0, 500), truncated: diagnostics.length > 500 };
}
