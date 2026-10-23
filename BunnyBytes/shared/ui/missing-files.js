/* Missing model files dialog, shared by Image / Video / Music.
 *
 * A studio answers Generate with 409 {error: "missing_model_files", missing: {files, nodes}}
 * when ComfyUI doesn't list a file the graph loads (shared/workflow_models.missing_files).
 * This catches that answer on any fetch, asks Models what can be done about each file
 * (/Models/api/required-files: a copy found on this computer to link in, or a known
 * download), and on "Get all" links/downloads them, shows progress, then sends the
 * same Generate request again.
 */
(() => {
  if (window.RocsyMissingFiles) return;
  const MODELS = '/Models';
  const CODE = 'missing_model_files';
  const nativeFetch = window.fetch.bind(window);
  let open = null; // the dialog being shown, so a second refusal doesn't stack another

  const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
  const gb = (bytes) => !bytes ? '' : bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`;
  const base = (name) => String(name).split('/').pop();

  async function api(path, body) {
    const response = await nativeFetch(`${MODELS}${path}`, body === undefined ? { headers: { Accept: 'application/json' } } : {
      method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || `Models answered ${response.status}`);
    return data;
  }

  function sourceLine(row) {
    const pick = row.choice;
    if (pick?.use === 'link') {
      const hit = row.found.find((h) => h.path === pick.path) || {};
      return `Found on this computer${hit.size_ok ? '' : ' (different size)'} · ${gb(hit.size)}<span class="mf-path">${esc(hit.path)}</span>`;
    }
    if (pick?.use === 'download') {
      return `Download ${gb(row.download.size)} from ${esc(row.download.origin)}${row.download.gated ? ' · needs a Hugging Face token with access (Models → Settings)' : ''}`;
    }
    return `Not on this computer and no known download. <a href="${MODELS}/?q=${encodeURIComponent(row.search)}" target="_blank" rel="noopener">Search in Models</a>`;
  }

  function choices(row) {
    const options = row.found.map((hit) => ({ use: 'link', path: hit.path, label: `Use the copy at ${hit.path}${hit.size_ok ? '' : ' (different size)'}` }));
    if (row.download) options.push({ use: 'download', label: `Download it (${gb(row.download.size) || 'size unknown'})` });
    options.push({ use: 'skip', label: 'Skip this file' });
    return options;
  }

  function show(missing, studio, replay) {
    if (open) open.close();
    const dialog = document.createElement('dialog');
    dialog.className = 'mf-dialog';
    const files = missing.files || [];
    const nodes = missing.nodes || [];
    dialog.innerHTML = `
      <form method="dialog" class="mf-body">
        <h2 class="mf-title">Missing model files</h2>
        <p class="mf-lead">${esc(studio || 'This studio')} can't render this yet: ComfyUI doesn't have ${files.length ? `${files.length} file${files.length > 1 ? 's' : ''}` : 'everything'} it needs.</p>
        <ul class="mf-rows">${files.map((f, i) => `<li class="mf-row" data-i="${i}"><div class="mf-name">${esc(base(f.name))} <span class="mf-kind">${esc(f.label || '')}${f.folder ? ` · models/${esc(f.folder)}` : ''}</span></div><div class="mf-src">Looking on this computer and for a download…</div></li>`).join('')}
        ${nodes.map((n) => `<li class="mf-row is-node"><div class="mf-name">${esc(n)} <span class="mf-kind">ComfyUI node</span></div><div class="mf-src">Not installed in ComfyUI. Install its pack in <a href="${MODELS}/" target="_blank" rel="noopener">Models → Core Backend → Custom nodes</a>, then restart ComfyUI.</div></li>`).join('')}</ul>
        <label class="mf-auto"><input type="checkbox" checked> Generate when every file is in place</label>
        <p class="mf-status" role="status"></p>
        <div class="mf-actions">
          <button type="submit" value="cancel" class="mf-cancel">Close</button>
          <button type="button" class="mf-go" disabled>Get all missing files</button>
        </div>
      </form>`;
    document.body.append(dialog);
    open = dialog;
    dialog.addEventListener('close', () => { if (open === dialog) open = null; dialog.remove(); });
    dialog.showModal();
    const status = dialog.querySelector('.mf-status');
    const go = dialog.querySelector('.mf-go');
    const auto = dialog.querySelector('.mf-auto input');
    let rows = [];

    const paint = () => {
      rows.forEach((row, i) => {
        const li = dialog.querySelector(`.mf-row[data-i="${i}"]`);
        if (!li || row.done) return;
        const options = choices(row);
        li.querySelector('.mf-src').innerHTML = sourceLine(row) + (options.length > 2 ? `<select class="mf-pick" aria-label="What to do with ${esc(base(row.name))}">${options.map((o, k) => `<option value="${k}"${(o.use === row.choice?.use && (o.path || '') === (row.choice?.path || '')) || (!row.choice && o.use === 'skip') ? ' selected' : ''}>${esc(o.label)}</option>`).join('')}</select>` : '');
        li.querySelector('.mf-pick')?.addEventListener('change', (event) => {
          const o = options[Number(event.target.value)];
          row.choice = o.use === 'skip' ? null : { use: o.use, path: o.path };
          paint();
        });
      });
      const picked = rows.filter((row) => row.choice && !row.done);
      const bytes = picked.filter((row) => row.choice.use === 'download').reduce((sum, row) => sum + (row.download.size || 0), 0);
      go.disabled = !picked.length;
      go.textContent = picked.length ? `Get all missing files${bytes ? ` (${gb(bytes)} to download)` : ''}` : 'Nothing to get';
    };

    if (files.length) {
      api('/api/required-files', { files }).then((data) => {
        rows = data.files.map((row) => ({ ...row,
          choice: row.action === 'link' ? { use: 'link', path: row.found[0].path } : row.action === 'download' ? { use: 'download' } : null }));
        if (rows.some((row) => !row.searched)) status.textContent = "Couldn't search this computer (host helper offline); only downloads are offered.";
        paint();
      }).catch((error) => { status.textContent = `Couldn't ask Models: ${error.message}`; });
    } else {
      go.hidden = true;
      auto.closest('label').hidden = true;
    }

    go.addEventListener('click', async () => {
      if (go.dataset.mode === 'generate') {
        go.disabled = true;
        replay().then(() => dialog.close()).catch((error) => { status.textContent = error.message; go.disabled = false; });
        return;
      }
      const picked = rows.filter((row) => row.choice && !row.done);
      go.disabled = true;
      dialog.querySelectorAll('.mf-pick').forEach((el) => { el.disabled = true; });
      status.textContent = 'Working…';
      let result;
      try {
        result = await api('/api/required-files/fetch', { files: picked.map((row) => ({ name: row.name, folder: row.folder, label: row.label, ...row.choice })) });
      } catch (error) {
        status.textContent = `Couldn't start: ${error.message}`;
        go.disabled = false;
        return;
      }
      const rowFor = (name) => rows.find((row) => row.name === name || base(row.name) === name);
      for (const item of result.linked) {
        const row = rowFor(item.name);
        if (!row) continue;
        row.done = 'linked';
        dialog.querySelector(`.mf-row[data-i="${rows.indexOf(row)}"] .mf-src`).innerHTML = `Linked into models/${esc(item.dest)} (${esc(item.method)}, no copy made)`;
      }
      const jobs = new Map(result.jobs.map((job) => [job.id, rowFor(job.name)]));
      const errors = [...result.errors];
      const finish = () => {
        const left = rows.filter((row) => row.choice && !row.done).length + rows.filter((row) => !row.choice).length + nodes.length;
        if (errors.length) status.innerHTML = errors.map(esc).join('<br>');
        if (!left && !errors.length) {
          status.textContent = auto.checked ? 'Every file is in place. Generating…' : 'Every file is in place.';
          if (auto.checked) {
            replay().then(() => setTimeout(() => dialog.close(), 900)).catch((error) => { status.textContent = `Generate failed: ${error.message}`; });
          } else {
            go.hidden = false; go.disabled = false; go.textContent = 'Generate now';
            go.dataset.mode = 'generate';
          }
        } else if (!errors.length) {
          status.textContent = 'Done. The files left need a choice or a search in Models first.';
        }
      };
      if (!jobs.size) { finish(); return; }
      status.textContent = 'Downloading… You can close this; downloads go on in Models → Manager → Downloads.';
      const poll = async () => {
        if (!dialog.isConnected) return;
        let data;
        try { data = await api(`/api/required-files/jobs?ids=${[...jobs.keys()].join(',')}`); } catch { setTimeout(poll, 3000); return; }
        let running = 0;
        for (const job of data.jobs) {
          const row = jobs.get(job.id);
          if (!row || row.done) continue;
          const src = dialog.querySelector(`.mf-row[data-i="${rows.indexOf(row)}"] .mf-src`);
          if (job.state === 'complete') { row.done = 'downloaded'; src.textContent = 'Downloaded.'; continue; }
          if (['failed', 'cancelled'].includes(job.state)) { row.done = 'failed'; row.choice = null; errors.push(`${base(row.name)}: ${job.error || job.state}`); src.textContent = `Download ${job.state}${job.error ? `: ${job.error}` : ''}`; continue; }
          running += 1;
          const pct = job.size ? Math.min(100, Math.floor((job.bytes / job.size) * 100)) : 0;
          src.innerHTML = `${job.retry ? 'Retrying' : job.state === 'queued' ? 'Waiting to download' : 'Downloading'} · ${gb(job.bytes) || '0 MB'}${job.size ? ` of ${gb(job.size)}` : ''}${job.speed ? ` · ${gb(job.speed)}/s` : ''}<span class="mf-bar"><span style="width:${pct}%"></span></span>`;
        }
        if (running) setTimeout(poll, 1500); else finish();
      };
      poll();
    });
  }

  // Catch the studio's 409 on any request (Generate buttons, presets, retries) without touching each app's code.
  window.fetch = async (input, init) => {
    const response = await nativeFetch(input, init);
    if (response.status === 409 && (response.headers.get('content-type') || '').includes('json')) {
      response.clone().json().then((data) => {
        if (data?.error !== CODE || !data.missing) return;
        const { signal, ...rest } = init || {};
        const replay = async () => {
          const again = await nativeFetch(input, rest);
          if (!again.ok) {
            const body = await again.json().catch(() => ({}));
            if (body?.error === CODE) { show(body.missing, body.studio, replay); return; }
            throw new Error(body.message || `Generate answered ${again.status}`);
          }
        };
        show(data.missing, data.studio, replay);
      }).catch(() => {});
    }
    return response;
  };
  window.RocsyMissingFiles = { show };
})();
