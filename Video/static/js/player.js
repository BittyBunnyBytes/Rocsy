/* BunnyPlayer: the Trainer's own video player (copy of Trainer/static/player.js, via the Agent page's
   copy: the playhead animation only runs while playing). Video adds a playlist mode:
   BunnyPlayer.create(host, {playlist: [{src, poster, label, title}]}) starts paused on the first item,
   plays every item in order once started (loop = the whole list), and p.setPlaylist(list) updates it
   without interrupting the clip that's playing. opts.poster / opts.videoClass also added.
   Wraps a <video> (no native controls) with a control bar whose scrub bar can show:
   - trims ("segments") colour-coded per lane, the selected one highlighted;
   - auto-cut scouting ("scout"): shot boundaries, candidate windows (pending / scanning in the
     agent's colour / keep with a score bar / skip), picks with a gold outline, and agent badges.
   Usage: const p = BunnyPlayer.create(host, {src, fps, autoplay, loop, muted});
          p.video, p.setSegments([...]), p.setScout({...}) */
(() => {
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const fmt = (t) => {
    if (!isFinite(t)) return "0:00.00";
    const m = Math.floor(t / 60), s = t - m * 60;
    return `${m}:${s.toFixed(2).padStart(5, "0")}`;
  };
  const SPEEDS = [0.25, 0.5, 1, 1.5, 2];
  const ICON = {
    play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>',
    prev: '<svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zM18 6v12L9.5 12z"/></svg>',
    next: '<svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 6l8.5 6L6 18z"/></svg>',
    prevClip: '<svg viewBox="0 0 24 24"><path d="M5 5h2.5v14H5zM19.5 5v14L9 12z"/></svg>',
    nextClip: '<svg viewBox="0 0 24 24"><path d="M16.5 5H19v14h-2.5zM4.5 5 15 12 4.5 19z"/></svg>',
    loop: '<svg viewBox="0 0 24 24"><path d="M7 7h10v3l4-4-4-4v3H5v6h2zM17 17H7v-3l-4 4 4 4v-3h12v-6h-2z"/></svg>',
    vol: '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 5V4L8 9zM16.5 12A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4z"/></svg>',
    mute: '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 5V4L8 9zM16 9l5 6M21 9l-5 6" stroke="currentColor" stroke-width="2" fill="none"/></svg>',
    full: '<svg viewBox="0 0 24 24"><path d="M5 5h5v2H7v3H5zM14 5h5v5h-2V7h-3zM5 14h2v3h3v2H5zM17 14h2v5h-5v-2h3z"/></svg>',
  };

  function create(host, opts = {}) {
    const root = el("div", "bp");
    const stage = el("div", "bp-stage");
    const video = el("video");
    video.playsInline = true; video.preload = opts.preload || "auto";
    if (opts.muted) video.muted = true;
    if (opts.loop) video.loop = true;
    if (opts.autoplay) video.autoplay = true;
    if (opts.videoClass) video.className = opts.videoClass;
    if (opts.poster) video.poster = opts.poster;
    if (opts.src) video.src = opts.src;
    const bigPlay = el("button", "bp-big"); bigPlay.type = "button"; bigPlay.innerHTML = ICON.play;
    const clipBadge = el("span", "bp-clip"); clipBadge.hidden = true;
    stage.append(video, bigPlay, clipBadge);

    const bar = el("div", "bp-bar");
    // scrub track: layers from back to front
    const track = el("div", "bp-track");
    const buffered = el("div", "bp-buffered");
    const scoutLayer = el("div", "bp-scout");
    const segLayer = el("div", "bp-segs");
    const played = el("div", "bp-played");
    const head = el("div", "bp-head");
    const agentLayer = el("div", "bp-agents");
    const tip = el("div", "bp-tip");
    track.append(buffered, scoutLayer, segLayer, played, head, agentLayer, tip);

    const btn = (html, title) => { const b = el("button", "bp-btn"); b.type = "button"; b.innerHTML = html; b.title = title; return b; };
    const playBtn = btn(ICON.play, "Play / pause (Space)");
    const prevBtn = btn(ICON.prev, "Back one frame");
    const nextBtn = btn(ICON.next, "Forward one frame");
    const time = el("span", "bp-time", "0:00.00 / 0:00.00");
    const speedBtn = el("button", "bp-btn bp-speed", "1×"); speedBtn.type = "button"; speedBtn.title = "Playback speed";
    const loopBtn = btn(ICON.loop, "Loop");
    const volBtn = btn(ICON.vol, "Mute / unmute");
    const vol = el("input", "bp-vol"); vol.type = "range"; vol.min = "0"; vol.max = "1"; vol.step = "0.05"; vol.value = String(video.muted ? 0 : 1);
    const fullBtn = btn(ICON.full, "Fullscreen");
    const controls = el("div", "bp-controls");
    const spacer = el("span", "bp-spacer");
    const prevClipBtn = btn(ICON.prevClip, "Previous clip"), nextClipBtn = btn(ICON.nextClip, "Next clip");
    prevClipBtn.classList.add("bp-list"); nextClipBtn.classList.add("bp-list");
    prevBtn.classList.add("bp-step"); nextBtn.classList.add("bp-step");
    controls.append(prevClipBtn, playBtn, nextClipBtn, prevBtn, nextBtn, time, spacer, speedBtn, loopBtn, volBtn, vol, fullBtn);
    bar.append(track, controls);
    root.append(stage, bar);
    host.replaceChildren(root);

    const fps = opts.fps || 24;
    let segments = [], scout = null, dragging = false;
    const dur = () => (isFinite(video.duration) && video.duration > 0 ? video.duration : (opts.duration || 0));
    const pct = (t) => (dur() ? Math.max(0, Math.min(100, (t / dur()) * 100)) : 0);

    function paintTime() {
      const t = video.currentTime || 0;
      played.style.width = `${pct(t)}%`; head.style.left = `${pct(t)}%`;
      time.textContent = `${fmt(t)} / ${fmt(dur())}`;
      const b = video.buffered; let end = 0;
      for (let i = 0; i < b.length; i++) if (b.start(i) <= t + 0.5) end = Math.max(end, b.end(i));
      buffered.style.width = `${pct(end)}%`;
    }
    function paintPlay() {
      const paused = video.paused;
      playBtn.innerHTML = paused ? ICON.play : ICON.pause;
      bigPlay.hidden = !paused;
      root.classList.toggle("is-paused", paused);
    }
    function paintSegments() {
      segLayer.replaceChildren();
      for (const s of segments) {
        const b = el("div", "bp-seg" + (s.active ? " active" : ""));
        b.style.left = `${pct(s.start)}%`; b.style.width = `${Math.max(0.35, pct(s.end) - pct(s.start))}%`;
        b.style.setProperty("--c", s.color || "#79add3");
        if (s.label != null) b.append(el("span", null, String(s.label)));
        b.title = s.title || `${fmt(s.start)} – ${fmt(s.end)}`;
        if (s.onClick) b.addEventListener("pointerdown", (e) => { e.stopPropagation(); s.onClick(); });
        segLayer.append(b);
      }
    }
    function paintScout() {
      scoutLayer.replaceChildren(); agentLayer.replaceChildren();
      root.classList.toggle("has-scout", !!scout);
      if (!scout) return;
      for (const t of scout.shots || []) { const tick = el("div", "bp-shot"); tick.style.left = `${pct(t)}%`; scoutLayer.append(tick); }
      const verdicts = new Map((scout.verdicts || []).map((v) => [v.start.toFixed(2), v]));
      const scanning = new Map((scout.scanning || []).map((w) => [w.start.toFixed(2), w]));
      const picks = new Set((scout.picks || []).map((p) => p.start.toFixed(2)));
      for (const w of scout.windows || []) {
        const k = w[0].toFixed(2), v = verdicts.get(k), sc = scanning.get(k);
        const b = el("div", "bp-win");
        b.style.left = `${pct(w[0])}%`; b.style.width = `${Math.max(0.5, pct(w[1]) - pct(w[0]))}%`;
        if (sc) { b.classList.add("scanning"); b.style.setProperty("--ac", sc.color); }
        else if (v && v.keep) { b.classList.add("keep"); b.style.setProperty("--h", `${30 + v.score * 7}%`); }
        else if (v) b.classList.add("skip");
        else b.classList.add("pending");
        if (picks.has(k)) b.classList.add("pick");
        b.title = v ? `${fmt(w[0])}: ${v.keep ? `keep ${v.score}/10` : "skip"} — ${v.subject ? v.subject + ": " : ""}${v.reason}` : `${fmt(w[0])}: ${sc ? "scouting…" : "waiting"}`;
        scoutLayer.append(b);
      }
      for (const w of scout.scanning || []) {
        const a = el("div", "bp-agent", w.label); a.style.left = `${pct((w.start + w.end) / 2)}%`; a.style.setProperty("--ac", w.color);
        agentLayer.append(a);
      }
    }

    // scrubbing
    const timeAt = (e) => { const r = track.getBoundingClientRect(); return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur(); };
    track.addEventListener("pointerdown", (e) => {
      dragging = true; track.setPointerCapture(e.pointerId); video.currentTime = timeAt(e); paintTime();
    });
    track.addEventListener("pointermove", (e) => {
      const t = timeAt(e);
      tip.textContent = fmt(t); tip.style.left = `${pct(t)}%`; tip.hidden = false;
      if (dragging) { video.currentTime = t; paintTime(); }
    });
    track.addEventListener("pointerleave", () => { tip.hidden = true; });
    track.addEventListener("pointerup", () => { dragging = false; });
    tip.hidden = true;

    const toggle = () => { if (video.paused) video.play().catch(() => {}); else video.pause(); };
    playBtn.addEventListener("click", toggle); bigPlay.addEventListener("click", toggle); video.addEventListener("click", toggle);
    prevBtn.addEventListener("click", () => { video.pause(); video.currentTime = Math.max(0, video.currentTime - 1 / fps); });
    nextBtn.addEventListener("click", () => { video.pause(); video.currentTime = Math.min(dur(), video.currentTime + 1 / fps); });
    speedBtn.addEventListener("click", () => {
      const i = (SPEEDS.indexOf(video.playbackRate) + 1) % SPEEDS.length;
      video.playbackRate = SPEEDS[i]; speedBtn.textContent = `${SPEEDS[i]}×`;
    });
    const paintLoop = () => loopBtn.classList.toggle("on", !!(video.loop || root.dataset.loop === "1"));
    loopBtn.addEventListener("click", () => {
      if (list.length) { root.dataset.loop = root.dataset.loop === "1" ? "0" : "1"; loopBtn.title = "Loop the whole list"; }
      else if (opts.onLoopToggle) { const on = root.dataset.loop !== "1"; root.dataset.loop = on ? "1" : "0"; opts.onLoopToggle(on); }
      else video.loop = !video.loop;
      paintLoop();
    });
    volBtn.addEventListener("click", () => { video.muted = !video.muted; if (!video.muted && video.volume === 0) video.volume = 1; });
    vol.addEventListener("input", () => { video.volume = +vol.value; video.muted = +vol.value === 0; });
    video.addEventListener("volumechange", () => { volBtn.innerHTML = video.muted || video.volume === 0 ? ICON.mute : ICON.vol; vol.value = String(video.muted ? 0 : video.volume); });
    fullBtn.addEventListener("click", () => { if (document.fullscreenElement) document.exitFullscreen(); else root.requestFullscreen?.(); });
    for (const ev of ["timeupdate", "seeked", "progress", "loadedmetadata", "durationchange"]) video.addEventListener(ev, () => { paintTime(); if (ev !== "timeupdate" && ev !== "progress") { paintSegments(); paintScout(); } });
    for (const ev of ["play", "pause", "ended"]) video.addEventListener(ev, paintPlay);
    // smooth playhead between timeupdate events, only while playing (a chat can hold many players)
    let raf = 0;
    const loop = () => { paintTime(); raf = video.paused ? 0 : requestAnimationFrame(loop); };
    video.addEventListener("play", () => { if (!raf) raf = requestAnimationFrame(loop); });
    // playlist: one <video>, its src swapped per item; ended -> next item (or back to the first, paused)
    let list = [], index = 0;
    const speed = () => video.playbackRate;
    function paintClip() {
      const many = list.length > 1;
      root.classList.toggle("has-list", many);
      clipBadge.hidden = !many;
      if (many) {
        const item = list[index];
        clipBadge.textContent = `${item.label ? `${item.label} · ` : ""}${index + 1}/${list.length}`;
        clipBadge.title = item.title || "";
      }
      prevClipBtn.disabled = index <= 0; nextClipBtn.disabled = index >= list.length - 1;
    }
    function load(i, play) {
      if (!list.length) return;
      index = Math.max(0, Math.min(list.length - 1, i));
      const item = list[index], rate = speed();
      video.poster = item.poster || "";
      video.src = item.src;
      video.playbackRate = rate;
      paintClip(); paintTime();
      if (play) video.play().catch(() => {});
    }
    video.addEventListener("ended", () => {
      if (!list.length) return;
      if (index < list.length - 1) load(index + 1, true);
      else load(0, root.dataset.loop === "1");
    });
    // the rate resets when src changes; keep the user's pick
    video.addEventListener("loadedmetadata", () => { const r = parseFloat(speedBtn.textContent); if (r && video.playbackRate !== r) video.playbackRate = r; });
    prevClipBtn.addEventListener("click", () => load(index - 1, !video.paused));
    nextClipBtn.addEventListener("click", () => load(index + 1, !video.paused));
    function setPlaylist(items) {
      const fresh = (items || []).filter((item) => item && item.src);
      const current = list[index]?.src;
      list = fresh;
      if (!list.length) { paintClip(); return; }
      const at = list.findIndex((item) => item.src === current);
      if (at >= 0) { index = at; paintClip(); return; }
      // nothing loaded yet, or the shown item is gone: show the first item paused (a playing clip finishes first)
      if (current && !video.paused) { index = Math.min(index, list.length - 1); paintClip(); return; }
      load(0, false);
    }
    if (opts.playlist) setPlaylist(opts.playlist);

    paintPlay(); paintTime(); paintLoop(); video.dispatchEvent(new Event("volumechange"));

    return {
      video, root,
      setSegments(list) { segments = list || []; paintSegments(); },
      setScout(s) { scout = s; paintScout(); },
      setPlaylist, get index() { return index; },
      setLoop(on) { root.dataset.loop = on ? "1" : "0"; paintLoop(); },
      destroy() { cancelAnimationFrame(raf); video.pause(); video.removeAttribute("src"); video.load(); },
    };
  }
  window.BunnyPlayer = {create};
})();
