// <music-player>: audio player with a live visualizer and a lyrics tab.
// Attributes: src, data-lyrics, data-duration, data-title, data-sync-id, data-cover, data-cover-state, data-cover-stage, compact.
// Lyrics follow Whisper's word timings once a song has them (data-timing="ready",
// fetched from /api/jobs/{id}/lyric-timing); until then, an estimate from line length.

const ICONS = {
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M8 5.5v13l11-6.5z"/></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>',
  volume: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M15 9a4 4 0 0 1 0 6M17.5 6.5a8 8 0 0 1 0 11"/></svg>',
  muted: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="m15.5 9.5 5 5m0-5-5 5"/></svg>',
};
const VIZ_MODES = ["bars", "wave", "radial"];
const VIZ_LABELS = { bars: "Bars", wave: "Wave", radial: "Radial" };
const VIZ_DEFAULT_KEY = "music.vizMode";

// The default visualizer, chosen in the Settings tab. Each player's own button
// only changes that player until the page reloads.
function defaultViz() {
  const mode = readSetting(VIZ_DEFAULT_KEY, "bars");
  return VIZ_MODES.includes(mode) ? mode : "bars";
}
function setDefaultViz(mode) {
  if (!VIZ_MODES.includes(mode)) return;
  saveSetting(VIZ_DEFAULT_KEY, mode);
  document.querySelectorAll("music-player").forEach((player) => player.setVizMode?.(mode));
}
// Settings tab: the visualizer takes its colors from the song's cover (when it has one).
const VIZ_COVER_KEY = "music.vizCoverColors";
const coverColorsOn = () => readSetting(VIZ_COVER_KEY, "off") === "on";
function setCoverColors(on) {
  saveSetting(VIZ_COVER_KEY, on ? "on" : "off");
  document.querySelectorAll("music-player").forEach((player) => player.updateCoverColors?.());
}

