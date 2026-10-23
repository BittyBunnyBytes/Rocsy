// Rocsy home: live studio status from /api/home (every 10 s while the tab is
// visible). The server renders the page with a snapshot embedded as
// #h-initial, so this paints that at once and only polls for changes.
(() => {
  // Studio ids (Vid, Krea, Mus) stay in URLs and data; people see these names.
  const LABELS = { Vid: "Video", Krea: "Image", Mus: "Music" };
  const label = (id) => LABELS[id] || id;
  const $ = (selector, root = document) => root.querySelector(selector);

  function setChip(name, text, state, href) {
    const chip = $(`[data-chip="${name}"]`);
    if (!chip) return;
    chip.dataset.state = state;
    $(".h-chip-text", chip).textContent = text;
    if (href) chip.href = href;
  }

  function setLive(card, info) {
    const live = $("[data-live]", card) || card;
    const text = $("[data-live-text]", card);
    const state = !info ? "unknown" : !info.online || info.problem ? "offline" : info.active || info.ok ? "busy" : info.waiting ? "waiting" : "idle";
    card.dataset.state = state;
    if (live !== card) live.dataset.state = state;
    if (text && info) text.textContent = info.text;
    const current = $("[data-current]", card);
    if (current) current.textContent = info?.current ? `“${info.current}”` : "";
  }

  function summary(data) {
    const studios = data.studios;
    const busy = data.busy.map((name) => name === "Agent" ? "the Master Agent is working"
      : `${label(name)} is ${studios[name].text.replace(/^\d+ /, "").split(" · ")[0]}`);
    const waiting = studios.Queue?.waiting || 0;
    if (!busy.length && !waiting) return studios.Queue?.online ? "Everything is idle. The GPU is all yours." : "The GPU queue is offline.";
    const text = [busy.join(", "), waiting ? `${waiting} waiting in the GPU queue` : ""].filter(Boolean).join(" · ") + ".";
    return text[0].toUpperCase() + text.slice(1);
  }

  // A thumbnail that fails (output deleted) drops its card.
  function watchImage(img) {
    const drop = () => img.closest(".h-shot")?.remove();
    if (img.complete && !img.naturalWidth) drop();
    else img.addEventListener("error", drop, { once: true });
  }

  function makeShot(item, index) {
    const link = document.createElement("a");
    link.className = "h-shot";
    link.href = item.url;
    link.dataset.studio = item.studio;
    const frame = document.createElement("span");
    frame.className = "h-shot-frame";
    const img = document.createElement("img");
    img.src = item.thumb;
    img.alt = "";
    img.decoding = "async";
    if (index >= 4) img.loading = "lazy";
    watchImage(img);
    const tag = document.createElement("span");
    tag.className = "h-shot-tag";
    tag.textContent = label(item.studio);
    frame.append(img, tag);
    const title = document.createElement("span");
    title.className = "h-shot-title";
    title.textContent = item.title || item.meta || label(item.studio);
    link.append(frame, title);
    return link;
  }

  function renderStrip(items) {
    const section = $("#h-latest-section");
    const strip = $("#h-strip");
    section.hidden = !items.length;
    const key = items.map((item) => item.thumb).join("|");
    if (strip.dataset.key === key) return;  // nothing new: leave the images alone
    strip.dataset.key = key;
    // Keep the cards that are still in the list so their images never reload.
    const existing = new Map([...strip.children].map((node) => [$("img", node)?.getAttribute("src"), node]));
    strip.replaceChildren(...items.map((item, index) => existing.get(item.thumb) || makeShot(item, index)));
  }

  function paintArt(latest) {
    for (const [studio, thumb] of Object.entries(latest || {})) {
      const art = $(`.h-studio[data-studio="${studio}"] .h-studio-art`);
      if (!thumb || !art || art.dataset.src === thumb) continue;
      art.dataset.src = thumb;
      // Fade in only once the image has loaded, so the card never flashes empty.
      const img = new Image();
      img.decoding = "async";
      img.onload = () => {
        art.style.backgroundImage = `url("${thumb.replace(/"/g, "%22")}")`;
        art.classList.add("has-art");
      };
      img.src = thumb;
    }
  }

  // Trainer card: one random dataset picture per visit, or a crossfading
  // slideshow of the dataset being trained (video-only datasets show clip thumbnails).
  const SLIDE_MS = 5000;
  const trainerArt = { key: null, slides: [], index: 0, timer: null };
  const stillMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches || window.RocsyDataSaver?.active;

  function showSlide(art, src) {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      const slide = document.createElement("span");
      slide.className = "h-art-slide";
      slide.style.backgroundImage = `url("${src.replace(/"/g, "%22")}")`;
      art.append(slide);
      art.classList.add("has-art");
      requestAnimationFrame(() => requestAnimationFrame(() => slide.classList.add("on")));
      // Drop the older slides once the new one has faded in over them.
      setTimeout(() => [...art.querySelectorAll(".h-art-slide")].slice(0, -1).forEach((old) => old.remove()), 1800);
    };
    img.onerror = () => {  // deleted since: skip it
      trainerArt.slides = trainerArt.slides.filter((item) => item !== src);
      if (trainerArt.timer && trainerArt.slides.length) nextSlide(art);
    };
    img.src = src;
  }

  function nextSlide(art) {
    clearTimeout(trainerArt.timer);
    trainerArt.timer = null;
    if (!trainerArt.slides.length) return;
    trainerArt.index = (trainerArt.index + 1) % trainerArt.slides.length;
    showSlide(art, trainerArt.slides[trainerArt.index]);
    if (trainerArt.slides.length > 1 && !stillMotion()) trainerArt.timer = setTimeout(() => nextSlide(art), SLIDE_MS);
  }

  function paintTrainer(info) {
    const art = $('.h-studio[data-studio="Trainer"] .h-studio-art');
    const show = info?.art;
    if (!art || !show?.pictures?.length) return;
    // Idle: keep the first random pick for the whole visit. Training: restart only when the dataset changes.
    const key = show.slideshow ? `train:${show.dataset}` : "idle";
    if (trainerArt.key === key) return;
    trainerArt.key = key;
    art.classList.toggle("h-art-slideshow", show.slideshow);
    trainerArt.slides = show.slideshow ? show.pictures : [show.pictures[Math.floor(Math.random() * show.pictures.length)]];
    trainerArt.index = -1;
    nextSlide(art);
  }

  // Voice card: one random character portrait per visit.
  function paintVoice(info) {
    const art = $('.h-studio[data-studio="Voice"] .h-studio-art');
    const pictures = info?.art?.pictures;
    if (!art || !pictures?.length || art.dataset.src) return;
    const src = pictures[Math.floor(Math.random() * pictures.length)];
    art.dataset.src = src;
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      art.style.backgroundImage = `url("${src.replace(/"/g, "%22")}")`;
      art.classList.add("has-art");
    };
    img.onerror = () => { delete art.dataset.src; };  // deleted since: try another next poll
    img.src = src;
  }

  // Music card: a waveform of the newest finished song. The page decodes the
  // audio once per song and keeps the bars in localStorage; data saver skips it.
  const WAVE_BARS = 96;
  const waveKey = "rocsy-mus-wave";
  let waveFor = null;

  function drawWave(art, peaks) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "h-art-wave");
    svg.setAttribute("viewBox", `0 0 ${WAVE_BARS * 2} 100`);
    svg.setAttribute("preserveAspectRatio", "none");
    peaks.forEach((peak, index) => {
      const height = Math.max(2, peak * 96);
      const bar = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      bar.setAttribute("x", index * 2 + 0.3);
      bar.setAttribute("y", (100 - height) / 2);
      bar.setAttribute("width", 1.4);
      bar.setAttribute("height", height);
      bar.setAttribute("rx", 0.7);
      svg.append(bar);
    });
    art.replaceChildren(svg);
    art.classList.add("has-art", "has-wave");
  }

  async function wavePeaks(src) {
    const response = await fetch(src, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    const context = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, 1, 44100);
    const audio = await context.decodeAudioData(await response.arrayBuffer());
    const samples = audio.getChannelData(0);
    const step = Math.max(1, Math.floor(samples.length / WAVE_BARS));
    const peaks = [];
    for (let bar = 0; bar < WAVE_BARS; bar++) {
      let sum = 0;  // RMS per slice reads as loudness better than the raw peak
      for (let i = bar * step, end = Math.min(samples.length, i + step); i < end; i++) sum += samples[i] * samples[i];
      peaks.push(Math.sqrt(sum / step));
    }
    const top = Math.max(...peaks) || 1;
    return peaks.map((peak) => Math.round((peak / top) * 100) / 100);
  }

  async function paintMusic(info) {
    const art = $('.h-studio[data-studio="Mus"] .h-studio-art');
    const song = info?.art;
    if (!art || !song?.id || waveFor === song.id) return;
    waveFor = song.id;
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem(waveKey) || "null"); } catch (_) {}
    if (cached?.id === song.id && Array.isArray(cached.peaks)) { drawWave(art, cached.peaks); return; }
    if (window.RocsyDataSaver?.active) { waveFor = null; return; }
    try {
      const peaks = await wavePeaks(song.audio);
      if (waveFor !== song.id) return;  // a newer song arrived meanwhile
      drawWave(art, peaks);
      try { localStorage.setItem(waveKey, JSON.stringify({ id: song.id, peaks })); } catch (_) {}
    } catch {
      // can't fetch or decode: keep the plain card and don't retry this song
    }
  }

  // Vibe Coding card: a GitHub-style grid of coding activity per local day
  // (columns are weeks, Sunday on top), newest week on the right.
  const HEAT_WEEKS = 26;
  let heatKey = null;

  function paintCode(activity) {
    const art = $('.h-studio[data-card="Code"] .h-studio-art');
    if (!art || !Array.isArray(activity)) return;
    const key = JSON.stringify(activity);
    if (heatKey === key) return;
    heatKey = key;
    const dayKey = (date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
    const perDay = new Map();
    for (const [hour, count] of activity) {
      const day = dayKey(new Date(hour * 1000));
      perDay.set(day, (perDay.get(day) || 0) + count);
    }
    const today = new Date();
    const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - today.getDay() - (HEAT_WEEKS - 1) * 7);
    const top = Math.max(1, ...perDay.values());
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "h-art-heat");
    svg.setAttribute("viewBox", `0 0 ${HEAT_WEEKS * 12} ${7 * 12}`);
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    for (let week = 0; week < HEAT_WEEKS; week++) {
      for (let weekday = 0; weekday < 7; weekday++) {
        const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + week * 7 + weekday);
        if (date > today) break;
        const count = perDay.get(dayKey(date)) || 0;
        const cell = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        cell.setAttribute("x", week * 12 + 1);
        cell.setAttribute("y", weekday * 12 + 1);
        cell.setAttribute("width", 10);
        cell.setAttribute("height", 10);
        cell.setAttribute("rx", 2);
        // Four levels like GitHub, relative to the busiest day.
        cell.setAttribute("class", count ? `h-heat-${Math.ceil((count / top) * 4)}` : "h-heat-0");
        svg.append(cell);
      }
    }
    art.replaceChildren(svg);
    art.classList.add("has-art", "has-heat");
  }

  function apply(data) {
    document.querySelectorAll("[data-studio]").forEach((card) => {
      if (card.classList.contains("h-shot")) return;
      setLive(card, data.studios[card.dataset.studio]);
    });
    const queue = data.studios.Queue;
    setChip("gpu", queue.online ? queue.text : "Queue offline", !queue.online ? "offline" : queue.active ? "busy" : "idle", queue.href);
    const llama = data.studios.Llama;
    setChip("llama", llama.online ? `Llama · ${llama.text}` : "Llama offline", !llama.online ? "offline" : llama.active ? "busy" : "idle", llama.href);
    const agent = data.studios.Agent;
    if (agent) setChip("agent", !agent.online ? "Master Agent offline" : agent.active ? `Master Agent · ${agent.text.toLowerCase()}` : "Master Agent · ready",
                       !agent.online ? "offline" : agent.active ? "busy" : "idle", agent.href);
    const models = data.studios.Models;
    if (models) {
      const n = models.updates || 0;
      setChip("updates", !models.online ? "Models offline" : n ? `${n} update${n === 1 ? "" : "s"} available` : "Models up to date",
              !models.online ? "offline" : n ? "waiting" : "idle", models.href);
    }
    $("#h-summary").textContent = summary(data);
    renderStrip(data.recent);
    paintArt(data.latest);
    paintTrainer(data.studios.Trainer);
    paintVoice(data.studios.Voice);
    paintMusic(data.studios.Mus);
    paintCode(data.code_activity);
  }

  let timer = null;
  let inflight = false;
  // Mobile data saver (static/js/data-saver.js): poll a third as often.
  const pollDelay = () => (window.RocsyDataSaver?.active ? 30000 : 10000);
  async function refresh() {
    clearTimeout(timer);
    if (inflight) return;
    inflight = true;
    try {
      const response = await fetch("/api/home", { credentials: "same-origin", cache: "no-store" });
      if (response.status === 401) { location.href = "/login?next=/"; return; }
      if (!response.ok) throw new Error(String(response.status));
      apply(await response.json());
    } catch {
      setChip("gpu", "Status unavailable", "offline");
    } finally {
      inflight = false;
    }
    if (!document.hidden) timer = setTimeout(refresh, pollDelay());
  }

  // Home prompt box: start a Master Agent session and open it. Without JS the form
  // GETs /Agent/?prompt=..., which only prefills the composer there.
  const ask = $("#h-ask");
  if (ask) {
    const field = $("#h-ask-input");
    const grow = () => { field.style.height = "auto"; field.style.height = `${Math.min(220, field.scrollHeight)}px`; };
    // Keep an unsent prompt across leaving and coming back to the home page.
    const draftKey = "rocsy-ask-draft";
    const saveDraft = (text) => { try { text ? sessionStorage.setItem(draftKey, text) : sessionStorage.removeItem(draftKey); } catch (_) {} };
    try { if (!field.value) field.value = sessionStorage.getItem(draftKey) || ""; } catch (_) {}
    if (field.value) grow();
    field.addEventListener("input", () => { grow(); saveDraft(field.value); });
    // "/" anywhere on the page jumps to the prompt box.
    document.addEventListener("keydown", (event) => {
      if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey || document.body.classList.contains("h-tour-open")) return;
      const active = document.activeElement;
      if (active && (active.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName))) return;
      event.preventDefault();
      field.focus();
    });
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); ask.requestSubmit(); }
    });
    ask.addEventListener("submit", async (event) => {
      event.preventDefault();
      const prompt = field.value.trim();
      if (!prompt) { field.focus(); return; }
      const button = $("button", ask), note = $("#h-ask-note");
      button.disabled = true; note.textContent = "Starting the Master Agent…";
      try {
        const response = await fetch("/Agent/api/sessions", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "" }) });
        if (response.status === 401) { location.href = "/login?next=/"; return; }
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || `The Agent service answered ${response.status}`);
        saveDraft("");
        location.href = `/Agent/?s=${encodeURIComponent(body.id)}`;
      } catch (error) {
        note.textContent = error.message; button.disabled = false;
      }
    });
  }

  document.querySelectorAll("#h-strip img").forEach(watchImage);
  let data = null;
  try { data = JSON.parse($("#h-initial")?.textContent || "null"); } catch { data = null; }
  if (data) {
    apply(data);
    timer = setTimeout(refresh, pollDelay());
  } else {
    refresh();
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
  // Coming back via the back button restores a frozen page: refresh it.
  window.addEventListener("pageshow", (event) => { if (event.persisted) refresh(); });
})();

