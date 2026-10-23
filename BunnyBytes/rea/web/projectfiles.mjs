// Read-only file browser for mod and rewrite projects.
import fs from "node:fs/promises";
import path from "node:path";
import { containedFile, kindOf, sendRaw } from "./decomp.mjs";

const MAX_FILES = 80000;
const MAX_DEPTH = 64;

export async function projectTree(root) {
  const files = [];
  const visit = async (dir, prefix, depth) => {
    if (depth > MAX_DEPTH || files.length >= MAX_FILES) return;
    const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
    entries.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    for (const entry of entries) {
      if (files.length >= MAX_FILES) break;
      // Never follow project-created links outside the project (or cycles inside it).
      if (entry.isSymbolicLink()) continue;
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) await visit(full, rel, depth + 1);
      else if (entry.isFile()) {
        const stat = await fs.stat(full).catch(() => null);
        if (stat) files.push({ p: rel, v: rel, s: stat.size, k: kindOf(rel) });
      }
    }
  };
  await visit(root, "", 0);
  return { dir: root, files, truncated: files.length >= MAX_FILES };
}

export async function projectFile({ root, action, method, url, req, res, send, httpError }) {
  if (method === "GET" && action === "tree") {
    send(res, 200, await projectTree(root));
    return true;
  }
  if ((method === "GET" || method === "HEAD") && action === "raw") {
    const rel = url.searchParams.get("path");
    const file = rel && await containedFile(root, rel);
    const stat = file && await fs.stat(file).catch(() => null);
    if (!stat?.isFile()) throw httpError(404, "File not found");
    await sendRaw(req, res, file, url);
    return true;
  }
  return false;
}
