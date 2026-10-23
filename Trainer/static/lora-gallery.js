(() => {
  const base = window.TRAINER_BASE || "/Trainer";
  const grid = document.querySelector("#shared-lora-grid");
  const search = document.querySelector("#shared-lora-search");
  const studio = document.querySelector("#shared-lora-studio");
  const nsfw = document.querySelector("#shared-lora-nsfw");
  let catalogs = null;
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const studios = { h3: {label:"Video", path:"/Video"}, krea: {label:"Krea Image", path:"/Image"} };
  function render() {
    if (!catalogs) return;
    const query = search.value.trim().toLowerCase();
    const items = Object.entries(catalogs).flatMap(([source, catalog]) => Object.entries(catalog).map(([filename, info]) => ({source, filename, info})))
      .filter(({source, filename, info}) => (studio.value === "all" || source === studio.value)
        && (!query || [filename, info.name, info.version, info.creator, info.base_model, info.description].join(" ").toLowerCase().includes(query)));
    grid.innerHTML = items.length ? items.map(({source, filename, info}) => {
      const preview = info.previews?.[0];
      const blurred = preview?.nsfw >= 16 && !nsfw.checked;
      const poster = preview?.poster ? `${studios[source].path}/${preview.poster.startsWith("api/") ? "" : "static/"}${preview.poster}` : "";
      const safeUrl = /^https?:\/\//i.test(info.url || "") ? info.url : "";
      return `<article class="shared-lora-card"><div class="shared-lora-preview${blurred ? " blurred" : ""}">${poster ? `<img src="${escape(poster)}" alt="" loading="lazy">` : "No preview"}</div><div class="shared-lora-info"><small>${studios[source].label} · ${escape(info.base_model)}</small><h3>${escape(info.name || filename)}</h3><p>${escape(info.creator || "")}${info.version ? ` · ${escape(info.version)}` : ""}</p><p class="shared-lora-description">${escape(info.description || "")}</p>${safeUrl ? `<a href="${escape(safeUrl)}" target="_blank" rel="noopener noreferrer">Source page</a>` : ""}</div></article>`;
    }).join("") : '<p class="muted">No matching LoRAs.</p>';
  }
  async function load() {
    if (catalogs) return render();
    grid.textContent = "Loading gallery…";
    try {
      const response = await fetch(`${base}/api/lora-gallery/shared`);
      if (!response.ok) throw new Error(`Gallery unavailable (${response.status})`);
      catalogs = await response.json();
      render();
    } catch (error) { grid.textContent = error.message; }
  }
  document.addEventListener("lora-gallery-tab-shown", load);
  for (const control of [search, studio, nsfw]) control.addEventListener(control === search ? "input" : "change", render);
  if (!document.querySelector("#tab-loras").hidden) load();
})();
