// Screenshot test runs: the game gets a real (virtual) display instead of running headless, and
// REA saves what it shows at fixed moments (startup, the menu it settles on, after a few inputs that
// usually start a game, the last frame) plus when the mod loads or an error appears.
// The display is Xvfb with Mesa's software renderer: this container has no GPU (and must not touch
// the one that also drives the desktop). Xvfb -fbdir keeps the screen in an XWD file, read here and
// written out as PNG, so no screenshot tool is needed. Inputs go through xdotool.
//   runs/<run>/shots/<name>.png + run.json shots[] {file, label, at, base}
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fsp from "node:fs/promises";
import path from "node:path";
import zlib from "node:zlib";

export const WIDTH = 1280, HEIGHT = 720;
const MAX_SHOTS = 24;          // per play (the mod run and the baseline each)
const MAX_EVENT_SHOTS = 4;     // mod-loaded / error captures per play

// Software GL everywhere (llvmpipe), a few threads only so the desktop stays responsive.
export const DISPLAY_ENV = { LIBGL_ALWAYS_SOFTWARE: "1", GALLIUM_DRIVER: "llvmpipe", LP_NUM_THREADS: "4",
  SDL_AUDIODRIVER: "dummy", ALSOFT_DRIVERS: "null", PULSE_SERVER: "none", __GL_SYNC_TO_VBLANK: "0", vblank_mode: "0" };

// ------------------------------------------------------------------------------ display --
// Start Xvfb on a free display number; resolves once it is accepting clients.
export async function startDisplay(dir, log) {
  const fbdir = path.join(dir, "fb");
  await fsp.mkdir(fbdir, { recursive: true });
  const child = spawn("Xvfb", ["-displayfd", "3", "-screen", "0", `${WIDTH}x${HEIGHT}x24`, "-fbdir", fbdir, "-nolisten", "tcp", "-ac", "+extension", "GLX", "-noreset"],
    { detached: true, stdio: ["ignore", "ignore", "pipe", "pipe"] });
  child.stderr.on("data", (d) => { const t = String(d); if (!/^(_XSERVTransmkdir|The XKEYBOARD keymap compiler)/.test(t)) log.write(`[Xvfb] ${t}`); });
  const display = await new Promise((resolve, reject) => {
    let out = "";
    const timer = setTimeout(() => reject(new Error("Xvfb didn't start within 15 s")), 15000);
    child.stdio[3].on("data", (d) => { out += d; if (out.includes("\n")) { clearTimeout(timer); resolve(`:${out.trim()}`); } });
    child.on("error", (e) => { clearTimeout(timer); reject(e); });
    child.on("exit", (code) => { clearTimeout(timer); reject(new Error(`Xvfb exited (code ${code})`)); });
  });
  log.write(`Virtual display ${display} (${WIDTH}x${HEIGHT}, software rendering)\n`);
  return {
    display, fb: path.join(fbdir, "Xvfb_screen0"),
    stop() { try { process.kill(-child.pid, "SIGTERM"); } catch {} setTimeout(() => { try { process.kill(-child.pid, "SIGKILL"); } catch {} }, 2000).unref(); },
  };
}

// xdotool on that display; never throws (an input that can't be sent just isn't).
export function xdo(display, args) {
  return new Promise((resolve) => {
    const child = spawn("xdotool", args, { env: { ...process.env, DISPLAY: display }, stdio: "ignore" });
    const timer = setTimeout(() => child.kill("SIGKILL"), 10000);
    child.on("error", () => { clearTimeout(timer); resolve(false); });
    child.on("close", (code) => { clearTimeout(timer); resolve(code === 0); });
  });
}

