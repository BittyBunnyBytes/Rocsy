// Rocsy Motion: renders the Master Agent's Remotion (React) projects.
//
// Projects live in the agent's workspace (~/.hermes/rocsy-workspace, Hermes sees it at
// /opt/data/rocsy-workspace, here /workspace). A render bundles the project (webpack, resolving
// packages from this image's node_modules), draws every frame in Chrome for Testing with GPU GL,
// renders the audio as WAV, and encodes on the GPU with ffmpeg h264_vaapi. Loopback only: the
// Agent service is the caller, and it holds the shared GPU slot (bunny-lock) for full renders.
import http from "node:http";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import { bundle } from "@remotion/bundler";
import { getCompositions, makeCancelSignal, renderFrames, renderMedia, renderStill, selectComposition } from "@remotion/renderer";

const PORT = Number(process.env.MOTION_PORT || 8970);
const HOST = process.env.MOTION_HOST || "127.0.0.1";
const WS = process.env.MOTION_WORKSPACE || "/workspace";
const HERMES_WS = (process.env.MOTION_HERMES_WORKSPACE || "/opt/data/rocsy-workspace").replace(/\/$/, "");
const DATA = process.env.MOTION_DATA || "/data";
const TEMPLATE = "/app/template";
const RENDER_NODE = process.env.MOTION_VAAPI_DEVICE || "/dev/dri/renderD128";
const GL = process.env.MOTION_GL || "angle";                // Chrome's GPU backend (angle | vulkan | egl)
const MIN_FREE_MB = Number(process.env.MOTION_MIN_FREE_VRAM_MB || 1500);
const QP = Number(process.env.MOTION_QP || 20);             // h264_vaapi CQP (~ libx264 CRF 16)
// Chrome for Testing (installed at build): it can use the GPU drivers; headless-shell can't. Passed as
// browserExecutable on every call, since not every Remotion API takes chromeMode.
const CHROME = process.env.MOTION_CHROME || "/app/node_modules/.remotion/chrome-for-testing/linux64/chrome-linux64/chrome";
const browser = { chromiumOptions: { gl: GL, enableMultiProcessOnLinux: true, headless: true }, chromeMode: "chrome-for-testing",
                  browserExecutable: CHROME, logLevel: "warn" };

const jobs = new Map();          // id -> job (persisted as DATA/jobs/<id>/job.json)
const bundles = new Map();       // project dir -> { sig, serveUrl }
let active = null;               // one render at a time

const log = (...a) => console.log(new Date().toISOString(), ...a);

// ---------------- helpers ----------------

class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }

function projectDir(value) {
  let p = String(value || "").trim();
  if (!p) throw new HttpError(422, "give the project path");
  if (p === HERMES_WS || p.startsWith(HERMES_WS + "/")) p = WS + p.slice(HERMES_WS.length);
  const dir = path.resolve(WS, p);
  if (!dir.startsWith(WS + "/")) throw new HttpError(422, `projects live in your workspace ${HERMES_WS}`);
  if (!fs.existsSync(path.join(dir, "src", "index.ts")) && !fs.existsSync(path.join(dir, "src", "index.tsx")))
    throw new HttpError(404, `no Remotion project at ${hermesPath(dir)} (src/index.ts missing; create one with motion new)`);
  return dir;
}
const hermesPath = (dir) => HERMES_WS + dir.slice(WS.length);
const entryOf = (dir) => fs.existsSync(path.join(dir, "src", "index.ts")) ? path.join(dir, "src", "index.ts") : path.join(dir, "src", "index.tsx");