// First boot stays on the home page and points at the real interface. Nothing
// under a highlighted link is opened, so it is safe to explore with Next/Back.
(() => {
  const storageKey = "rocsy-onboarding-complete";
  const forced = new URLSearchParams(location.search).get("onboarding") === "1";
  let complete = false;
  try { complete = localStorage.getItem(storageKey) === "1"; } catch (_) {}
  if (complete && !forced) return;

  const tour = document.getElementById("h-tour");
  const card = document.querySelector(".h-tour-card");
  const title = document.getElementById("h-tour-title");
  const copy = document.getElementById("h-tour-copy");
  const stepLabel = document.getElementById("h-tour-step");
  const progress = document.getElementById("h-tour-progress");
  const back = document.getElementById("h-tour-back");
  const next = document.getElementById("h-tour-next");
  const skip = document.getElementById("h-tour-skip");
  if (!tour || !card) return;

  const steps = [
    { target: null, title: "Welcome to Rocsy", copy: "Rocsy is your local AI workspace for creating with video, images, music, voice and local language models. This quick tour shows you where everything lives without opening another page." },
    { target: "#h-ask", title: "Master Agent", copy: "Describe what you want in plain language. The Master Agent can plan work across video, image, music, voice and training, then coordinate the GPU queue for you." },
    { target: ".h-chip[data-chip='gpu']", alsoHighlight: [".h-chip[data-chip='llama']", ".h-chip[data-chip='agent']"], title: "Queue system", copy: "These buttons show the GPU queue, Llama and Master Agent status. One GPU means one heavy job at a time, so queued work waits safely instead of fighting for VRAM." },
    { target: ".h-create", mobileTarget: ".h-create .h-studio", title: "Creative studios", copy: "This entire area is your creative workspace. Video is one example: its studio lets you create clips, use Agentic sessions, and post-edit finished work. Other cards cover images, music, voice and training." },
    { target: ":is(.h-studio, .h-tool)[href^='/Trainer/']", title: "Trainer", copy: "Trainer is where you prepare datasets and train your own LoRAs. Training is GPU-heavy, so it participates in the same queue." },
    { target: ":is(.h-studio, .h-tool)[href^='/Models/']", title: "Model downloader", copy: "Models downloads checkpoints and LoRAs from supported sources, checks hardware fit, and keeps your local library up to date." },
    { target: ":is(.h-studio, .h-tool)[href^='/Llama/']", title: "Llama", copy: "Llama runs local language models through llama-swap. Open it when you need model selection, parameters, or backend controls." },
    { target: ".h-icon-link[href='/Settings']", title: "Settings", copy: "This gear opens Settings: themes, notifications, visible modules, services and Master Agent permissions. You can replay this tour from Settings → Modules." },
    { target: null, title: "You’re ready", copy: "Start with a studio when you know what you want, or ask the Master Agent when you want Rocsy to plan the whole job. You can safely leave queued work running in the background." },
  ].filter((step) => !step.target || document.querySelector(step.target));
  let index = 0;
  let highlighted = [];
  const previousFocus = document.activeElement;
  const mobile = matchMedia("(max-width: 560px)");

  function finish() {
    try { localStorage.setItem(storageKey, "1"); } catch (_) {}
    highlighted.forEach((element) => element.classList.remove("h-tour-target"));
    tour.hidden = true;
    tour.setAttribute("aria-hidden", "true");
    document.body.classList.remove("h-tour-open");
    if (forced && history.replaceState) history.replaceState(null, "", location.pathname + location.hash);
    previousFocus?.focus?.({ preventScroll: true });
  }

  function place(target) {
    if (mobile.matches) {
      if (!target) return;
      target.scrollIntoView({ block: "center", behavior: "auto" });
      const rect = target.getBoundingClientRect();
      const topEdge = 68; // Leave the sticky header and spotlight outline visible.
      const bottomEdge = card.getBoundingClientRect().top - 16;
      const available = bottomEdge - topEdge;
      if (rect.height > available || rect.top < topEdge) {
        scrollBy({ top: rect.top - topEdge, behavior: "auto" });
      } else if (rect.bottom > bottomEdge) {
        scrollBy({ top: rect.bottom - bottomEdge, behavior: "auto" });
      }
      return;
    }
    if (!target) {
      const top = Math.max(12, (innerHeight - card.offsetHeight) / 2);
      card.style.setProperty("--tour-left", `${Math.max(12, (innerWidth - Math.min(420, innerWidth - 24)) / 2)}px`);
      card.style.setProperty("--tour-top", `${top}px`);
      return;
    }
    // Measure only after the target is actually centered. Smooth scrolling can
    // leave the tooltip positioned against the target's old off-screen rect.
    target.scrollIntoView({ block: "center", behavior: "auto" });
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const rect = target.getBoundingClientRect();
      const width = Math.min(420, innerWidth - 24);
      const below = rect.bottom + 18;
      const useAbove = below + card.offsetHeight > innerHeight - 12 && rect.top > card.offsetHeight + 28;
      card.style.setProperty("--tour-left", `${Math.max(12, Math.min(innerWidth - width - 12, rect.left + rect.width / 2 - width / 2))}px`);
      const maxTop = Math.max(12, innerHeight - card.offsetHeight - 12);
      card.style.setProperty("--tour-top", `${useAbove ? Math.max(12, rect.top - card.offsetHeight - 18) : Math.max(12, Math.min(maxTop, below))}px`);
    }));
  }

  function show() {
    highlighted.forEach((element) => element.classList.remove("h-tour-target"));
    const step = steps[index];
    highlighted = [mobile.matches && step.mobileTarget ? step.mobileTarget : step.target, ...(step.alsoHighlight || [])]
      .filter(Boolean).map((selector) => document.querySelector(selector)).filter(Boolean);
    highlighted.forEach((element) => element.classList.add("h-tour-target"));
    stepLabel.textContent = `QUICK TOUR · ${index + 1} OF ${steps.length}`;
    title.textContent = step.title;
    copy.textContent = step.copy;
    progress.style.width = `${((index + 1) / steps.length) * 100}%`;
    back.hidden = index === 0;
    next.textContent = index === steps.length - 1 ? "Start using Rocsy" : "Next";
    place(highlighted[0]);
    next.focus({ preventScroll: true });
  }

  next.addEventListener("click", () => index === steps.length - 1 ? finish() : (index += 1, show()));
  back.addEventListener("click", () => { if (index) { index -= 1; show(); } });
  skip.addEventListener("click", finish);
  document.addEventListener("click", (event) => {
    if (!tour.hidden && !card.contains(event.target)) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
  document.addEventListener("keydown", (event) => {
    if (tour.hidden) return;
    if (event.key === "Escape") { finish(); return; }
    if (event.key === "ArrowRight") { next.click(); return; }
    if (event.key === "ArrowLeft" && index) { back.click(); return; }
    if (event.key === "Tab") {
      const controls = [...card.querySelectorAll("button:not([hidden])")];
      const at = controls.indexOf(document.activeElement);
      if (event.shiftKey && at <= 0) { event.preventDefault(); controls.at(-1).focus(); }
      else if (!event.shiftKey && at === controls.length - 1) { event.preventDefault(); controls[0].focus(); }
    }
  });
  window.addEventListener("resize", () => { if (!tour.hidden) place(highlighted[0]); });
  mobile.addEventListener("change", () => { if (!tour.hidden) show(); });
  window.visualViewport?.addEventListener("resize", () => { if (!tour.hidden) place(highlighted[0]); });

  tour.hidden = false;
  tour.setAttribute("aria-hidden", "false");
  document.body.classList.add("h-tour-open");
  show();
})();