// ---------------------------------------------------------------------------- XWD → PNG --
// Xvfb's XWD header is 25 big-endian CARD32s, then the window name, then ncolors × 12-byte colours.
export function xwdToRgb(buf) {
  const u = (i) => buf.readUInt32BE(i * 4);
  const headerSize = u(0), format = u(2), width = u(4), height = u(5), byteOrder = u(7), bpp = u(11), stride = u(12);
  const masks = [u(14), u(15), u(16)], ncolors = u(19);
  if (format !== 2 || (bpp !== 32 && bpp !== 24)) throw new Error(`Unsupported XWD (format ${format}, ${bpp} bpp)`);
  const offset = headerSize + ncolors * 12;
  const shift = masks.map((m) => { let s = 0; while (m && !(m & 1)) { m >>>= 1; s++; } return s; });
  const rgb = Buffer.alloc(width * height * 3);
  const step = bpp / 8;
  for (let y = 0; y < height; y++) {
    let src = offset + y * stride, dst = y * width * 3;
    for (let x = 0; x < width; x++, src += step) {
      const v = step === 4 ? (byteOrder ? buf.readUInt32BE(src) : buf.readUInt32LE(src))
        : byteOrder ? (buf[src] << 16) | (buf[src + 1] << 8) | buf[src + 2] : buf[src] | (buf[src + 1] << 8) | (buf[src + 2] << 16);
      rgb[dst++] = (v & masks[0]) >>> shift[0];
      rgb[dst++] = (v & masks[1]) >>> shift[1];
      rgb[dst++] = (v & masks[2]) >>> shift[2];
    }
  }
  return { width, height, rgb };
}

const CRC = new Int32Array(256).map((_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c; });
function crc32(buf) { let c = -1; for (const b of buf) c = CRC[(c ^ b) & 255] ^ (c >>> 8); return (c ^ -1) >>> 0; }
function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type), data]);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}
export function rgbToPng({ width, height, rgb }, sub = false) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y++) rgb.copy(raw, y * (width * 3 + 1) + 1, y * width * 3, (y + 1) * width * 3);
  // PNG "Sub" filter (each byte minus the pixel to its left): game frames deflate far smaller.
  if (sub) for (let y = 0; y < height; y++) {
    const row = y * (width * 3 + 1);
    raw[row] = 1;
    for (let i = width * 3; i > 3; i--) raw[row + i] = (raw[row + i] - raw[row + i - 3]) & 255;
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(raw, { level: 6 })), chunk("IEND", Buffer.alloc(0))]);
}

// ------------------------------------------------------------------------------ shooter --
// Collects one play's screenshots. grab(): a PNG buffer of the current frame (or null).
// Identical consecutive frames are kept once; an all-black frame is labelled as such.
export function shooter({ dir, prefix, base = false, log, grab, maxEvents = MAX_EVENT_SHOTS }) {
  const shots = [];
  let last = "", n = 0, events = 0, started = Date.now(), busy = Promise.resolve();
  const take = (label) => (busy = busy.then(async () => {
    if (shots.length >= MAX_SHOTS) return;
    let frame;
    try { frame = await grab(); } catch (error) { log.write(`[screenshot] ${label}: ${error.message}\n`); return; }
    if (!frame) return;
    const hash = createHash("sha1").update(frame.key || frame.png).digest("hex");
    if (hash === last) { if (shots.length) shots[shots.length - 1].label += ` · ${label}`; return; }
    last = hash;
    const file = `${prefix}${String(++n).padStart(2, "0")}.png`;
    await fsp.mkdir(dir, { recursive: true });
    await fsp.writeFile(path.join(dir, file), frame.png);
    shots.push({ file, label: frame.black ? `${label} (black screen)` : label, at: Math.round((Date.now() - started) / 100) / 10, base });
    log.write(`[screenshot] ${file}: ${label}\n`);
  }));
  return {
    shots, take,
    restart() { started = Date.now(); },
    // A one-off capture a moment after something happened (the mod loaded, an error line).
    event(label, delayMs = 1500) {
      if (events >= maxEvents) return;
      events++;
      setTimeout(() => take(label), delayMs).unref();
    },
    done: () => busy,
  };
}

