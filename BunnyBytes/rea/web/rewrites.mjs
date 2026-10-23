// Standalone game rewrite projects. The original game is mounted read-only;
// agents write a separate implementation and record parity evidence here.
import { randomBytes } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { projectFile } from "./projectfiles.mjs";
import { readGoals } from "./goals.mjs";

const ROOT = "/opt/data/rocsy-workspace/rea-rewrites";
const idPattern = /^[0-9a-f]{12}$/;
const stages = new Set(["planning", "approved", "building", "testing"]);
const limit = (value, n) => String(value || "").trim().slice(0, n);
const location = (id) => path.join(ROOT, id);

async function read(id) {
  if (!idPattern.test(id)) return null;
  try { return JSON.parse(await fs.readFile(path.join(location(id), "project.json"), "utf8")); }
  catch { return null; }
}

async function save(project) {
  const file = path.join(location(project.id), "project.json");
  await fs.writeFile(`${file}.tmp`, JSON.stringify(project, null, 2));
  await fs.rename(`${file}.tmp`, file);
}

async function view(project) {
  const dir = location(project.id);
  const plan = await fs.readFile(path.join(dir, "PLAN.md"), "utf8").catch(() => "");
  let parity = [];
  try {
    const data = JSON.parse(await fs.readFile(path.join(dir, "PARITY.json"), "utf8"));
    if (Array.isArray(data)) parity = data.filter((row) => row && typeof row.name === "string").map((row) => ({
      name: limit(row.name, 160), status: ["unverified", "passed", "failed"].includes(row.status) ? row.status : "unverified",
      evidence: limit(row.evidence, 2000),
    })).slice(0, 500);
  } catch {}
  const source = await fs.readdir(path.join(dir, "src")).catch(() => []);
  return { ...project, projectPath: dir, plan, parity, sourceCount: source.length, goals: await readGoals(dir),
    parityPassed: parity.filter((row) => row.status === "passed" && row.evidence).length };
}

export async function handleRewrites({ method, api, url, req, res, send, readJson, httpError, targets }) {
  if (api === "/mods/rewrites") {
    if (method === "GET") {
      const folder = url.searchParams.get("folder");
      const ids = await fs.readdir(ROOT).catch(() => []);
      const projects = (await Promise.all(ids.map(read))).filter((p) => p && (!folder || p.folder === folder));
      send(res, 200, { rewrites: await Promise.all(projects.sort((a, b) => b.created - a.created).map(view)) });
      return true;
    }
    if (method === "POST") {
      const body = await readJson(req);
      const folder = targets[String(body.folder)];
      if (!folder || folder.type !== "folder") throw httpError(404, "Pick a game folder first");
      const language = limit(body.language, 80), engine = limit(body.engine, 80);
      if (!language && !engine) throw httpError(400, "Choose a target language or engine");
      const name = limit(body.name, 120) || `${folder.name} rewrite`;
      const id = randomBytes(6).toString("hex");
      const project = { id, folder: folder.id, gameName: folder.name, gamePath: folder.path,
        name, language, engine, goal: limit(body.goal, 4000), stage: "new", session: "", created: Date.now() };
      const dir = location(id);
      await fs.mkdir(path.join(dir, "src"), { recursive: true });
      await fs.mkdir(path.join(dir, "tests"), { recursive: true });
      await save(project);
      await fs.writeFile(path.join(dir, "GOAL.md"), `# ${name}\n\nOriginal game: ${folder.name}\nTarget language: ${language || "to decide"}\nTarget engine: ${engine || "to decide"}\n\n${project.goal}\n\nThe goal is feature and behavior parity with the original game. Record each observed behavior and its test in PARITY.json. Do not mark parity passed without evidence.\n`);
      await fs.writeFile(path.join(dir, "PLAN.md"), "");
      await fs.writeFile(path.join(dir, "PARITY.json"), "[]\n");
      send(res, 201, await view(project)); return true;
    }
  }
  const match = api.match(/^\/mods\/rewrites\/([0-9a-f]{12})(?:\/(session|tree|raw))?$/);
  if (!match) return false;
  const project = await read(match[1]);
  if (!project) throw httpError(404, "Unknown rewrite project");
  if (await projectFile({ root: location(project.id), action: match[2], method, url, req, res, send, httpError })) return true;
  if (method === "GET" && !match[2]) { send(res, 200, await view(project)); return true; }
  if (method === "POST" && match[2] === "session") {
    const body = await readJson(req);
    if (body.session !== undefined) project.session = limit(body.session, 80);
    if (stages.has(body.stage)) project.stage = body.stage;
    await save(project);
    send(res, 200, await view(project)); return true;
  }
  if (method === "DELETE" && !match[2]) {
    await fs.rm(location(project.id), { recursive: true, force: true });
    send(res, 200, { ok: true }); return true;
  }
  return false;
}
