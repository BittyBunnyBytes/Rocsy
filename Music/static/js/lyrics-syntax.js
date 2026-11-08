// Colors for lyric syntax: [section tags] by kind, (parenthetical) vocals.
// Plain script, loaded before the modules, shared by the form, the job details and the player.
(() => {
  const KINDS = [
    ["pre", /pre-?\s?chorus|post-?\s?chorus|build/i],
    ["chorus", /chorus|hook|refrain/i],
    ["verse", /verse|rap/i],
    ["bridge", /bridge/i],
    ["inst", /intro|outro|instrumental|solo|break|drop|interlude/i],
  ];
  const tagKind = (tag) => KINDS.find(([, pattern]) => pattern.test(tag))?.[0] || "other";
  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));

  // Keeps every character, so it can sit exactly behind a textarea.
  function highlight(text) {
    let html = "", last = 0;
    for (const match of String(text).matchAll(/\[[^\]\n]*\]|\([^)\n]*\)/g)) {
      html += escapeHtml(text.slice(last, match.index));
      const className = match[0][0] === "[" ? `ly-tag ly-${tagKind(match[0])}` : "ly-paren";
      html += `<span class="${className}">${escapeHtml(match[0])}</span>`;
      last = match.index + match[0].length;
    }
    return html + escapeHtml(text.slice(last));
  }

  function bindEditor(textarea) {
    const editor = document.createElement("div");
    editor.className = "ly-editor";
    const layer = document.createElement("pre");
    layer.className = "ly-layer";
    layer.setAttribute("aria-hidden", "true");
    textarea.before(editor);
    editor.append(layer, textarea);
    const sync = () => {
      // clientWidth excludes the scrollbar, so both layers wrap at the same width.
      const width = textarea.clientWidth, height = textarea.clientHeight;
      const style = getComputedStyle(textarea);
      layer.style.font = style.font;
      layer.style.padding = style.padding;
      layer.style.top = layer.style.left = style.borderTopWidth;
      layer.style.width = `${width}px`;
      layer.style.height = `${height}px`;
      // A trailing newline only takes up a line in a <pre> when something follows it.
      layer.innerHTML = `${highlight(textarea.value)}\n`;
      layer.scrollTop = textarea.scrollTop;
    };
    // Retry and the section buttons set .value or call setRangeText; neither fires "input".
    const valueProperty = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
    Object.defineProperty(textarea, "value", {
      get() { return valueProperty.get.call(this); },
      set(value) { valueProperty.set.call(this, value); sync(); },
    });
    const setRangeText = textarea.setRangeText;
    textarea.setRangeText = function (...args) { setRangeText.apply(this, args); sync(); };
    textarea.addEventListener("input", sync);
    textarea.addEventListener("scroll", () => { layer.scrollTop = textarea.scrollTop; });
    new ResizeObserver(sync).observe(textarea);
    sync();
  }

  window.MusicLyrics = { tagKind, highlight, bindEditor };
})();