async function signature(dir) {
  const parts = [];
  async function walk(d) {
    let entries = [];
    try { entries = await fsp.readdir(d, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (e.name === "node_modules" || e.name === "out" || e.name.startsWith(".")) continue;
      const full = path.join(d, e.name);
      if (e.isDirectory()) await walk(full);
      else { const st = await fsp.stat(full); parts.push(`${full}:${st.size}:${st.mtimeMs}`); }
    }
  }
  await walk(path.join(dir, "src"));
  await walk(path.join(dir, "public"));
  return crypto.createHash("sha1").update(parts.sort().join("|")).digest("hex");
}

async function bundled(dir, onProgress) {
  const sig = await signature(dir);
  const cached = bundles.get(dir);
  if (cached && cached.sig === sig && fs.existsSync(cached.serveUrl)) return cached.serveUrl;
  const outDir = path.join(DATA, "bundles", crypto.createHash("sha1").update(dir).digest("hex").slice(0, 16));
  await fsp.rm(outDir, { recursive: true, force: true });
  const serveUrl = await bundle({
    entryPoint: entryOf(dir), outDir, publicDir: path.join(dir, "public"), onProgress,
    // The project has no node_modules of its own: packages come from this image.
    webpackOverride: (config) => ({
      ...config,
      resolve: { ...config.resolve, modules: [...(config.resolve?.modules || ["node_modules"]), "/app/node_modules"] },
      resolveLoader: { ...config.resolveLoader, modules: [...(config.resolveLoader?.modules || ["node_modules"]), "/app/node_modules"] },
    }),
  });
  bundles.set(dir, { sig, serveUrl });
  return serveUrl;
}

function vramFreeMb() {
  // The biggest GPU's VRAM (the RX 7900 XTX); null when sysfs can't be read.
  let best = null;
  try {
    for (const card of fs.readdirSync("/sys/class/drm")) {
      if (!/^card\d+$/.test(card)) continue;
      const base = `/sys/class/drm/${card}/device`;
      try {
        const total = Number(fs.readFileSync(`${base}/mem_info_vram_total`, "utf8"));
        const used = Number(fs.readFileSync(`${base}/mem_info_vram_used`, "utf8"));
        if (!best || total > best.total) best = { total, free: (total - used) / 1048576 };
      } catch { /* not an amdgpu card */ }
    }
  } catch { /* no sysfs */ }
  return best ? Math.round(best.free) : null;
}

function needVram(what) {
  const free = vramFreeMb();
  if (free !== null && free < MIN_FREE_MB)
    throw new HttpError(409, `only ${free} MB of VRAM is free; ${what} needs ${MIN_FREE_MB} MB (the GPU is busy rendering). Try again after your turn, or render with motion render.`);
}

function props(value) {
  if (value === undefined || value === null || value === "") return {};
  const parsed = typeof value === "string" ? JSON.parse(value) : value;
  if (typeof parsed !== "object" || Array.isArray(parsed)) throw new HttpError(422, "--props must be a JSON object");
  return parsed;
}

function run(cmd, args, signal) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: ["ignore", "ignore", "pipe"] });
    let err = "";
    child.stderr.on("data", (d) => { err = (err + d).slice(-4000); });
    const kill = () => child.kill("SIGKILL");
    signal?.addEventListener("abort", kill);
    child.on("close", (code) => { signal?.removeEventListener("abort", kill); code === 0 ? resolve() : reject(new Error(`${cmd} failed: ${err.trim().split("\n").slice(-3).join(" ")}`)); });
    child.on("error", reject);
  });
}

