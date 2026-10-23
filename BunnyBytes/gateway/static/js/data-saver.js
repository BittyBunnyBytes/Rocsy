// Rocsy mobile data saver. Loaded (not deferred) at the end of <head> on every
// Rocsy page and studio, so it sees each <video> before the browser loads it.
// On a phone using mobile data (or any browser asking for Save-Data) it:
//  - stops autoplay and preloading of videos/audio (tap play still works),
//  - lazy-loads images,
//  - sets the rocsy_lite cookie, so the gateway sends big images as smaller
//    WebP copies (datasaver.py) and the home page polls less often.
// Mode comes from Settings -> Appearance -> Mobile data: auto | phone | off.
// Browsers without the Network Information API (Safari, Firefox) can't tell
// Wi-Fi from mobile data, so "auto" never switches on there - use "phone".
(() => {
  const mode = document.currentScript?.dataset.mode || "auto";
  const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection || null;
  const ua = navigator.userAgent;
  const phone = Boolean(navigator.userAgentData?.mobile) || /Android|iPhone|iPad|iPod|Mobi/i.test(ua)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1); // iPadOS asks for desktop sites

  function detect() {
    if (mode === "off") return [false, "turned off in Settings"];
    if (conn?.saveData) return [true, "the browser asks to save data"];
    if (!phone) return [false, "not a phone or tablet"];
    if (mode === "phone") return [true, "always on for phones"];
    if (!conn) return [false, "this browser doesn't say whether it's on Wi-Fi"];
    if (conn.type && conn.type !== "unknown") {
      const cellular = conn.type === "cellular" || conn.type === "wimax";
      return [cellular, cellular ? "on mobile data" : `on ${conn.type === "wifi" ? "Wi-Fi" : conn.type}`];
    }
    if (["slow-2g", "2g", "3g"].includes(conn.effectiveType)) return [true, `slow connection (${conn.effectiveType})`];
    return [false, "connection type unknown"];
  }

  const state = { mode, phone, active: false, reason: "" };
  window.RocsyDataSaver = state;

  function setCookie(on) {
    const has = /(?:^|;\s*)rocsy_lite=1/.test(document.cookie);
    if (has === on) return;
    const secure = location.protocol === "https:" ? "; secure" : "";
    document.cookie = on ? `rocsy_lite=1; path=/; max-age=2592000; samesite=lax${secure}`
      : `rocsy_lite=; path=/; max-age=0; samesite=lax${secure}`;
  }

  function trimMedia(el) {
    if (el.tagName === "IMG") {
      if (!el.hasAttribute("loading")) el.loading = "lazy";
      if (!el.hasAttribute("decoding")) el.decoding = "async";
      return;
    }
    if (el.tagName !== "VIDEO" && el.tagName !== "AUDIO") return;
    if (el.autoplay) {
      el.autoplay = false;
      if (!el.paused && !navigator.userActivation?.isActive) el.pause();
    }
    // A poster already shows something; without one keep the first frame.
    const preload = el.tagName === "VIDEO" && !el.poster ? "metadata" : "none";
    if (el.readyState === 0 && el.getAttribute("preload") !== preload && el.preload !== "none") el.preload = preload;
  }

  function trimTree(node) {
    if (node.nodeType !== 1) return;
    trimMedia(node);
    if (node.firstElementChild) node.querySelectorAll("video, audio, img").forEach(trimMedia);
  }

  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "attributes") trimMedia(record.target);
      else record.addedNodes.forEach(trimTree);
    }
  });

  function apply() {
    const [active, reason] = detect();
    const changed = active !== state.active;
    Object.assign(state, { active, reason, type: conn?.type || "", effectiveType: conn?.effectiveType || "" });
    setCookie(active);
    document.documentElement.toggleAttribute("data-rocsy-lite", active);
    if (active && changed) {
      observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true,
        attributeFilter: ["autoplay", "preload", "src"] });
      trimTree(document.documentElement);
    } else if (!active && changed) {
      observer.disconnect();
    }
    document.dispatchEvent(new CustomEvent("rocsy:datasaver", { detail: state }));
  }

  apply();
  conn?.addEventListener?.("change", apply);
})();