// Half size (2×2 box average): the agent's screenshots go to a vision model, where pixels cost time.
function halve({ width, height, rgb }) {
  const w = width >> 1, h = height >> 1, out = Buffer.alloc(w * h * 3);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) for (let c = 0; c < 3; c++) {
    const a = ((2 * y) * width + 2 * x) * 3 + c, b = a + width * 3;
    out[(y * w + x) * 3 + c] = (rgb[a] + rgb[a + 3] + rgb[b] + rgb[b + 3] + 2) >> 2;
  }
  return { width: w, height: h, rgb: out };
}

// The current Xvfb frame as a PNG (small = half size, for the agent's runs).
export function xvfbGrab(fb, small = false) {
  return async () => {
    const { width, height, rgb } = xwdToRgb(await fsp.readFile(fb));
    let lit = false;
    for (let i = 0; i < rgb.length && !lit; i += 997) lit = rgb[i] > 8;
    return { png: small ? rgbToPng(halve({ width, height, rgb }), true) : rgbToPng({ width, height, rgb }), key: rgb, black: !lit };
  };
}

// The capture plan for a run of `ms`: startup → menu → three inputs that usually get a game going
// (Enter, a click in the middle, holding W/→ to move) → the last frame. `input(step)` sends one.
export function plan(ms, input, mode = "full") {
  const at = (f) => Math.max(1500, Math.round(ms * f));
  if (mode === "three") return planThree(ms, at, input);
  return [
    ...(ms >= 20000 ? [{ at: 2500, label: "first frame" }] : []),
    { at: at(0.12), label: "startup" },
    { at: at(0.25), label: "loading / intro" },
    { at: at(0.4), label: "main menu (before any input)" },
    { at: at(0.48), act: () => input("enter"), label: "after pressing Enter" },
    { at: at(0.6), act: () => input("click"), label: "after clicking the middle of the screen" },
    { at: at(0.72), act: () => input("move"), label: "gameplay: after holding W / → for 2 s" },
    { at: at(0.84), label: "gameplay" },
    { at: Math.max(at(0.95), ms - 2500), label: "last frame before the time limit" },
  ];
}

// The agent's test runs: the same inputs, but only three frames (like its play tests on the real
// game): 15 s in, half way, just before the time limit. Frames under 3 s apart are merged.
function planThree(ms, at, input) {
  const shots = [];
  const times = [[Math.min(15000, ms - 3000), "15 s in"], [ms / 2, "half way"], [ms - 3000, "just before the end"]].sort((a, b) => a[0] - b[0]);
  for (const [t, label] of times) {
    const when = Math.max(1500, Math.round(t));
    if (shots.length && when - shots[shots.length - 1].at < 3000) continue;
    shots.push({ at: when, label });
  }
  return [
    { at: at(0.48), act: () => input("enter"), noShot: true },
    { at: at(0.6), act: () => input("click"), noShot: true },
    { at: at(0.72), act: () => input("move"), noShot: true },
    ...shots,
  ];
}

// Runs a capture plan next to a running game; stop() cancels what hasn't happened yet.
export function schedule(steps, shoot) {
  const timers = [];
  for (const s of steps) timers.push(setTimeout(async () => {
    if (s.act) { try { await s.act(); } catch {} if (s.noShot) return; await new Promise((r) => setTimeout(r, 1500)); }
    shoot.take(s.label);
  }, s.at));
  return { stop() { for (const t of timers) clearTimeout(t); } };
}

// Inputs on an X display: the pointer is parked mid-screen and the game window focused first.
export function xInput(display) {
  const mid = ["mousemove", String(WIDTH / 2), String(HEIGHT / 2)];
  return async (step) => {
    // There is no window manager to hand out keyboard focus: give it to the game's top-level window(s).
    await xdo(display, ["search", "--onlyvisible", "--maxdepth", "1", "--name", ".", "windowfocus", "%@"]);
    if (step === "enter") return xdo(display, [...mid, "key", "Return"]);
    if (step === "click") return xdo(display, [...mid, "click", "1"]);
    if (step === "move") {
      await xdo(display, [...mid, "keydown", "w", "keydown", "Right"]);
      await new Promise((r) => setTimeout(r, 2000));
      return xdo(display, ["keyup", "w", "keyup", "Right"]);
    }
  };
}