// Error text for the agent: no colour codes, no stack frames.
const friendly = (e) => String(e?.message || e).replace(/\u001b\[[0-9;]*m/g, "").split("\n")
  .filter((l) => !/^\s+at /.test(l)).join("\n").slice(0, 1500);

// ---------------- jobs ----------------

function publicJob(j) {
  const { cancel, abort, ...rest } = j;
  return { ...rest, output_url: j.state === "complete" ? `/api/outputs/${j.id}` : null };
}
async function saveJob(j) {
  const dir = path.join(DATA, "jobs", j.id);
  await fsp.mkdir(dir, { recursive: true });
  await fsp.writeFile(path.join(dir, "job.json"), JSON.stringify(publicJob(j)));
}
function update(j, patch) { Object.assign(j, patch, { updated_at: Date.now() / 1000 }); saveJob(j).catch(() => {}); }

async function render(j) {
  const dir = projectDir(j.project);
  const work = path.join(DATA, "jobs", j.id);
  const frames = path.join(work, "frames");
  await fsp.mkdir(frames, { recursive: true });
  const { cancelSignal, cancel } = makeCancelSignal();
  const abort = new AbortController();
  j.cancel = () => { cancel(); abort.abort(); };
  try {
    needVram("a render");
    update(j, { state: "bundling", stage: "Compiling the project", progress: 1 });
    const serveUrl = await bundled(dir, (p) => update(j, { progress: Math.round(p / 20) }));
    const composition = await selectComposition({ serveUrl, id: j.composition, inputProps: j.props, ...browser });
    const { width, height, fps, durationInFrames } = composition;
    if (width % 2 || height % 2 || width < 128 || height < 128 || width > 4096 || height > 4096)
      throw new Error(`the composition is ${width}x${height}; use even sizes from 128 to 4096 px (GPU encode limits)`);
    update(j, { state: "rendering", stage: `Drawing ${durationInFrames} frames on the GPU`, width, height, fps,
                duration: +(durationInFrames / fps).toFixed(2), progress: 5 });
    await renderFrames({
      composition, serveUrl, inputProps: j.props, outputDir: frames, imageFormat: "jpeg", jpegQuality: 95, cancelSignal,
      onStart: () => {}, onFrameUpdate: (done) => {
        const progress = 5 + Math.floor((done / durationInFrames) * 80);
        if (progress !== j.progress) update(j, { progress, stage: `Frame ${done} of ${durationInFrames}` });
      },
      ...browser,
    });
    update(j, { state: "rendering", stage: "Mixing the audio", progress: 86 });
    const wav = path.join(work, "audio.wav");
    let audio = true;
    try {
      await renderMedia({ composition, serveUrl, inputProps: j.props, codec: "wav", outputLocation: wav, cancelSignal, ...browser });
    } catch (e) {
      if (abort.signal.aborted) throw e;
      log("audio render failed, using silence:", friendly(e));
      audio = false;
    }
    update(j, { state: "encoding", stage: "Encoding on the GPU (VA-API)", progress: 92 });
    const out = path.join(work, "out.mp4");
    await run("ffmpeg", [
      "-nostdin", "-v", "error", "-y", "-init_hw_device", `vaapi=va:${RENDER_NODE}`, "-filter_hw_device", "va",
      "-framerate", String(fps), "-pattern_type", "glob", "-i", path.join(frames, "*.jpeg"),
      ...(audio ? ["-i", wav] : ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]),
      "-map", "0:v", "-map", "1:a", "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", String(QP),
      "-c:a", "aac", "-b:a", "256k", "-t", String(durationInFrames / fps), "-movflags", "+faststart", out,
    ], abort.signal);
    await fsp.rm(frames, { recursive: true, force: true });
    const size = (await fsp.stat(out)).size;
    update(j, { state: "complete", stage: "", progress: 100, size, completed_at: Date.now() / 1000 });
    log("rendered", j.id.slice(0, 8), `${width}x${height}@${fps}`, `${durationInFrames} frames`, `${(size / 1e6).toFixed(1)} MB`);
  } catch (e) {
    await fsp.rm(frames, { recursive: true, force: true }).catch(() => {});
    if (abort.signal.aborted) update(j, { state: "cancelled", stage: "", error: "cancelled" });
    else update(j, { state: "failed", stage: "", error: friendly(e) });
    log("render", j.id.slice(0, 8), j.state, j.error);
  } finally {
    delete j.cancel;
  }
}

async function pump() {
  if (active) return;
  const next = [...jobs.values()].find((j) => j.state === "queued");
  if (!next) return;
  active = next;
  try { await render(next); } finally { active = null; setImmediate(pump); }
}

// ---------------- routes ----------------

async function readJson(req) {
  let body = "";
  for await (const chunk of req) { body += chunk; if (body.length > 1e6) throw new HttpError(413, "request too large"); }
  try { return body ? JSON.parse(body) : {}; } catch { throw new HttpError(400, "invalid JSON"); }
}

