// Windows PE resources (icons, bitmaps, cursors, version info, string tables, manifests, RCDATA...)
// pulled out of an .exe/.dll into ordinary files, so a native game's embedded assets show up in the
// Decompiled tab's file tree. Pure parsing with bounded reads: nothing from the file is executed.
import fsp from "node:fs/promises";
import path from "node:path";

const TYPES = { 1: "Cursors", 2: "Bitmaps", 3: "Icons", 4: "Menus", 5: "Dialogs", 6: "Strings", 7: "Fonts", 8: "Fonts",
  9: "Accelerators", 10: "RCDATA", 11: "Message tables", 12: "Cursors", 14: "Icons", 16: "Version", 17: "Dialogs",
  21: "Cursors", 22: "Icons", 23: "HTML", 24: "Manifest" };
const MAX_ENTRY = 64 * 1024 * 1024;
const MAX_TOTAL = 512 * 1024 * 1024;
const MAX_ENTRIES = 5000;

const safe = (s) => String(s).replace(/[^\w.+@ -]+/g, "_").replace(/^\.+/, "_").slice(0, 100) || "_";

// A file extension from the first bytes, for RCDATA and custom resource types.
function sniffExt(b) {
  const hex = b.subarray(0, 4).toString("hex");
  const ascii = b.subarray(0, 16).toString("latin1");
  if (hex === "89504e47") return "png";
  if (hex.startsWith("ffd8ff")) return "jpg";
  if (ascii.startsWith("GIF8")) return "gif";
  if (ascii.startsWith("BM")) return "bmp";
  if (ascii.startsWith("RIFF") && ascii.slice(8, 12) === "WAVE") return "wav";
  if (ascii.startsWith("RIFF") && ascii.slice(8, 12) === "AVI ") return "avi";
  if (ascii.startsWith("RIFF") && ascii.slice(8, 12) === "WEBP") return "webp";
  if (ascii.startsWith("OggS")) return "ogg";
  if (ascii.startsWith("ID3") || hex.startsWith("fffb")) return "mp3";
  if (ascii.startsWith("fLaC")) return "flac";
  if (ascii.startsWith("MThd")) return "mid";
  if (hex === "504b0304") return "zip";
  if (ascii.startsWith("MZ")) return "exe";
  if (hex === "00000100") return "ico";
  if (ascii.startsWith("OTTO")) return "otf";
  if (hex === "00010000") return "ttf";
  if (ascii.startsWith("wOFF")) return "woff";
  if (ascii.startsWith("wOF2")) return "woff2";
  if (ascii.startsWith("glTF")) return "glb";
  if (ascii.startsWith("DDS ")) return "dds";
  const text = b.subarray(0, 512).toString("utf8").replace(/^\ufeff/, "").trimStart();
  if (/^<\?xml|^<assembly/i.test(text)) return "xml";
  if (/^<!doctype html|^<html/i.test(text)) return "html";
  if (/^<svg/i.test(text)) return "svg";
  if (/^[{[]/.test(text)) return "json";
  if (b.length && !b.subarray(0, 4096).includes(0)) return "txt";
  return "bin";
}

// VS_VERSIONINFO → "Key: value" lines (fixed file/product version + every StringFileInfo pair).
function versionText(b) {
  const out = [];
  const fixed = b.indexOf(Buffer.from([0xbd, 0x04, 0xef, 0xfe]));
  if (fixed >= 0 && fixed + 52 <= b.length) {
    const v = (hi, lo) => `${b.readUInt16LE(hi + 2)}.${b.readUInt16LE(hi)}.${b.readUInt16LE(lo + 2)}.${b.readUInt16LE(lo)}`;
    out.push(`FileVersion (fixed): ${v(fixed + 8, fixed + 12)}`, `ProductVersion (fixed): ${v(fixed + 16, fixed + 20)}`);
  }
  // Walk the node tree; String nodes (wType 1) carry key + UTF-16 value.
  const walk = (start, end, depth) => {
    let p = start;
    while (p + 6 <= end && depth < 6) {
      const len = b.readUInt16LE(p), valueLen = b.readUInt16LE(p + 2), type = b.readUInt16LE(p + 4);
      if (len < 6 || p + len > end) break;
      let k = p + 6, key = "";
      while (k + 1 < p + len && b.readUInt16LE(k)) { key += String.fromCharCode(b.readUInt16LE(k)); k += 2; }
      k = (k + 2 + 3) & ~3;
      if (type === 1 && valueLen && depth >= 3) {
        // Values end at the first NUL (some build tools write malformed lengths).
        const value = b.subarray(k, Math.min(k + valueLen * 2, p + len)).toString("utf16le").split("\0")[0].replace(/[\x00-\x1f]/g, "");
        if (value) out.push(`${key}: ${value}`);
      }
      const children = type === 1 ? k + valueLen * 2 : k + valueLen;
      walk((children + 3) & ~3, p + len, depth + 1);
      p = (p + len + 3) & ~3;
    }
  };
  walk(0, b.length, 0);
  return out.join("\n") + "\n";
}

// BITMAPINFO resource → a .bmp file (adds the 14-byte file header).
function bitmapFile(b) {
  if (b.length < 16) return b;
  const header = b.readUInt32LE(0);
  const bits = header >= 16 ? b.readUInt16LE(14) : 0;
  const compression = header >= 20 ? b.readUInt32LE(16) : 0;
  const used = header >= 36 ? b.readUInt32LE(32) : 0;
  const palette = (used || (bits && bits <= 8 ? 1 << bits : 0)) * 4 + (header === 40 && compression === 3 ? 12 : 0);
  const file = Buffer.alloc(14);
  file.write("BM", 0, "latin1");
  file.writeUInt32LE(14 + b.length, 2);
  file.writeUInt32LE(14 + header + palette, 10);
  return Buffer.concat([file, b]);
}

// GRPICONDIR (or GRPCURSORDIR) + the referenced images → one .ico/.cur file.
function groupFile(group, images, cursor) {
  const count = group.length >= 6 ? group.readUInt16LE(4) : 0;
  const entries = [];
  for (let i = 0; i < count && 6 + i * 14 + 14 <= group.length; i++) {
    const e = group.subarray(6 + i * 14, 6 + i * 14 + 14);
    const data = images.get(e.readUInt16LE(12));
    if (data) entries.push({ e, data: cursor ? data.subarray(4) : data, hotspot: cursor ? data.subarray(0, 4) : null });
  }
  if (!entries.length) return null;
  const head = Buffer.alloc(6 + entries.length * 16);
  head.writeUInt16LE(cursor ? 2 : 1, 2);
  head.writeUInt16LE(entries.length, 4);
  let offset = head.length;
  entries.forEach(({ e, data, hotspot }, i) => {
    const o = 6 + i * 16;
    e.copy(head, o, 0, 4);                       // width, height, colours, reserved
    if (hotspot) hotspot.copy(head, o + 4);      // cursor hotspot x, y
    else e.copy(head, o + 4, 4, 8);              // planes, bit count
    head.writeUInt32LE(data.length, o + 8);
    head.writeUInt32LE(offset, o + 12);
    offset += data.length;
  });
  return Buffer.concat([head, ...entries.map((x) => x.data)]);
}

// Read the resource tree of a PE file. Returns [{type, typeName, name, lang, data}] or throws.
export async function readResources(file) {
  const fh = await fsp.open(file, "r");
  try {
    const { size } = await fh.stat();
    const read = async (pos, len) => {
      if (pos < 0 || len < 0 || pos + len > size) throw new Error("Truncated PE file");
      const b = Buffer.alloc(len);
      await fh.read(b, 0, len, pos);
      return b;
    };
    const dos = await read(0, 64);
    if (dos.toString("latin1", 0, 2) !== "MZ") throw new Error("Not a PE file (no MZ header)");
    const pe = dos.readUInt32LE(0x3c);
    const coff = await read(pe, 24);
    if (coff.toString("latin1", 0, 4) !== "PE\0\0") throw new Error("Not a PE file (no PE signature)");
    const sections = coff.readUInt16LE(6), optSize = coff.readUInt16LE(20);
    const opt = await read(pe + 24, optSize);
    const dirs = opt.readUInt16LE(0) === 0x20b ? 112 : 96;
    if (dirs + 24 > optSize) return [];
    const resRva = opt.readUInt32LE(dirs + 16), resSize = opt.readUInt32LE(dirs + 20);
    if (!resRva || !resSize) return [];
    const table = await read(pe + 24 + optSize, sections * 40);
    const secs = [];
    for (let i = 0; i < sections; i++) {
      const s = table.subarray(i * 40);
      secs.push({ va: s.readUInt32LE(12), vsize: s.readUInt32LE(8), raw: s.readUInt32LE(20), rawSize: s.readUInt32LE(16) });
    }
    const toOffset = (rva) => {
      // VirtualSize is what the loader maps (Godot's embedded "pck" section has a tiny one and a huge raw size).
      const s = secs.find((x) => rva >= x.va && rva < x.va + (x.vsize || x.rawSize));
      return s ? rva - s.va + s.raw : -1;
    };
    const base = toOffset(resRva);
    if (base < 0) throw new Error("Resource directory outside every section");
    const res = await read(base, Math.min(resSize, size - base, 64 * 1024 * 1024));
    const nameAt = (off) => {
      if (off + 2 > res.length) return "?";
      const len = res.readUInt16LE(off);
      return res.subarray(off + 2, Math.min(off + 2 + len * 2, res.length)).toString("utf16le");
    };
    const entries = (off) => {
      if (off + 16 > res.length) return [];
      const n = res.readUInt16LE(off + 12) + res.readUInt16LE(off + 14);
      const list = [];
      for (let i = 0; i < n && off + 16 + i * 8 + 8 <= res.length; i++) {
        const name = res.readUInt32LE(off + 16 + i * 8), to = res.readUInt32LE(off + 20 + i * 8);
        list.push({ id: name & 0x80000000 ? nameAt(name & 0x7fffffff) : name, dir: Boolean(to & 0x80000000), to: to & 0x7fffffff });
      }
      return list;
    };
    const found = [];
    let total = 0;
    for (const t of entries(0).filter((x) => x.dir)) {
      for (const n of entries(t.to).filter((x) => x.dir)) {
        for (const l of entries(n.to).filter((x) => !x.dir)) {
          if (found.length >= MAX_ENTRIES || l.to + 16 > res.length) continue;
          const rva = res.readUInt32LE(l.to), len = res.readUInt32LE(l.to + 4);
          const at = toOffset(rva);
          if (at < 0 || len > MAX_ENTRY || total + len > MAX_TOTAL) continue;
          total += len;
          const data = at >= base && at + len <= base + res.length ? res.subarray(at - base, at - base + len) : await read(at, len).catch(() => null);
          if (data) found.push({ type: t.id, typeName: typeof t.id === "number" ? TYPES[t.id] || `Type ${t.id}` : t.id, name: n.id, lang: l.id, data });
        }
      }
    }
    return found;
  } finally { await fh.close(); }
}

// Extract every resource of file into dir. Returns { files, counts }.
export async function extractResources(file, dir) {
  const all = await readResources(file);
  await fsp.rm(dir, { recursive: true, force: true });
  await fsp.mkdir(dir, { recursive: true });
  const used = new Set();
  const counts = {};
  const write = async (folder, base, ext, data) => {
    let name = `${safe(base)}.${ext}`;
    for (let i = 2; used.has(`${folder}/${name}`); i++) name = `${safe(base)}_${i}.${ext}`;
    used.add(`${folder}/${name}`);
    await fsp.mkdir(path.join(dir, safe(folder)), { recursive: true });
    await fsp.writeFile(path.join(dir, safe(folder), name), data);
    counts[folder] = (counts[folder] || 0) + 1;
  };
  const langs = new Map();
  for (const r of all) langs.set(`${r.type}/${r.name}`, (langs.get(`${r.type}/${r.name}`) || 0) + 1);
  const label = (r) => `${r.name}${langs.get(`${r.type}/${r.name}`) > 1 ? `_${r.lang}` : ""}`;
  const byId = (type) => new Map(all.filter((r) => r.type === type && typeof r.name === "number").map((r) => [r.name, r.data]));
  const icons = byId(3), cursors = byId(1);
  const grouped = new Set();
  const strings = [];
  for (const r of all) {
    if (r.type === 14 || r.type === 12) {
      const data = groupFile(r.data, r.type === 14 ? icons : cursors, r.type === 12);
      if (data) {
        await write(r.typeName, label(r), r.type === 14 ? "ico" : "cur", data);
        const count = r.data.readUInt16LE(4);
        for (let i = 0; i < count && 6 + i * 14 + 14 <= r.data.length; i++) grouped.add(`${r.type === 14 ? 3 : 1}/${r.data.readUInt16LE(6 + i * 14 + 12)}`);
      }
    }
  }
  for (const r of all) {
    if (r.type === 14 || r.type === 12) continue;
    if ((r.type === 3 || r.type === 1) && grouped.has(`${r.type}/${r.name}`)) continue;
    if (r.type === 3) await write(r.typeName, `icon_${label(r)}`, r.data.subarray(0, 4).toString("hex") === "89504e47" ? "png" : "dib", r.data);
    else if (r.type === 1) await write(r.typeName, `cursor_${label(r)}`, "bin", r.data);
    else if (r.type === 2) await write(r.typeName, label(r), "bmp", bitmapFile(r.data));
    else if (r.type === 16) await write(r.typeName, label(r), "txt", versionText(r.data));
    else if (r.type === 24) await write(r.typeName, label(r), "xml", r.data);
    else if (r.type === 23) await write(r.typeName, label(r), "html", r.data);
    else if (r.type === 6 && typeof r.name === "number") {
      for (let p = 0, i = 0; i < 16 && p + 2 <= r.data.length; i++) {
        const len = r.data.readUInt16LE(p);
        if (len) strings.push([(r.name - 1) * 16 + i, r.lang, r.data.subarray(p + 2, p + 2 + len * 2).toString("utf16le")]);
        p += 2 + len * 2;
      }
    } else await write(r.typeName, label(r), sniffExt(r.data), r.data);
  }
  if (strings.length) {
    // id<TAB>text, with a language column (LCID) when the table exists in several languages.
    const multi = new Set(strings.map((x) => x[1])).size > 1;
    strings.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    await write("Strings", "string_table", "txt", strings.map(([id, lang, s]) => `${id}\t${multi ? `${lang}\t` : ""}${s.replace(/\r?\n/g, "\\n")}`).join("\n") + "\n");
  }
  return { files: Object.values(counts).reduce((a, b) => a + b, 0), counts, resources: all.length };
}
