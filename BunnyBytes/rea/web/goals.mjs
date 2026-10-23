// The agent's standing goal for a mod or rewrite, split into milestones it checks off with proof
// (Agent service: `$R goal parts` / `$R goal done`). The Agent mirrors every change here by session
// id, so the Goals tab shows it and a fresh agent session sees what is already proven.
import fs from "node:fs/promises";
import path from "node:path";

const ROOTS = ["/opt/data/rocsy-workspace/rea-mods", "/opt/data/rocsy-workspace/rea-rewrites"];
const FILE = "GOALS.json";
const text = (value, n) => String(value || "").trim().slice(0, n);

function clean(goal) {
  if (!goal || typeof goal !== "object") return null;
  const parts = Array.isArray(goal.parts) ? goal.parts.slice(0, 20).map((p) => ({
    text: text(p?.text, 400), status: p?.status === "done" ? "done" : "todo", evidence: text(p?.evidence, 1000),
  })).filter((p) => p.text) : [];
  return {
    text: text(goal.text, 12000), status: ["active", "paused", "done", "cleared"].includes(goal.status) ? goal.status : "active",
    parts, part: Math.max(0, Math.min(Number(goal.part) || 0, parts.length)), achieved: Boolean(goal.achieved),
    evidence: text(goal.evidence, 1000), turns: Number(goal.turns) || 0, maxTurns: Number(goal.max_turns ?? goal.maxTurns) || 0,
    session: text(goal.session, 80), updated: Date.now(),
  };
}

export async function readGoals(dir) {
  try { return clean(JSON.parse(await fs.readFile(path.join(dir, FILE), "utf8"))); } catch { return null; }
}

// Every mod/rewrite project whose agent session is `sid`.
async function projectsFor(sid) {
  const out = [];
  for (const root of ROOTS) for (const id of await fs.readdir(root).catch(() => [])) {
    if (!/^[0-9a-f]{12}$/.test(id)) continue;
    try {
      const p = JSON.parse(await fs.readFile(path.join(root, id, "project.json"), "utf8"));
      if (p.session === sid) out.push(path.join(root, id));
    } catch {}
  }
  return out;
}

export async function handleGoals({ method, api, req, res, send, readJson, httpError }) {
  const match = api.match(/^\/goals\/session\/([0-9a-f]{32})$/);
  if (!match || method !== "POST") return false;
  const body = await readJson(req);
  const goal = body.goal === null ? null : clean({ ...body.goal, session: match[1] });
  if (body.goal !== null && !goal) throw httpError(400, "Give the goal object");
  const saved = [];
  for (const dir of await projectsFor(match[1])) {
    if (goal) {
      await fs.writeFile(path.join(dir, `${FILE}.tmp`), JSON.stringify(goal, null, 1));
      await fs.rename(path.join(dir, `${FILE}.tmp`), path.join(dir, FILE));
    } else {
      // A cleared goal stays visible as history until the next one replaces it.
      const old = await readGoals(dir);
      if (old) await fs.writeFile(path.join(dir, FILE), JSON.stringify({ ...old, status: "cleared", updated: Date.now() }, null, 1));
    }
    saved.push(path.basename(dir));
  }
  send(res, 200, { saved });
  return true;
}