const routes = {
  "GET /health": async () => ({ status: "ok", gl: GL, vram_free_mb: vramFreeMb(), rendering: active?.id || null }),

  // A new project from the template: WORKSPACE/<session>/motion/<name>.
  "POST /api/projects": async (req) => {
    const body = await readJson(req);
    const session = String(body.session || "").replace(/[^a-f0-9]/g, "").slice(0, 32);
    const name = String(body.name || "").trim().replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
    if (!session || !name) throw new HttpError(422, "give the session and a project name (letters, digits, - and _)");
    const dir = path.join(WS, session, "motion", name);
    const existed = fs.existsSync(dir);
    if (!existed) await fsp.cp(TEMPLATE, dir, { recursive: true });
    const files = [];
    for (const f of await fsp.readdir(path.join(dir, "src"))) files.push(`src/${f}`);
    return { path: hermesPath(dir), existed, files: [...files, "public/", "AGENT.md"] };
  },

  // Length / size of files in a project's public/ folder (the agent sizes compositions to them).
  "POST /api/probe": async (req) => {
    const body = await readJson(req);
    const dir = projectDir(body.project);
    const files = [];
    for (const name of (Array.isArray(body.files) ? body.files : []).slice(0, 40)) {
      const file = path.resolve(dir, "public", String(name));
      if (!file.startsWith(path.join(dir, "public") + "/") || !fs.existsSync(file)) { files.push({ name, error: "missing" }); continue; }
      const out = await new Promise((resolve) => {
        const child = spawn("ffprobe", ["-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,avg_frame_rate",
                                        "-of", "json", file]);
        let text = ""; child.stdout.on("data", (d) => { text += d; });
        child.on("close", () => { try { resolve(JSON.parse(text)); } catch { resolve({}); } });
        child.on("error", () => resolve({}));
      });
      const v = (out.streams || []).find((s) => s.codec_type === "video");
      const a = (out.streams || []).find((s) => s.codec_type === "audio");
      const [num, den] = String(v?.avg_frame_rate || "0/1").split("/").map(Number);
      const still = /\.(png|jpe?g|webp|gif)$/i.test(name);
      files.push({ name, kind: still ? "image" : v ? "video" : a ? "audio" : "file",
                   seconds: still ? null : (Number(out.format?.duration) ? +Number(out.format.duration).toFixed(2) : null),
                   width: v?.width || null, height: v?.height || null, fps: !still && den ? +(num / den).toFixed(3) : null,
                   has_audio: !!a });
    }
    return { files };
  },

  // Compile and list the compositions (catches code errors before any rendering).
  "POST /api/compositions": async (req) => {
    const body = await readJson(req);
    const dir = projectDir(body.project);
    let serveUrl;
    try { serveUrl = await bundled(dir); } catch (e) { throw new HttpError(422, "the project doesn't compile: " + friendly(e)); }
    try {
      const list = await getCompositions(serveUrl, { inputProps: props(body.props), ...browser });
      return { compositions: list.map((c) => ({ id: c.id, width: c.width, height: c.height, fps: c.fps, frames: c.durationInFrames,
                                                 seconds: +(c.durationInFrames / c.fps).toFixed(2) })) };
    } catch (e) { throw new HttpError(422, "the project compiles but fails to load: " + friendly(e)); }
  },

  // One frame as a PNG in the project's out/ folder, for the agent's vision tool.
  "POST /api/still": async (req) => {
    const body = await readJson(req);
    const dir = projectDir(body.project);
    if (active) throw new HttpError(409, "a Motion render is running; look at frames after it finishes");
    needVram("a still");
    let serveUrl;
    try { serveUrl = await bundled(dir); } catch (e) { throw new HttpError(422, "the project doesn't compile: " + friendly(e)); }
    const inputProps = props(body.props);
    try {
      const composition = await selectComposition({ serveUrl, id: String(body.composition || "Main"), inputProps, ...browser });
      const frame = Math.max(0, Math.min(composition.durationInFrames - 1, Number(body.frame) || 0));
      await fsp.mkdir(path.join(dir, "out"), { recursive: true });
      const output = path.join(dir, "out", `still-${composition.id}-${frame}.png`);
      await renderStill({ composition, serveUrl, output, frame, imageFormat: "png", inputProps, ...browser });
      return { path: hermesPath(output), frame, of: composition.durationInFrames, fps: composition.fps, width: composition.width, height: composition.height };
    } catch (e) { throw new HttpError(422, "rendering the frame failed: " + friendly(e)); }
  },

  "POST /api/jobs": async (req) => {
    const body = await readJson(req);
    const project = hermesPath(projectDir(body.project));
    const j = { id: crypto.randomUUID().replace(/-/g, ""), project, composition: String(body.composition || "Main").slice(0, 100),
                props: props(body.props), label: String(body.label || "").slice(0, 300), state: "queued", stage: "Queued", progress: 0,
                error: "", created_at: Date.now() / 1000 };
    jobs.set(j.id, j);
    await saveJob(j);
    setImmediate(pump);
    return publicJob(j);
  },
  "GET /api/jobs": async () => ({ jobs: [...jobs.values()].sort((a, b) => b.created_at - a.created_at).slice(0, 200).map(publicJob) }),
};