const RGB = (r, g, b) => [r / 255, g / 255, b / 255];
function toHsl([r, g, b]) {
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
  if (!d) return [0, 0, l];
  const s = d / (1 - Math.abs(2 * l - 1));
  const h = max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h * 60, s, l];
}
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const hsl = ([h, s, l]) => `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;

// The cover's most prominent vivid color (the accent) and a second, different one, brightened or
// darkened to read on the dark player. Null for a greyscale cover or one that can't be read.
const palettes = new Map();   // cover url -> Promise<{accent, faint} | null>
function coverPalette(url) {
  if (!palettes.has(url)) palettes.set(url, new Promise((resolve) => {
    const image = new Image();
    image.decoding = "async";
    image.onerror = () => resolve(null);
    image.onload = () => {
      try {
        const size = 48;
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = size;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(image, 0, 0, size, size);
        const data = context.getImageData(0, 0, size, size).data;
        const BINS = 24;   // 15° of hue each
        const bins = Array.from({ length: BINS }, () => ({ weight: 0, h: 0, s: 0, l: 0 }));
        for (let i = 0; i < data.length; i += 4) {
          const [h, s, l] = toHsl(RGB(data[i], data[i + 1], data[i + 2]));
          if (s < 0.18 || l < 0.1 || l > 0.93) continue;     // greys, near black and white
          const weight = s * s * (1 - Math.abs(2 * l - 1));   // vivid, mid-bright pixels count most
          const bin = bins[Math.floor(h / (360 / BINS)) % BINS];
          bin.weight += weight; bin.h += h * weight; bin.s += s * weight; bin.l += l * weight;
        }
        const ranked = bins.map((bin, index) => ({ ...bin, index })).filter((bin) => bin.weight > 0)
          .sort((a, b) => b.weight - a.weight);
        const total = ranked.reduce((sum, bin) => sum + bin.weight, 0);
        if (!ranked.length || total < size * size * 0.01) return resolve(null);
        const color = (bin) => [bin.h / bin.weight, bin.s / bin.weight, bin.l / bin.weight];
        const top = ranked[0];
        const apart = (bin) => Math.min(Math.abs(bin.index - top.index), BINS - Math.abs(bin.index - top.index)) >= 3;
        const second = ranked.find((bin) => apart(bin) && bin.weight >= top.weight * 0.15);
        const [h, s] = color(top);
        const [h2, s2] = second ? color(second) : [h, s];
        resolve({
          accent: hsl([h, clamp(s, 0.45, 0.9), 0.66]),
          faint: hsl([h2, clamp(s2 * 0.85, 0.3, 0.75), second ? 0.42 : 0.3]),
        });
      } catch {
        resolve(null);   // a canvas that can't be read
      }
    };
    image.src = url;
  }));
  return palettes.get(url);
}

window.MusicViz = { getDefault: defaultViz, setDefault: setDefaultViz, getCoverColors: coverColorsOn, setCoverColors };
window.MusicTiming = {
  enabled: true,
  setEnabled(on) {
    this.enabled = on;
    document.querySelectorAll("music-player").forEach((player) => player.showTiming?.());
  },
};
const API_BASE_PATH = document.body?.dataset.appPrefix || "";
const INSTRUMENTAL_TAG = /intro|outro|instrumental|solo|break|drop|interlude|hook riff/i;

function readSetting(key, fallback) {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function saveSetting(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage blocked */ }
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function countSyllables(text) {
  return text.toLowerCase().split(/\s+/).filter(Boolean).reduce((total, word) => {
    const groups = word.replace(/[^\p{L}\p{N}]/gu, "").match(/[aeiouyà-ÿ]+|[^\x00-\x7f]/g);
    return total + Math.max(1, groups?.length || 1);
  }, 0);
}

// Returns rows of { text, isTag, weight }. A sung line takes time per syllable,
// a section tag only a beat, and an instrumental tag a few bars.
export function parseLyrics(raw) {
  const rows = [];
  for (const source of String(raw || "").split(/\r?\n/)) {
    let line = source.trim();
    if (!line) continue;
    const tag = line.match(/^\[([^\]]*)\]\s*(.*)$/);
    if (tag) {
      rows.push({ text: tag[1].trim(), isTag: true, weight: INSTRUMENTAL_TAG.test(tag[1]) ? 16 : 2 });
      line = tag[2];
      if (!line) continue;
    }
    rows.push({ text: line, isTag: false, weight: countSyllables(line) + 2 });
  }
  return rows;
}

// Adds start/end (seconds) to each row, spread over the track length.
export function timeLyrics(rows, duration) {
  const total = rows.reduce((sum, row) => sum + row.weight, 0) || 1;
  let cursor = 0;
  for (const row of rows) {
    row.start = (cursor / total) * duration;
    cursor += row.weight;
    row.end = (cursor / total) * duration;
  }
  return rows;
}

export function activeRow(rows, time) {
  if (!rows.length || time < rows[0].start) return -1;
  for (let index = 0; index < rows.length; index += 1) {
    if (time < rows[index].end) return index;
  }
  return rows.length - 1;
}

/* One AudioContext for the page; each player adds its own analyser. */
let sharedContext = null;
function audioContext() {
  if (!sharedContext) sharedContext = new (window.AudioContext || window.webkitAudioContext)();
  return sharedContext;
}

const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// The spans of each whitespace-separated word, inside the syntax colors: "love(oh)" is one word in two
// colors, so two spans. Null if they don't line up with the timings.
function wrapWords(node, count) {
  const texts = [];
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) texts.push(walker.currentNode);
  const words = [];
  let inWord = false;
  for (const text of texts) {
    const pieces = document.createDocumentFragment();
    for (const piece of text.nodeValue.split(/(\s+)/)) {
      if (!piece) continue;
      if (/^\s+$/.test(piece)) { pieces.append(piece); inWord = false; continue; }
      const span = document.createElement("span");
      span.className = "mp-word";
      span.textContent = piece;
      if (inWord) words.at(-1).push(span);
      else words.push([span]);
      inWord = true;
      pieces.append(span);
    }
    text.replaceWith(pieces);
  }
  return words.length === count ? words : null;
}

class MusicPlayer extends HTMLElement {
  static observedAttributes = ["data-timing", "data-cover", "data-cover-state", "data-cover-stage"];

  attributeChangedCallback(name) {
    if (!this.built) return;
    if (name !== "data-timing") return this.showArt();
    this.showTiming();
    const state = this.getAttribute("data-timing");
    if (state === "running") this.staleTimings = true;   // a redo: fetch the new ones when it's done
    if (state === "ready" && !this.panel?.hidden) this.loadTimings();
  }

  connectedCallback() {
    if (!this.built) this.build();
    else if (this.source) this.connectGraph();
  }

  disconnectedCallback() {
    // A card reordered by the job list is reconnected in the same task: keep playing.
    queueMicrotask(() => {
      if (this.isConnected) return;
      this.audio.pause();
      this.source?.disconnect();
      this.analyser?.disconnect();
    });
  }

  build() {
    this.built = true;
    const compact = this.hasAttribute("compact");
    this.vizMode = defaultViz();
    this.rows = parseLyrics(this.dataset.lyrics);
    this.hasSungLines = this.rows.some((row) => !row.isTag);
    this.syncKey = `music.lyricSync.${this.dataset.syncId || ""}`;
    this.syncOffset = Number(readSetting(this.syncKey, 0)) || 0;
    this.following = true;
    this.activeIndex = -2;
    this.level = 0;

    this.innerHTML = `
      <div class="mp-deck${compact ? " is-compact" : ""}">
        <div class="mp-stage">
          <button type="button" class="mp-art" hidden><img alt="" decoding="async"><span class="mp-art-working"><span class="cover-spinner" aria-hidden="true"></span><span class="mp-art-stage"></span></span></button>
          <canvas class="mp-canvas" aria-hidden="true"></canvas>
          <button type="button" class="mp-viz-mode" title="Change visualizer"></button>
        </div>
        <div class="mp-controls">
          <button type="button" class="mp-play" aria-label="Play" title="Play">${ICONS.play}</button>
          <span class="mp-time mp-current">0:00</span>
          <input type="range" class="mp-seek" min="0" max="1" step="1" value="0" aria-label="Seek">
          <span class="mp-time mp-total">0:00</span>
          <div class="mp-volume-group">
            <button type="button" class="mp-mute" aria-label="Mute" title="Mute">${ICONS.volume}</button>
            <input type="range" class="mp-volume" min="0" max="1" step="0.01" aria-label="Volume">
          </div>
        </div>
      </div>
      ${this.hasSungLines ? `
      <div class="mp-lyrics">
        <button type="button" class="mp-lyrics-tab" aria-expanded="false"><span>Lyrics</span><i class="mp-tab-chevron" aria-hidden="true"></i></button>
        <div class="mp-lyrics-panel" hidden>
          <div class="mp-lyrics-scroll" tabindex="0" role="group" aria-label="Lyrics"></div>
          <div class="mp-lyrics-bar">
            <span class="mp-sync" title="Timing is estimated from the lyrics. Shift it if the highlight runs early or late.">
              <button type="button" data-sync="-0.5" aria-label="Highlight earlier">−</button>
              <output class="mp-sync-value"></output>
              <button type="button" data-sync="0.5" aria-label="Highlight later">+</button>
            </span>
            <button type="button" class="mp-timing" hidden></button>
            <button type="button" class="mp-follow" hidden>Follow</button>
          </div>
        </div>
      </div>` : ""}`;

    this.audio = document.createElement("audio");
    this.audio.preload = compact ? "none" : "metadata";
    this.audio.src = this.getAttribute("src");
    this.audio.hidden = true;
    this.append(this.audio);

    this.canvas = this.querySelector(".mp-canvas");
    this.playButton = this.querySelector(".mp-play");
    this.seek = this.querySelector(".mp-seek");
    this.volume = this.querySelector(".mp-volume");
    this.muteButton = this.querySelector(".mp-mute");
    this.modeButton = this.querySelector(".mp-viz-mode");
    this.modeButton.textContent = VIZ_LABELS[this.vizMode];
    this.art = this.querySelector(".mp-art");
    this.showArt();

    this.bindPlayback();
    this.bindLyrics();
    this.setDuration(Number(this.dataset.duration) || 0);
    new ResizeObserver(() => this.sizeCanvas()).observe(this.canvas);
    this.sizeCanvas();
  }

  /* The song cover sits at the left of the visualizer; a shimmer stands in while it's being made. */
  showArt() {
    const cover = this.dataset.cover || "";
    const making = this.dataset.coverState === "running";
    const image = this.art.querySelector("img");
    this.art.hidden = !cover && !making;
    this.art.classList.toggle("is-making", making);
    this.art.disabled = !cover;
    const stage = `${this.dataset.coverStage || "Making the cover"}…`;
    this.art.querySelector(".mp-art-stage").textContent = stage;
    this.art.title = making ? `Song cover: ${stage}` : "View the song cover";
    this.art.setAttribute("aria-label", this.art.title);
    if (cover && image.getAttribute("src") !== cover) image.src = cover;
    image.hidden = !cover;
    if (cover !== this.paletteFor) this.updateCoverColors();
    this.art.closest(".mp-stage").classList.toggle("has-art", !this.art.hidden);
    if (this.audio && !this.audio.paused) this.updateMediaSession();
  }

  /* ---------------------------------------------------- playback -- */

  bindPlayback() {
    const { audio, seek, volume } = this;
    this.playButton.addEventListener("click", () => {
      if (audio.paused) this.play();
      else audio.pause();
    });
    audio.addEventListener("play", () => {
      document.querySelectorAll("music-player").forEach((other) => { if (other !== this) other.audio?.pause(); });
      this.setPlayIcon(true);
      this.updateMediaSession();
      this.startDrawing();
      this.startFollowing();
    });
    audio.addEventListener("pause", () => this.setPlayIcon(false));
    audio.addEventListener("ended", () => this.setPlayIcon(false));
    audio.addEventListener("loadedmetadata", () => { if (Number.isFinite(audio.duration)) this.setDuration(audio.duration); });
    audio.addEventListener("durationchange", () => { if (Number.isFinite(audio.duration)) this.setDuration(audio.duration); });
    audio.addEventListener("timeupdate", () => this.syncTime());
    audio.addEventListener("seeked", () => this.syncTime());
    audio.addEventListener("error", () => {
      this.querySelector(".mp-current").textContent = "Error";
      this.setPlayIcon(false);
    });

    seek.addEventListener("input", () => {
      this.seeking = true;
      this.paintRange(seek);
      this.querySelector(".mp-current").textContent = formatTime(Number(seek.value));
    });
    seek.addEventListener("change", () => {
      audio.currentTime = Number(seek.value);
      this.seeking = false;
    });

    audio.volume = Math.min(1, Math.max(0, Number(readSetting("music.volume", 1))));
    volume.value = audio.volume;
    this.paintRange(volume);
    volume.addEventListener("input", () => {
      audio.volume = Number(volume.value);
      audio.muted = false;
      saveSetting("music.volume", volume.value);
      this.paintRange(volume);
      this.setMuteIcon();
    });
    this.muteButton.addEventListener("click", () => {
      audio.muted = !audio.muted;
      this.setMuteIcon();
    });

    this.modeButton.addEventListener("click", () => {
      this.setVizMode(VIZ_MODES[(VIZ_MODES.indexOf(this.vizMode) + 1) % VIZ_MODES.length]);
    });
  }

  setVizMode(mode) {
    this.vizMode = mode;
    this.modeButton.textContent = VIZ_LABELS[mode];
    this.draw();
  }

  play() {
    this.connectGraph();
    if (sharedContext?.state === "suspended") sharedContext.resume().catch(() => {});
    this.audio.play().catch((error) => {
      if (error.name !== "AbortError") this.querySelector(".mp-current").textContent = "Error";
    });
  }

  // Built on the first Play click (a user gesture). If Web Audio fails the
  // track still plays through the element, just without the visualizer.
  connectGraph() {
    try {
      const context = audioContext();
      if (!this.source) {
        this.source = context.createMediaElementSource(this.audio);
        this.analyser = context.createAnalyser();
        this.analyser.fftSize = 2048;
        this.analyser.smoothingTimeConstant = 0.78;
        this.frequency = new Uint8Array(this.analyser.frequencyBinCount);
        this.waveform = new Uint8Array(this.analyser.fftSize);
      }
      this.source.connect(this.analyser);
      this.analyser.connect(context.destination);
    } catch {
      this.analyser = null;
    }
  }

  setDuration(duration) {
    if (duration <= 0) return;
    this.duration = duration;
    this.seek.max = String(Math.max(1, duration));
    this.querySelector(".mp-total").textContent = formatTime(duration);
    this.paintRange(this.seek);
    if (this.hasSungLines) {
      if (!this.timed) timeLyrics(this.rows, duration);
      else this.rows.at(-1).end = Math.max(this.rows.at(-1).end, duration);
      this.renderHighlight();
    }
  }

  syncTime() {
    const time = this.audio.currentTime;
    if (!this.seeking) {
      this.seek.value = String(time);
      this.paintRange(this.seek);
      this.querySelector(".mp-current").textContent = formatTime(time);
    }
    this.renderHighlight();
  }

  paintRange(range) {
    const max = Number(range.max) || 1;
    range.style.setProperty("--mp-fill", `${Math.min(100, (Number(range.value) / max) * 100)}%`);
  }

  setPlayIcon(playing) {
    this.playButton.innerHTML = playing ? ICONS.pause : ICONS.play;
    const label = playing ? "Pause" : "Play";
    this.playButton.setAttribute("aria-label", label);
    this.playButton.title = label;
    this.classList.toggle("is-playing", playing);
  }

  setMuteIcon() {
    const silent = this.audio.muted || this.audio.volume === 0;
    this.muteButton.innerHTML = silent ? ICONS.muted : ICONS.volume;
    const label = this.audio.muted ? "Unmute" : "Mute";
    this.muteButton.setAttribute("aria-label", label);
    this.muteButton.title = label;
  }

  updateMediaSession() {
    if (!("mediaSession" in navigator) || !window.MediaMetadata) return;
    const artwork = this.dataset.cover ? [{ src: new URL(this.dataset.cover, location.href).href, sizes: "1024x1024", type: "image/png" }] : [];
    navigator.mediaSession.metadata = new MediaMetadata({ title: (this.dataset.title || "Music").slice(0, 120), artist: "Rocsy Music", artwork });
    navigator.mediaSession.setActionHandler("play", () => this.play());
    navigator.mediaSession.setActionHandler("pause", () => this.audio.pause());
  }

  /* ------------------------------------------------------ lyrics -- */

  bindLyrics() {
    if (!this.hasSungLines) return;
    this.tab = this.querySelector(".mp-lyrics-tab");
    this.panel = this.querySelector(".mp-lyrics-panel");
    this.scroller = this.querySelector(".mp-lyrics-scroll");
    this.followButton = this.querySelector(".mp-follow");

    this.lineNodes = this.rows.map((row, index) => {
      const node = document.createElement("button");
      node.type = "button";
      node.className = row.isTag ? `mp-line is-tag ly-${window.MusicLyrics.tagKind(row.text)}` : "mp-line";
      if (row.isTag) node.textContent = row.text;
      else node.innerHTML = window.MusicLyrics.highlight(row.text);
      node.addEventListener("click", () => {
        if (row.start === undefined) return;
        this.audio.currentTime = Math.max(0, row.start + this.syncOffset);
        this.setFollowing(true);
        this.activeIndex = -2;
        if (this.audio.paused) this.play();
        this.highlight(index);
      });
      return node;
    });
    this.scroller.replaceChildren(...this.lineNodes);

    this.tab.addEventListener("click", () => {
      const open = this.panel.hidden;
      this.panel.hidden = !open;
      this.tab.setAttribute("aria-expanded", String(open));
      this.querySelector(".mp-lyrics").classList.toggle("is-open", open);
      if (open) {
        this.setFollowing(true);
        this.scrollToActive(false);
        this.loadTimings();
        this.startFollowing();
      }
    });

    // Only real user input pauses following, so its own auto-scroll never does.
    const release = () => this.setFollowing(false);
    this.scroller.addEventListener("wheel", release, { passive: true });
    this.scroller.addEventListener("touchmove", release, { passive: true });
    this.scroller.addEventListener("pointerdown", (event) => { if (event.target === this.scroller) release(); });
    this.scroller.addEventListener("keydown", (event) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) release();
    });
    this.followButton.addEventListener("click", () => {
      this.setFollowing(true);
      this.scrollToActive(true);
    });

    this.timingButton = this.querySelector(".mp-timing");
    this.timingButton.addEventListener("click", () => this.requestTimings());
    this.showTiming();
    this.showSync();
    this.querySelectorAll("[data-sync]").forEach((button) => button.addEventListener("click", () => {
      this.syncOffset = Math.round((this.syncOffset + Number(button.dataset.sync)) * 10) / 10;
      saveSetting(this.syncKey, this.syncOffset);
      this.showSync();
      this.renderHighlight();
    }));
  }

  /* Word timings: one job per song, run by the server after the render or from the button here. */
  timingUrl() {
    return `${API_BASE_PATH}/api/jobs/${this.dataset.syncId}/lyric-timing`;
  }

  showTiming() {
    const button = this.timingButton;
    if (!button) return;
    const state = this.requesting ? "running" : this.getAttribute("data-timing") || "";
    button.hidden = !this.dataset.syncId || (!window.MusicTiming.enabled && state !== "running");
    button.disabled = state === "running";
    button.textContent = state === "running" ? "Finding word timings…" : state === "failed" ? "Couldn't time it: try again" : state === "ready" ? "Redo word timings" : "Find word timings";
    this.querySelector(".mp-sync").title = state === "ready"
      ? `Timing comes from Whisper${this.timingModel ? ` ${this.timingModel}` : ""}. Shift it if the highlight runs early or late.`
      : "Timing is estimated from the lyrics. Shift it if the highlight runs early or late.";
  }

  async requestTimings() {
    this.requesting = true;
    this.showTiming();
    try {
      const response = await fetch(this.timingUrl(), { method: "POST" });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).message || "Couldn't queue the timing.");
      this.setAttribute("data-timing", "running");
    } catch (error) {
      this.setAttribute("data-timing", "failed");
      this.timingButton.title = error.message;
    } finally {
      this.requesting = false;
      this.showTiming();
    }
  }

  async loadTimings() {
    if ((this.timed && !this.staleTimings) || this.loadingTimings || this.getAttribute("data-timing") !== "ready") return;
    this.loadingTimings = true;
    try {
      const response = await fetch(this.timingUrl());
      if (response.ok) {
        const timings = await response.json();
        this.timingModel = timings.model;
        this.applyTimings(timings.lines);
        this.staleTimings = false;
        this.showTiming();
      }
    } catch { /* keeps the estimate */ } finally {
      this.loadingTimings = false;
    }
  }

  applyTimings(lines) {
    const sung = this.rows.filter((row) => !row.isTag);
    if (!Array.isArray(lines) || lines.length !== sung.length || lines.some((line) => !line.length)) return;
    sung.forEach((row, index) => {
      row.words = lines[index];
      row.start = row.words[0][0];
    });
    // A line stays lit until the next one starts; a tag is lit over the gap before its line.
    let previousEnd = 0;
    this.rows.forEach((row, index) => {
      const next = this.rows.slice(index + 1).find((other) => !other.isTag);
      if (row.isTag) {
        row.start = previousEnd;
        row.end = next ? next.start : Math.max(previousEnd, this.duration || 0);
      } else {
        row.end = next ? next.start : Math.max(row.words.at(-1)[1], this.duration || 0);
        previousEnd = row.words.at(-1)[1];
      }
    });
    // Consecutive tags share the gap, so none is left spanning past the next row.
    this.rows.forEach((row, index) => { if (row.isTag && this.rows[index + 1]) row.end = this.rows[index + 1].isTag ? row.start : row.end; });
    this.rows.forEach((row, index) => { if (!row.isTag) row.wordNodes ||= wrapWords(this.lineNodes[index], row.words.length); });
    this.timed = true;
    this.activeIndex = -2;
    this.renderHighlight();
  }

  // Timeupdate fires about 4 times a second, too slow to follow single words.
  startFollowing() {
    if (this.followFrame || !this.panel || this.panel.hidden || this.audio.paused) return;
    const tick = () => {
      this.followFrame = 0;
      if (this.panel.hidden || this.audio.paused || !this.isConnected) return;
      this.renderHighlight();
      this.followFrame = requestAnimationFrame(tick);
    };
    this.followFrame = requestAnimationFrame(tick);
  }

  renderWords() {
    const time = this.audio.currentTime - this.syncOffset;
    const nodes = this.rows[this.activeIndex]?.wordNodes;
    if (this.wordLine && this.wordLine !== nodes) {
      this.wordLine.flat().forEach((node) => node.classList.remove("is-sung", "is-current"));
    }
    this.wordLine = nodes || null;
    if (!nodes) return;
    const words = this.rows[this.activeIndex].words;
    nodes.forEach((spans, index) => spans.forEach((node) => {
      node.classList.toggle("is-sung", time >= words[index][0]);
      node.classList.toggle("is-current", time >= words[index][0] && time < words[index][1]);
    }));
  }

  showSync() {
    const value = this.syncOffset;
    this.querySelector(".mp-sync-value").textContent = `Sync ${value > 0 ? "+" : ""}${value.toFixed(1)}s`;
  }

  setFollowing(following) {
    this.following = following;
    if (this.followButton) this.followButton.hidden = following;
  }

  renderHighlight() {
    if (!this.hasSungLines || this.rows[0].start === undefined) return;
    const started = this.audio.currentTime > 0 || !this.audio.paused;
    this.highlight(started ? activeRow(this.rows, this.audio.currentTime - this.syncOffset) : -1);
    if (this.timed) this.renderWords();
  }

  highlight(index) {
    if (index === this.activeIndex) return;
    this.activeIndex = index;
    this.lineNodes.forEach((node, position) => {
      node.classList.toggle("is-active", position === index);
      node.classList.toggle("is-past", index >= 0 && position < index);
      if (position === index) node.setAttribute("aria-current", "true");
      else node.removeAttribute("aria-current");
    });
    if (this.following) this.scrollToActive(true);
  }

  // Scrolls the lyrics box only, never the page behind it.
  scrollToActive(smooth) {
    if (this.panel.hidden) return;
    const node = this.lineNodes[this.activeIndex];
    if (!node) return;
    const top = node.offsetTop - (this.scroller.clientHeight - node.offsetHeight) / 2;
    this.scroller.scrollTo({ top, behavior: smooth && !reducedMotion() ? "smooth" : "auto" });
  }

  /* -------------------------------------------------- visualizer -- */

  sizeCanvas() {
    const box = this.canvas.getBoundingClientRect();
    if (!box.width) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(box.width * ratio);
    this.canvas.height = Math.round(box.height * ratio);
    this.width = box.width;
    this.height = box.height;
    this.ratio = ratio;
    this.draw();
  }

  startDrawing() {
    this.colors = this.coverColors || this.readColors();
    if (this.frame || reducedMotion()) return;
    const tick = () => {
      this.draw();
      // Runs on after a pause only until the levels have fallen back.
      this.frame = (!this.audio.paused || this.level > 0.004) && this.isConnected ? requestAnimationFrame(tick) : 0;
    };
    this.frame = requestAnimationFrame(tick);
  }

  readColors() {
    const style = getComputedStyle(this);
    return { accent: style.getPropertyValue("--blue").trim() || "#7cb3dc", faint: style.getPropertyValue("--border-strong").trim() || "#354550" };
  }

  // Settings → "Visualizer colors from the song cover": the cover's palette, else the theme's.
  async updateCoverColors() {
    const cover = coverColorsOn() ? this.dataset.cover || "" : "";
    this.paletteFor = this.dataset.cover || "";
    const palette = cover ? await coverPalette(new URL(cover, location.href).href) : null;
    if (cover !== (coverColorsOn() ? this.dataset.cover || "" : "")) return;   // changed meanwhile
    this.coverColors = palette ? { ...palette, fromCover: true } : null;
    this.colors = this.coverColors || this.readColors();
    this.draw?.();
  }

  // A cover palette blends its two colors in every mode; the theme keeps its single accent line.
  strokeFor(context, kind) {
    const { width, height, colors } = this;
    if (!colors.fromCover) return colors.accent;
    if (kind === "radial") {
      const reach = Math.min(width, height) / 2;
      const gradient = context.createRadialGradient(width / 2, height / 2, reach * 0.2, width / 2, height / 2, reach);
      gradient.addColorStop(0, colors.faint);
      gradient.addColorStop(1, colors.accent);
      return gradient;
    }
    const gradient = context.createLinearGradient(0, 0, width, 0);
    gradient.addColorStop(0, colors.faint);
    gradient.addColorStop(0.5, colors.accent);
    gradient.addColorStop(1, colors.faint);
    return gradient;
  }

  // Log-spaced bands, so bass does not fill the whole width.
  bands(count) {
    const values = new Array(count).fill(0);
    if (!this.analyser) return values;
    this.analyser.getByteFrequencyData(this.frequency);
    const usable = this.frequency.length * 0.72;
    for (let band = 0; band < count; band += 1) {
      const from = Math.floor(Math.pow(usable, band / count));
      const to = Math.max(from + 1, Math.floor(Math.pow(usable, (band + 1) / count)));
      let peak = 0;
      for (let bin = from; bin < to; bin += 1) peak = Math.max(peak, this.frequency[bin]);
      values[band] = peak / 255;
    }
    return values;
  }

  draw() {
    const context = this.canvas.getContext("2d");
    if (!context || !this.width) return;
    this.colors ||= this.readColors();
    context.setTransform(this.ratio, 0, 0, this.ratio, 0, 0);
    context.clearRect(0, 0, this.width, this.height);
    if (this.vizMode === "wave") this.drawWave(context);
    else if (this.vizMode === "radial") this.drawRadial(context);
    else this.drawBars(context);
  }

  drawBars(context) {
    const { width, height, colors } = this;
    const count = Math.max(16, Math.min(96, Math.floor(width / 6)));
    const values = this.bands(count);
    const pitch = width / count;
    const barWidth = Math.max(1.5, pitch - 2);
    const gradient = context.createLinearGradient(0, height, 0, 0);
    gradient.addColorStop(0, colors.faint);
    gradient.addColorStop(0.75, colors.accent);
    context.fillStyle = gradient;
    let sum = 0;
    values.forEach((value, index) => {
      sum += value;
      const barHeight = Math.max(2, value * value * (height - 4));
      context.beginPath();
      context.roundRect(index * pitch + (pitch - barWidth) / 2, height - barHeight, barWidth, barHeight, 1.5);
      context.fill();
    });
    this.level = sum / count;
  }

  drawWave(context) {
    const { width, height } = this;
    const middle = height / 2;
    let peak = 0;
    context.beginPath();
    if (this.analyser) {
      this.analyser.getByteTimeDomainData(this.waveform);
      const step = this.waveform.length / width;
      for (let x = 0; x <= width; x += 1) {
        const sample = (this.waveform[Math.floor(x * step)] ?? 128) / 128 - 1;
        peak = Math.max(peak, Math.abs(sample));
        const y = middle + sample * (middle - 3);
        if (x === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
    } else {
      context.moveTo(0, middle);
      context.lineTo(width, middle);
    }
    context.strokeStyle = this.strokeFor(context, "wave");
    context.lineWidth = 1.75;
    context.lineJoin = "round";
    context.stroke();
    this.level = peak;
  }

  drawRadial(context) {
    const { width, height } = this;
    const count = 72;
    const half = this.bands(count / 2);
    const values = [...half, ...[...half].reverse()];
    const centerX = width / 2, centerY = height / 2;
    const inner = Math.max(8, Math.min(width, height) * 0.2);
    const reach = Math.min(width, height) / 2 - inner - 2;
    let sum = 0;
    context.strokeStyle = this.strokeFor(context, "radial");
    context.lineWidth = 2;
    context.lineCap = "round";
    context.beginPath();
    values.forEach((value, index) => {
      sum += value;
      const angle = (index / count) * Math.PI * 2 - Math.PI / 2;
      const outer = inner + 1 + value * value * reach;
      context.moveTo(centerX + Math.cos(angle) * inner, centerY + Math.sin(angle) * inner);
      context.lineTo(centerX + Math.cos(angle) * outer, centerY + Math.sin(angle) * outer);
    });
    context.stroke();
    this.level = sum / count;
  }
}

if (!customElements.get("music-player")) customElements.define("music-player", MusicPlayer);
