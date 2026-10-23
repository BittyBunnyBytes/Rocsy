// Agent requests REA's server makes on its own (e.g. "a test run failed, fix it"). The Agent
// service can't be reached from this container's network, so it polls GET /REA/api/outbox and
// reports back which session took each request (POST /outbox/<id>/done).
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

const DATA = process.env.REA_WEB_DATA || "/home/node/rea-web";
const FILE = path.join(DATA, "outbox.json");
const MODS = "/opt/data/rocsy-workspace/rea-mods";
const handlers = new Map(); // kind -> async (item, session) after delivery

async function load() {
  try { const items = JSON.parse(await fsp.readFile(FILE, "utf8")); return Array.isArray(items) ? items : []; } catch { return []; }
}
async function save(items) {
  await fsp.mkdir(DATA, { recursive: true });
  await fsp.writeFile(FILE + ".tmp", JSON.stringify(items, null, 1));
  await fsp.rename(FILE + ".tmp", FILE);
}

export function onDelivered(kind, fn) { handlers.set(kind, fn); }

// REA's one Master Agent model + thinking level (Models dialog), used for every REA task.
// model "" = the Agentic settings' model; thinking defaults to medium.
export const DEFAULT_MODEL = { model: "", provider: "", reasoning: "medium" };
export function reaModel(settings) {
  const m = settings?.model;
  if (m && typeof m === "object") return { model: String(m.model || ""), provider: m.model ? String(m.provider || "") : "", reasoning: String(m.reasoning || "") };
  return { ...DEFAULT_MODEL };                      // also replaces the old per-task choices
}
export async function modelFor() {
  try { return reaModel(JSON.parse(await fsp.readFile(path.join(DATA, "settings.json"), "utf8"))); }
  catch { return { ...DEFAULT_MODEL }; }
}

// One pending request per key (a newer failure replaces an undelivered older one).
export async function queueAgent({ key, kind, session = "", text, model, meta = {} }) {
  const items = (await load()).filter((x) => !key || x.key !== key);
  const item = { id: randomBytes(6).toString("hex"), key: key || "", kind, session, text, model: model || { model: "" }, meta, created: Date.now() };
  items.push(item);
  await save(items.slice(-50));
  return item;
}

export async function handleOutbox({ method, api, req, res, send, readJson, httpError }) {
  if (api === "/outbox" && method === "GET") {
    // Requests older than a day are dropped: the situation they describe has moved on.
    // So are requests about a mod that has been deleted since.
    const items = (await load()).filter((x) => Date.now() - x.created < 86400000 &&
      (!x.meta?.mod || fs.existsSync(path.join(MODS, x.meta.mod, "project.json"))));
    send(res, 200, { items });
    return true;
  }
  const match = api.match(/^\/outbox\/([0-9a-f]{12})\/done$/);
  if (match && method === "POST") {
    const body = await readJson(req);
    const items = await load();
    const item = items.find((x) => x.id === match[1]);
    if (!item) throw httpError(404, "Unknown request");
    await save(items.filter((x) => x.id !== item.id));
    const session = String(body.session || "").slice(0, 80);
    const fn = handlers.get(item.kind);
    if (fn && session) await fn(item, session).catch((error) => console.error(`outbox ${item.kind}: ${error.message}`));
    send(res, 200, { ok: true });
    return true;
  }
  return false;
}