async function jobRoute(method, id, action, res) {
  const j = jobs.get(id);
  if (!j) throw new HttpError(404, "no such job");
  if (method === "GET" && !action) return publicJob(j);
  if (method === "POST" && action === "cancel") {
    if (j.state === "queued") update(j, { state: "cancelled", stage: "", error: "cancelled" });
    else j.cancel?.();
    return publicJob(j);
  }
  throw new HttpError(404, "not found");
}

function stream(req, res, id) {
  const j = jobs.get(id);
  const file = path.join(DATA, "jobs", id, "out.mp4");
  if (!j || j.state !== "complete" || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
  const size = fs.statSync(file).size;
  const range = /bytes=(\d*)-(\d*)/.exec(req.headers.range || "");
  if (range) {
    const start = range[1] ? Number(range[1]) : size - Number(range[2]);
    const end = range[1] && range[2] ? Math.min(Number(range[2]), size - 1) : size - 1;
    res.writeHead(206, { "Content-Type": "video/mp4", "Content-Range": `bytes ${start}-${end}/${size}`, "Content-Length": end - start + 1, "Accept-Ranges": "bytes" });
    fs.createReadStream(file, { start, end }).pipe(res);
  } else {
    res.writeHead(200, { "Content-Type": "video/mp4", "Content-Length": size, "Accept-Ranges": "bytes" });
    fs.createReadStream(file).pipe(res);
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const send = (status, data) => { res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(data)); };
  try {
    if (req.headers["cf-connecting-ip"]) throw new HttpError(403, "forbidden");
    let m;
    if (req.method === "GET" && (m = /^\/api\/outputs\/([0-9a-f]{32})$/.exec(url.pathname))) return stream(req, res, m[1]);
    if ((m = /^\/api\/jobs\/([0-9a-f]{32})(?:\/(cancel))?$/.exec(url.pathname))) return send(200, await jobRoute(req.method, m[1], m[2], res));
    const handler = routes[`${req.method} ${url.pathname}`];
    if (!handler) throw new HttpError(404, "not found");
    send(200, await handler(req));
  } catch (e) {
    if (!(e instanceof HttpError)) log("error", req.method, url.pathname, friendly(e));
    send(e.status || 500, { detail: e instanceof HttpError ? e.message : friendly(e) });
  }
});

// Jobs from before a restart: finished ones stay listed, unfinished ones failed.
try {
  for (const id of fs.readdirSync(path.join(DATA, "jobs"))) {
    try {
      const j = JSON.parse(fs.readFileSync(path.join(DATA, "jobs", id, "job.json"), "utf8"));
      if (!["complete", "failed", "cancelled"].includes(j.state)) Object.assign(j, { state: "failed", error: "Motion restarted during the render" });
      delete j.output_url;
      jobs.set(j.id, j);
    } catch { /* skip */ }
  }
} catch { /* first start */ }

process.on("unhandledRejection", (e) => log("unhandled:", friendly(e)));
server.listen(PORT, HOST, () => log(`bunny-motion on ${HOST}:${PORT} (gl=${GL}, ${jobs.size} jobs)`));
