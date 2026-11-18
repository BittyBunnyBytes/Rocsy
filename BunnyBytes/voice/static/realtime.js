// Live voice: talk into the microphone and hear, record or route yourself in a Rocsy voice.
// Streams over the gateway's WebSocket /Voice/v1/realtime (voice/realtime/README.md), so it works
// wherever this page does. Nothing plays unless "Play the converted voice" is ticked.
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const root = $('live-panel');
  if (!root) return;

  const PRESETS = {
    fast: {block_ms: 150, diffusion_steps: 6, prompt_seconds: 3, cfg_rate: 0.7},
    balanced: {block_ms: 200, diffusion_steps: 6, prompt_seconds: 3, cfg_rate: 0.7},
    best: {block_ms: 300, diffusion_steps: 10, prompt_seconds: 3, cfg_rate: 0.7},
  };
  const RATES = [16000, 22050, 24000, 32000, 44100, 48000];
  const STORE = 'voice.live';
  const saved = (() => { try { return JSON.parse(localStorage.getItem(STORE) || '{}'); } catch { return {}; } })();

  const state = {
    voices: [], voice: saved.voice || '', kind: 'all', search: '',
    tune: {...PRESETS.balanced, ...(saved.tune || {})},
    phase: 'off',              // off | connecting | waiting | live
    muted: false, testing: false, calibrating: null,
    route: ['off', 'mic', 'ears'].includes(saved.route) ? saved.route : 'off',   // where the converted voice goes
    micSink: '', micReady: false, defaultOn: false, beat: 0,
    api: !!saved.api, channel: /^[A-Za-z0-9_-]{1,40}$/.test(saved.channel || '') ? saved.channel
      : `rocsy-${Math.random().toString(36).slice(2, 6)}`, listeners: 0,
    hear: !!saved.hear,   // also play a copy on the headphones (while sending to the mic or the API)
    started: 0, stats: null,
    // Adaptive (Custom preset only): the custom values are the ceiling; eff holds the lowered ones while the GPU is behind.
    adaptive: !!saved.adaptive, eff: null, adaptWait: 0, adaptCalm: 0, adaptNeed: 3, adaptRaised: 0,
    adaptFloor: false, adaptSent: 0,
  };
  // Audio graph (shared by "Test mic" and a live session).
  let ctx = null, stream = null, source = null, capture = null, player = null, gain = null, rate = 48000, frame = 960;
  let hearGain = null, hearDest = null;   // the headphone copy: player -> hearGain -> <audio> on the headphones
  let ws = null, pending = [], pendingLength = 0;
  let inPeak = 0, outPeak = 0, meterTimer = 0, clock = 0;
  let recording = null;      // {converted: Int16Array[], original: Int16Array[] | null, length}

  const remember = () => {
    try {
      localStorage.setItem(STORE, JSON.stringify({voice: state.voice, tune: state.tune, mic: $('live-mic').value,
        output: $('live-output').value, silence: Number($('live-silence').value), volume: Number($('live-volume').value),
        ns: $('live-ns').checked, recordMode: $('live-record-mode').value, route: state.route,
        defaultMic: $('live-default-mic').checked, api: state.api, channel: state.channel,
        hear: state.hear, hearVolume: Number($('live-hear-volume').value), adaptive: state.adaptive}));
    } catch { /* private window */ }
  };
  const status = (text, kind = '') => { $('live-status').textContent = text; $('live-status').dataset.kind = kind; };
  const setPhase = (phase, label) => {
    state.phase = phase;
    root.dataset.state = phase;
    $('live-pill').textContent = label || {off: 'Off', connecting: 'Connecting', waiting: 'Waiting for GPU', live: 'Live'}[phase];
    const busy = phase !== 'off';
    $('live-start').textContent = busy ? 'Stop' : 'Start';
    $('live-start').disabled = !busy && !currentVoice();
    $('live-mute').disabled = !busy && !state.testing;
    $('live-record').disabled = phase !== 'live';
    if ($('live-apibox')) showApi();
    $('live-block').disabled = busy;
    showTune();
  };

  // ---- voices: realtime list + the studio's portraits / emoji / samples ----
  const currentVoice = () => state.voices.find(v => v.id === state.voice);

  async function loadVoices() {
    $('live-voice-grid').replaceChildren(Object.assign(document.createElement('p'), {className: 'field-hint', textContent: 'Loading voices…'}));
    try {
      const [rt, studio] = await Promise.all([
        fetch('/Voice/v1/realtime/voices', {credentials: 'same-origin'}).then(async r => {
          const data = await r.json();
          if (!r.ok) throw new Error(data.message || `HTTP ${r.status}`);
          return data.voices;
        }),
        fetch('/Voice/api/state', {credentials: 'same-origin'}).then(r => (r.ok ? r.json() : {})).catch(() => ({})),
      ]);
      const info = new Map((studio.voices || []).map(v => [`voice:${v.id}`, v]));
      state.voices = rt.map(v => {
        const extra = info.get(v.id) || {};
        const bare = v.id.split(':')[1];
        return {...v, icon: extra.icon || (v.kind === 'music' ? '🎵' : ''), portrait: extra.portrait || '',
          category: v.kind === 'music' ? 'Music voice' : (extra.category && extra.category !== 'Uncategorized' ? extra.category : ''), summary: extra.summary || '',
          sample: v.kind === 'music' ? `/Music/api/voices/${encodeURIComponent(bare)}/audio` : (extra.reference || `/Voice/api/voices/${encodeURIComponent(bare)}/reference`)};
      });
      if (!currentVoice()) state.voice = state.voices[0]?.id || '';
      renderVoices();
      showVoice();
      if (!state.voices.length) status('No voices yet: add a character first (Add character tab).', 'warn');
      else if (state.phase === 'off' && $('live-status').dataset.kind === 'error') status('Pick a voice and press Start.');
    } catch (error) {
      state.voices = [];
      renderVoices();
      showVoice();
      status(`Real-time voice is not available (${error.message}). Is rocsy-voice-realtime running?`, 'error');
    }
  }

  function avatar(voice, cls) {
    const box = document.createElement('div');
    box.className = cls;
    if (voice?.portrait) {
      const img = document.createElement('img');
      img.src = voice.portrait; img.alt = ''; img.loading = 'lazy'; img.decoding = 'async';
      box.append(img);
    } else {
      box.textContent = voice?.icon || (voice?.name || '?').trim().charAt(0).toUpperCase();
      box.classList.add('is-glyph');
    }
    return box;
  }

  function renderVoices() {
    const grid = $('live-voice-grid');
    const needle = state.search.trim().toLowerCase();
    const shown = state.voices.filter(v => (state.kind === 'all' || v.kind === state.kind) &&
      (!needle || `${v.name} ${v.category} ${v.summary}`.toLowerCase().includes(needle)));
    if (!shown.length) {
      grid.replaceChildren(Object.assign(document.createElement('p'), {className: 'field-hint',
        textContent: state.voices.length ? 'No voice matches.' : 'No voices.'}));
      return;
    }
    grid.replaceChildren(...shown.map(v => {
      const card = document.createElement('div');
      card.className = 'live-voice';
      card.setAttribute('role', 'option');
      card.setAttribute('aria-selected', String(v.id === state.voice));
      card.tabIndex = 0;
      card.dataset.id = v.id;
      const text = document.createElement('div');
      text.className = 'live-voice-text';
      const name = document.createElement('strong'); name.textContent = v.name;
      const sub = document.createElement('span');
      sub.textContent = [v.category, v.seconds ? `${Math.round(v.seconds)} s sample` : ''].filter(Boolean).join(' · ');
      text.append(name, sub);
      const play = document.createElement('button');
      play.type = 'button'; play.className = 'live-voice-play'; play.title = `Play ${v.name}'s sample`;
      play.setAttribute('aria-label', play.title);
      play.textContent = '▶';
      play.addEventListener('click', event => { event.stopPropagation(); preview(v, play); });
      card.append(avatar(v, 'live-voice-art'), text, play);
      const pick = () => chooseVoice(v.id);
      card.addEventListener('click', pick);
      card.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); pick(); } });
      return card;
    }));
  }

  function showVoice() {
    const v = currentVoice();
    $('live-avatar').replaceWith(Object.assign(avatar(v, 'live-avatar'), {id: 'live-avatar'}));
    $('live-voice-name').textContent = v ? v.name : (state.voices.length ? 'Pick a voice' : 'No voices');
    $('live-voice-sub').textContent = v ? [v.category, v.summary].filter(Boolean).join(' · ') : '';
    $('live-voice-grid').querySelectorAll('.live-voice').forEach(card => card.setAttribute('aria-selected', String(card.dataset.id === state.voice)));
    if (state.phase === 'off') $('live-start').disabled = !v;
  }

  function chooseVoice(id) {
    if (id === state.voice) return;
    state.voice = id;
    remember();
    showVoice();
    if (state.phase === 'live' && ws) {
      ws.send(JSON.stringify({type: 'voice', voice: id}));
      status(`Switching to ${currentVoice()?.name}…`);
    }
  }

  // Sample preview: one shared player, only on a click.
  function preview(voice, button) {
    const audio = $('live-preview');
    const playing = !audio.paused && audio.dataset.id === voice.id;
    audio.pause();
    root.querySelectorAll('.live-voice-play.is-playing').forEach(b => { b.classList.remove('is-playing'); b.textContent = '▶'; });
    if (playing) return;
    audio.src = voice.sample; audio.dataset.id = voice.id;
    audio.play().then(() => { button.classList.add('is-playing'); button.textContent = '■'; })
      .catch(() => status(`Couldn't play ${voice.name}'s sample.`, 'warn'));
    audio.onended = () => { button.classList.remove('is-playing'); button.textContent = '▶'; };
  }

  // ---- devices ----
  const canRoute = typeof AudioContext !== 'undefined' && 'setSinkId' in AudioContext.prototype;
  const isRocsyDevice = label => /rocsy[-_ ]voice/i.test(label || '');

  async function loadDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const devices = await navigator.mediaDevices.enumerateDevices();
    const fill = (select, kind, fallback, wanted) => {
      select.replaceChildren(new Option(fallback, ''), ...devices.filter(d => d.kind === kind && d.deviceId && d.deviceId !== 'default')
        .map((d, i) => new Option(d.label || `${kind === 'audioinput' ? 'Microphone' : 'Output'} ${i + 1}`, d.deviceId)));
      if (wanted && [...select.options].some(o => o.value === wanted)) select.value = wanted;
    };
    fill($('live-mic'), 'audioinput', 'Default microphone', $('live-mic').value || saved.mic);
    fill($('live-output'), 'audiooutput', 'Default output', $('live-output').value || saved.output);
    // The Rocsy Voice devices are never offered as the input (it would convert its own output) or as headphones.
    [...$('live-mic').options, ...$('live-output').options].forEach(o => { if (isRocsyDevice(o.textContent)) o.remove(); });
    return devices;
  }

  // The real microphone to capture: never Rocsy Voice, and a concrete device rather than "default",
  // so making Rocsy Voice the default microphone can't move this page's input onto its own output.
  async function realMicId() {
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
    const chosen = $('live-mic').value;
    if (chosen) return chosen;
    const def = devices.find(d => d.deviceId === 'default');
    const name = (def?.label || '').replace(/^Default\s*-\s*/i, '');
    const real = devices.filter(d => d.deviceId && d.deviceId !== 'default' && d.deviceId !== 'communications' && !isRocsyDevice(d.label));
    return (real.find(d => d.label === name) || (isRocsyDevice(name) ? real[0] : null))?.deviceId || '';
  }

  // ---- audio graph ----
  async function openAudio() {
    if (ctx) return;
    const constraints = mic => ({audio: {deviceId: mic ? {exact: mic} : undefined, channelCount: 1,
      echoCancellation: false, autoGainControl: false, noiseSuppression: $('live-ns').checked}});
    stream = await navigator.mediaDevices.getUserMedia(constraints($('live-mic').value));
    await loadDevices().catch(() => {});   // labels appear once the microphone is allowed
    const real = await realMicId().catch(() => '');
    const track = stream.getAudioTracks()[0];
    if (real && track && (track.getSettings().deviceId !== real || isRocsyDevice(track.label))) {
      stream.getTracks().forEach(t => t.stop());
      stream = await navigator.mediaDevices.getUserMedia(constraints(real));
    }
    if (isRocsyDevice(stream.getAudioTracks()[0]?.label)) {
      stream.getTracks().forEach(t => t.stop()); stream = null;
      throw new Error('the microphone is Rocsy Voice itself; pick your real microphone');
    }
    ctx = new AudioContext({sampleRate: 48000, latencyHint: 'interactive'});
    rate = RATES.includes(ctx.sampleRate) ? ctx.sampleRate : 48000;
    frame = rate / 50;
    await ctx.audioWorklet.addModule('/Voice/static/realtime-worklet.js?v=2');
    source = ctx.createMediaStreamSource(stream);
    capture = new AudioWorkletNode(ctx, 'rocsy-capture', {numberOfInputs: 1, numberOfOutputs: 0});
    player = new AudioWorkletNode(ctx, 'rocsy-player', {numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1]});
    gain = ctx.createGain();
    gain.gain.value = Number($('live-volume').value) / 100;
    player.connect(gain);
    // A second path for "Let me hear my converted voice": an AudioContext plays on one device only, so
    // the copy goes through a media element that can pick the headphones itself.
    hearGain = ctx.createGain();
    hearGain.gain.value = Number($('live-hear-volume').value) / 100;
    hearDest = ctx.createMediaStreamDestination();
    player.connect(hearGain);
    hearGain.connect(hearDest);
    source.connect(capture);
    capture.port.onmessage = ({data}) => onMic(data);
    await applyRoute();
    clearInterval(meterTimer);
    meterTimer = setInterval(drawMeters, 80);
  }

  function closeAudio() {
    clearInterval(meterTimer);
    try { source?.disconnect(); capture?.disconnect(); player?.disconnect(); gain?.disconnect(); hearGain?.disconnect(); } catch { /* gone */ }
    const hearing = $('live-monitor-audio');
    hearing.pause(); hearing.srcObject = null;
    hearGain = hearDest = null;
    stream?.getTracks().forEach(track => track.stop());
    ctx?.close().catch(() => {});
    ctx = stream = source = capture = player = gain = null;
    inPeak = outPeak = 0;
    drawMeters();
  }

  // Where the converted voice goes. "Nowhere" (the default) plays nothing; "My microphone" plays only into
  // the Rocsy Voice device and never falls back to the speakers if that device can't be used.
  const hearing = () => state.hear && state.route !== 'ears';
  const playing = () => state.route === 'ears' || (state.route === 'mic' && state.micReady) || hearing();
  async function applyHear() {
    const el = $('live-monitor-audio');
    if (!hearDest || !hearing()) { el.pause(); el.srcObject = null; return; }
    try {
      if (el.setSinkId) await el.setSinkId($('live-output').value || '');
      if (el.srcObject !== hearDest.stream) el.srcObject = hearDest.stream;
      await el.play();
    } catch (error) { status(`Couldn't play it on your headphones: ${error.message}`, 'warn'); }
  }
  async function applyRoute() {
    if (!gain || !ctx) return;
    try { gain.disconnect(); } catch { /* not connected */ }
    let sink = null;
    if (state.route === 'ears') sink = $('live-output').value || '';
    else if (state.route === 'mic' && state.micReady && state.micSink) sink = state.micSink;
    await applyHear();
    if (sink === null) { if (!hearing()) player?.port.postMessage('clear'); return; }
    if (canRoute) {
      try { await ctx.setSinkId(sink); } catch (error) {
        player?.port.postMessage('clear');
        status(`Couldn't use that output: ${error.message}`, 'error');
        return;
      }
    } else if (state.route === 'mic') {
      return status('This browser can\'t choose where audio plays; use Chrome/Edge, or the Rocsy Voice client.', 'error');
    }
    gain.connect(ctx.destination);
  }

  // ---- microphone frames ----
  function onMic(samples) {
    if (state.muted) samples.fill(0);
    let sum = 0;
    for (let i = 0; i < samples.length; i++) { const a = Math.abs(samples[i]); inPeak = Math.max(inPeak, a); sum += a * a; }
    if (state.calibrating) state.calibrating.push(Math.sqrt(sum / samples.length));
    if (state.phase !== 'live' || !ws || ws.readyState !== WebSocket.OPEN) return;
    pending.push(samples); pendingLength += samples.length;
    if (pendingLength < frame) return;
    const pcm = new Int16Array(pendingLength);
    let at = 0;
    for (const chunk of pending) for (let i = 0; i < chunk.length; i++) pcm[at++] = Math.max(-1, Math.min(1, chunk[i])) * 32767;
    pending = []; pendingLength = 0;
    ws.send(pcm.buffer);
    if (recording?.original) recording.original.push(pcm);
  }

  function onAudio(buffer) {
    const pcm = new Int16Array(buffer);
    const samples = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) { samples[i] = pcm[i] / 32768; outPeak = Math.max(outPeak, Math.abs(samples[i])); }
    if (playing() && player) player.port.postMessage(samples, [samples.buffer]);
    if (recording) {
      recording.converted.push(pcm); recording.length += pcm.length;
      $('live-record-time').textContent = `● ${(recording.length / rate).toFixed(1)} s`;
      if (recording.length > rate * 300) stopRecording();   // 5 minutes
    }
  }

  // Meters on a -80..0 dB scale; the input meter shows the silence threshold as a line.
  const dbWidth = db => `${Math.max(0, Math.min(100, ((db + 80) / 80) * 100))}%`;
  const toDb = peak => (peak > 0 ? 20 * Math.log10(peak) : -100);
  function drawMeters() {
    $('live-in-meter').style.width = dbWidth(toDb(inPeak));
    $('live-out-meter').style.width = dbWidth(toDb(outPeak));
    $('live-in-meter').classList.toggle('is-open', toDb(inPeak) > Number($('live-silence').value));
    inPeak *= 0.55; outPeak *= 0.55;
  }

  // ---- live session ----
  async function start() {
    const voice = currentVoice();
    if (!voice) return;
    setPhase('connecting');
    status('Opening the microphone…');
    try {
      await openAudio();
    } catch (error) {
      status(error.name === 'NotAllowedError' ? 'The browser blocked the microphone. Allow it for this site and try again.'
        : `Couldn't start the microphone: ${error.message}`, 'error');
      setPhase('off');
      return;
    }
    status('Connecting…');
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${scheme}://${location.host}/Voice/v1/realtime`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => ws.send(JSON.stringify({voice: voice.id, sample_rate: rate, ...state.tune,
      silence_db: Number($('live-silence').value), ...(state.api ? {publish: state.channel} : {})}));
    ws.onmessage = ({data}) => (typeof data === 'string' ? onInfo(JSON.parse(data)) : onAudio(data));
    ws.onclose = event => {
      const was = state.phase;
      ws = null;
      endSession();
      if (was !== 'off') {
        status(event.code === 4401 || event.code === 4403 ? 'Not allowed: log in to Rocsy again.'
          : was === 'live' ? 'Disconnected.' : 'Could not connect to real-time voice.', 'error');
      }
    };
  }

  function onInfo(info) {
    if (info.type === 'waiting') {
      setPhase('waiting');
      status(`Another job is using the GPU (queue position ${info.position ?? '?'}). Live voice starts when it's done.`, 'warn');
    } else if (info.type === 'ready') {
      setPhase('live');
      state.started = Date.now();
      clearInterval(clock);
      clock = setInterval(showClock, 500);
      status(`Live. Speak normally; about ${info.latency_ms} ms delay plus conversion. ` + ({
        off: 'Not sent anywhere yet: choose "My microphone" or "My headphones" under Output.',
        mic: (state.micReady ? 'Going into the Rocsy Voice microphone.' : 'The Rocsy Voice microphone isn\'t ready.') +
          (state.hear ? ' You hear it too.' : ''),
        ears: 'Playing on your headphones.'}[state.route]), state.route === 'mic' && !state.micReady ? 'warn' : 'ok');
      syncDefaultMic();
    } else if (info.type === 'stats') {
      state.stats = info;
      adapt(info.realtime_factor);
      $('live-stats').textContent = ` · ≈${info.latency_ms} ms · GPU ${Math.round(info.realtime_factor * 100)}%` +
        (adaptiveOn() && state.eff ? ` · adaptive ${state.eff.diffusion_steps} steps` : '');
      $('live-gpu-warning').textContent = adaptiveOn()
        ? (state.adaptFloor && info.realtime_factor > 0.9
          ? 'Even the lowest adaptive settings can\'t keep up: use a bigger block size (applies when you start again).' : '')
        : info.realtime_factor > 0.9 ? 'The GPU can barely keep up: pick Fastest or fewer quality steps.' : '';
    } else if (info.type === 'voice') {
      status(`Now speaking as ${info.voice.name}.`, 'ok');
    } else if (info.type === 'settings') {
      if (state.adaptSent > 0) state.adaptSent--;   // adaptive changes stay quiet; the live bar shows them
      else status('Settings applied.', 'ok');
    } else if (info.type === 'listeners' || info.type === 'publish') {
      state.listeners = info.type === 'listeners' ? info.count : (info.listeners || 0);
      if (info.type === 'publish' && !info.channel) state.listeners = 0;
      showApi();
    } else if (info.type === 'error') {
      status(info.message, info.fatal === false ? 'warn' : 'error');
    }
  }

  function showClock() {
    const s = Math.floor((Date.now() - state.started) / 1000);
    $('live-timer').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  function endSession() {
    clearInterval(clock);
    setDefaultMic(false);
    if (recording) stopRecording();
    if (ws) { ws.onclose = null; try { ws.close(); } catch { /* closed */ } ws = null; }
    pending = []; pendingLength = 0;
    resetAdapt();
    $('live-timer').textContent = ''; $('live-stats').textContent = '';
    state.listeners = 0;
    if (!state.testing) closeAudio();
    setPhase('off');
  }

  function stop() {
    if (state.phase === 'off' && !state.testing) return;
    state.testing = false;
    $('live-mic-test').setAttribute('aria-pressed', 'false');
    endSession();
    closeAudio();
    setMuted(false);
    status('Stopped.');
  }

  $('live-start').addEventListener('click', () => {
    if (state.phase === 'off') return start();
    const testing = state.testing;
    endSession();
    status(testing ? 'Stopped converting; the mic test is still on.' : 'Stopped.');
  });

  // ---- mute / mic test / calibration ----
  function setMuted(muted) {
    state.muted = muted;
    $('live-mute').setAttribute('aria-pressed', String(muted));
    $('live-mute').textContent = muted ? 'Unmute' : 'Mute';
    root.classList.toggle('is-muted', muted);
  }
  $('live-mute').addEventListener('click', () => setMuted(!state.muted));

  $('live-mic-test').addEventListener('click', async () => {
    if (state.testing) {
      state.testing = false;
      $('live-mic-test').setAttribute('aria-pressed', 'false');
      if (state.phase === 'off') closeAudio();
      return;
    }
    try {
      await openAudio();
      state.testing = true;
      $('live-mic-test').setAttribute('aria-pressed', 'true');
      $('live-mute').disabled = false;
      status('Mic test: watch the level. Nothing is converted or sent.');
    } catch (error) {
      status(`Couldn't open the microphone: ${error.message}`, 'error');
    }
  });

  $('live-calibrate').addEventListener('click', async () => {
    try { await openAudio(); } catch (error) { return status(`Couldn't open the microphone: ${error.message}`, 'error'); }
    state.calibrating = [];
    $('live-calibrate').disabled = true;
    $('live-calibrate-hint').textContent = 'Measuring… stay quiet.';
    setTimeout(() => {
      const levels = (state.calibrating || []).filter(x => x > 0).sort((a, b) => a - b);
      state.calibrating = null;
      $('live-calibrate').disabled = false;
      if (state.phase === 'off' && !state.testing) closeAudio();
      if (!levels.length) { $('live-calibrate-hint').textContent = 'No sound came in; check the microphone.'; return; }
      const noise = toDb(levels[Math.floor(levels.length * 0.9)]);   // loud end of the background
      const threshold = Math.round(Math.max(-80, Math.min(-20, noise + 8)));
      $('live-silence').value = threshold;
      showSilence(); applySilence();
      $('live-calibrate-hint').textContent = `Background ≈ ${Math.round(noise)} dB → threshold ${threshold} dB.`;
    }, 2000);
  });

  // ---- silence / output / volume ----
  const showSilence = () => {
    $('live-silence-value').textContent = `${$('live-silence').value} dB`;
    $('live-threshold-mark').style.left = dbWidth(Number($('live-silence').value));
  };
  const applySilence = () => {
    remember();
    if (state.phase === 'live' && ws) ws.send(JSON.stringify({type: 'settings', silence_db: Number($('live-silence').value)}));
  };
  $('live-silence').addEventListener('input', showSilence);
  $('live-silence').addEventListener('change', applySilence);
  $('live-output').addEventListener('change', () => { remember(); applyRoute(); });
  $('live-hear').addEventListener('change', () => {
    state.hear = $('live-hear').checked; remember(); showRoute(); applyRoute();
    if (state.phase === 'live') status(state.hear ? 'You hear your converted voice on your headphones.' : 'No longer playing it to you.', 'ok');
  });
  $('live-hear-volume').addEventListener('input', () => {
    $('live-hear-volume-value').textContent = `${$('live-hear-volume').value}%`;
    if (hearGain) hearGain.gain.value = Number($('live-hear-volume').value) / 100;
  });
  $('live-hear-volume').addEventListener('change', remember);
  $('live-volume').addEventListener('input', () => {
    $('live-volume-value').textContent = `${$('live-volume').value}%`;
    if (gain) gain.gain.value = Number($('live-volume').value) / 100;
  });
  $('live-volume').addEventListener('change', remember);
  $('live-mic').addEventListener('change', () => { remember(); if (ctx) status('The new microphone is used from the next start.', 'warn'); });
  $('live-ns').addEventListener('change', () => { remember(); if (ctx) status('Noise suppression changes from the next start.', 'warn'); });

  // ---- advanced: presets and fine tuning ----
  const LIVE_KEYS = ['diffusion_steps', 'prompt_seconds', 'cfg_rate'];   // the server applies these mid-stream
  function presetOf(t) {
    return Object.keys(PRESETS).find(k => Object.entries(PRESETS[k]).every(([key, v]) => t[key] === v)) || 'custom';
  }
  const adaptiveOn = () => state.adaptive && presetOf(state.tune) === 'custom';
  // The values the server is running with right now (the custom ones, or adaptive's lowered ones).
  const liveValues = () => ({...Object.fromEntries(LIVE_KEYS.map(k => [k, state.tune[k]])), ...(adaptiveOn() ? state.eff : null)});
  function sendLive(running, quiet = false) {
    if (state.phase !== 'live' || !ws) return;
    const now = liveValues();
    const diff = Object.fromEntries(LIVE_KEYS.filter(k => now[k] !== running[k]).map(k => [k, now[k]]));
    if (!Object.keys(diff).length) return;
    if (quiet) state.adaptSent++;
    ws.send(JSON.stringify({type: 'settings', ...diff}));
  }
  function resetAdapt() {
    Object.assign(state, {eff: null, adaptWait: 0, adaptCalm: 0, adaptNeed: 3, adaptRaised: 0, adaptFloor: false});
  }

  // Runs on every stats message (~3 s; GPU % = conversion time / block length over the last ~20 blocks).
  // Behind (>= 85%): lower quality steps, then voice match. Room (<= 70% several times in a row): step back up
  // toward the custom values. Lowering right after a raise makes the next raise wait longer, so it settles.
  function adapt(rtf) {
    if (!adaptiveOn() || state.phase !== 'live' || !Number.isFinite(rtf)) return;
    state.adaptRaised = Math.max(0, state.adaptRaised - 1);
    if (state.adaptWait > 0) { state.adaptWait--; return; }   // the average still holds blocks from before the change
    const top = {diffusion_steps: state.tune.diffusion_steps, prompt_seconds: state.tune.prompt_seconds};
    const cur = {...top, ...(state.eff || {})};
    let next = null;
    if (rtf >= 0.85) {
      state.adaptCalm = 0;
      const minSteps = Math.min(3, top.diffusion_steps), minPrompt = Math.min(1.5, top.prompt_seconds);
      if (cur.diffusion_steps > minSteps) next = {...cur, diffusion_steps: Math.max(minSteps, cur.diffusion_steps - (cur.diffusion_steps > 8 ? 2 : 1))};
      else if (cur.prompt_seconds > minPrompt) next = {...cur, prompt_seconds: Math.max(minPrompt, cur.prompt_seconds - 1)};
      else state.adaptFloor = true;
      if (next && state.adaptRaised) state.adaptNeed = Math.min(20, state.adaptNeed * 2);
    } else if (rtf <= 0.7 && state.eff) {
      if (++state.adaptCalm < state.adaptNeed) return;
      state.adaptCalm = 0;
      if (cur.prompt_seconds < top.prompt_seconds) next = {...cur, prompt_seconds: Math.min(top.prompt_seconds, cur.prompt_seconds + 1)};
      else if (cur.diffusion_steps < top.diffusion_steps) next = {...cur, diffusion_steps: cur.diffusion_steps + 1};
      if (next) state.adaptRaised = 3;
    } else {
      state.adaptCalm = 0;
    }
    if (!next) return;
    const running = liveValues();
    state.adaptFloor = false;
    state.eff = next.diffusion_steps === top.diffusion_steps && next.prompt_seconds === top.prompt_seconds ? null : next;
    state.adaptWait = 1;
    sendLive(running, true);
    showTune();
  }

  function showTune() {
    const t = state.tune, preset = presetOf(t);
    root.querySelectorAll('[data-live-quality]').forEach(chip => chip.setAttribute('aria-pressed', String(chip.dataset.liveQuality === preset)));
    $('live-adaptive-row').hidden = preset !== 'custom';
    $('live-adaptive').checked = state.adaptive;
    $('live-block').value = t.block_ms; $('live-block-value').textContent = `${t.block_ms} ms`;
    $('live-steps').value = t.diffusion_steps; $('live-steps-value').textContent = String(t.diffusion_steps);
    $('live-prompt').value = t.prompt_seconds; $('live-prompt-value').textContent = `${t.prompt_seconds} s`;
    $('live-cfg').value = t.cfg_rate; $('live-cfg-value').textContent = Number(t.cfg_rate).toFixed(2);
    const gpu = Math.round(100 * (55 + 9 * t.diffusion_steps) * (1 + (t.prompt_seconds - 3) * 0.08) / t.block_ms);
    $('live-quality-hint').textContent = `About ${t.block_ms + 180} ms delay, roughly ${gpu}% of the GPU's time` +
      (state.phase !== 'off' ? '. Block size changes when you start again.' : '.') +
      (adaptiveOn() && state.eff ? ` Adaptive is running lower right now: ${state.eff.diffusion_steps} steps, ${state.eff.prompt_seconds} s voice match.` : '');
  }
  function setTune(change) {
    const running = liveValues();
    state.tune = {...state.tune, ...change};
    resetAdapt();   // new custom values are the new ceiling; adaptive starts again from them
    showTune(); remember();
    sendLive(running);
  }
  $('live-adaptive').addEventListener('change', () => {
    const running = liveValues();
    state.adaptive = $('live-adaptive').checked;
    resetAdapt();
    showTune(); remember();
    sendLive(running);
    if (state.phase === 'live') status(state.adaptive ? 'Adaptive on: it lowers settings if the GPU falls behind.' : 'Adaptive off: back to your custom values.', 'ok');
  });
  root.querySelectorAll('[data-live-quality]').forEach(chip => chip.addEventListener('click', () => {
    if (PRESETS[chip.dataset.liveQuality]) setTune(state.phase === 'off' ? PRESETS[chip.dataset.liveQuality]
      : Object.fromEntries(Object.entries(PRESETS[chip.dataset.liveQuality]).filter(([k]) => k !== 'block_ms')));
  }));
  $('live-block').addEventListener('input', () => setTune({block_ms: Number($('live-block').value)}));
  for (const [id, key] of [['live-steps', 'diffusion_steps'], ['live-prompt', 'prompt_seconds'], ['live-cfg', 'cfg_rate']]) {
    $(id).addEventListener('input', () => {   // label follows the slider; the server gets the value on release
      const v = Number($(id).value);
      $(`${id}-value`).textContent = key === 'cfg_rate' ? v.toFixed(2) : key === 'prompt_seconds' ? `${v} s` : String(v);
    });
    $(id).addEventListener('change', () => setTune({[key]: Number($(id).value)}));
  }
  $('live-reset').addEventListener('click', () => setTune(state.phase === 'off' ? PRESETS.balanced
    : {diffusion_steps: 6, prompt_seconds: 3, cfg_rate: 0.7}));

  // ---- output route: nowhere / my microphone / my headphones ----
  const ROUTE_HINT = {
    off: 'Nothing plays or leaves this page. Pick where the converted voice should go.',
    mic: 'Becomes a microphone called "Rocsy Voice" on this computer, for Discord, games, Signal… It only plays for you if you tick the box below.',
    ears: 'Plays on your headphones so you can hear yourself.',
  };
  const action = (path, body) => fetch(`/voice-mic/${path}`, {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-BB-Action': '1'}, body: JSON.stringify(body || {})})
    .then(async r => { const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`); return d; });

  function showRoute() {
    root.querySelectorAll('[data-live-route]').forEach(c => c.setAttribute('aria-pressed', String(c.dataset.liveRoute === state.route)));
    $('live-route-hint').textContent = ROUTE_HINT[state.route];
    $('live-micbox').hidden = state.route !== 'mic';
    $('live-hear-row').hidden = state.route === 'ears';        // "My headphones" already plays it to you
    $('live-hear').checked = state.hear;
    $('live-hear-volume-row').hidden = !hearing();
    $('live-output-row').hidden = !(state.route === 'ears' || hearing()) || !canRoute;
    $('live-volume-label').firstChild.textContent = state.route === 'mic' ? 'Microphone volume ' : 'Volume ';
    root.dataset.route = state.route;
  }

  function micState(text, kind = '') { $('live-mic-state').textContent = text; $('live-mic-state').dataset.kind = kind; }

  // Make sure the Rocsy Voice microphone exists and find its output device in this browser.
  async function prepareMic() {
    state.micReady = false; state.micSink = '';
    micState('Setting up the Rocsy Voice microphone…');
    let info = await fetch('/voice-mic', {credentials: 'same-origin'}).then(r => r.json()).catch(() => ({available: false, error: 'Rocsy is unreachable'}));
    if (info.remote || !info.available) { micState(info.error || 'Not available here.', 'error'); return; }
    if (!info.exists) info = await action('create').catch(error => ({error: error.message}));
    if (info.error) { micState(`Couldn't create it: ${info.error}`, 'error'); return; }
    // Device names need the microphone permission; ask once if this page doesn't have it yet.
    let devices = await navigator.mediaDevices.enumerateDevices();
    if (!devices.some(d => d.label)) {
      try { (await navigator.mediaDevices.getUserMedia({audio: true})).getTracks().forEach(t => t.stop()); }
      catch { micState('Allow the microphone for this site first.', 'error'); return; }
    }
    for (let i = 0; i < 25 && !state.micSink; i++) {      // a new device shows up within a second or two
      devices = await loadDevices().catch(() => []) || [];
      state.micSink = devices.find(d => d.kind === 'audiooutput' && isRocsyDevice(d.label))?.deviceId || '';
      if (!state.micSink) await new Promise(r => setTimeout(r, 200));
    }
    if (!state.micSink) { micState('The browser doesn\'t list the Rocsy Voice output (Chrome/Edge on this computer are needed).', 'error'); return; }
    state.micReady = true;
    micState(info.is_default ? 'Rocsy Voice is ready and is your default microphone.' : 'Rocsy Voice is ready.', 'ok');
    await applyRoute();
  }

  async function setRoute(route) {
    if (route === state.route && route !== 'mic') return;
    if (state.route === 'mic' && route !== 'mic') setDefaultMic(false);
    state.route = route; showRoute(); remember();
    if (route === 'mic') await prepareMic();
    await applyRoute();
    if (state.phase === 'live') syncDefaultMic();
  }
  root.querySelectorAll('[data-live-route]').forEach(chip => chip.addEventListener('click', () => setRoute(chip.dataset.liveRoute)));

  // "Default microphone while live": on only while live and routed to the mic; a heartbeat keeps it.
  function syncDefaultMic() {
    setDefaultMic(state.phase === 'live' && state.route === 'mic' && state.micReady && $('live-default-mic').checked);
  }
  function setDefaultMic(on) {
    if (on === state.defaultOn) return;
    state.defaultOn = on;
    clearInterval(state.beat);
    if (on) state.beat = setInterval(() => action('heartbeat').catch(() => {}), 10000);
    action('default', {on}).then(info => {
      if (state.route === 'mic') micState(on ? 'Rocsy Voice is your default microphone now.' :
        `Rocsy Voice is ready${info.default_source ? '; your default microphone is your own again' : ''}.`, 'ok');
    }).catch(error => { if (on) { state.defaultOn = false; clearInterval(state.beat); micState(`Couldn't make it the default: ${error.message}`, 'error'); } });
  }
  $('live-default-mic').addEventListener('change', () => { remember(); syncDefaultMic(); });
  $('live-mic-remove').addEventListener('click', async () => {
    setDefaultMic(false);
    try { await action('remove'); } catch (error) { return micState(`Couldn't remove it: ${error.message}`, 'error'); }
    state.micReady = false; state.micSink = '';
    await setRoute('off');
    await loadDevices().catch(() => {});
    status('Removed the Rocsy Voice microphone.');
  });

  // ---- 📡 API: publish the converted voice on a channel others can listen to ----
  function listenUrl() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${location.host}/Voice/v1/realtime/listen?channel=${encodeURIComponent(state.channel)}`;
  }
  function showApi() {
    $('live-api').setAttribute('aria-pressed', String(state.api));
    $('live-apibox').hidden = !state.api;
    if (document.activeElement !== $('live-channel')) $('live-channel').value = state.channel;
    $('live-api-url').textContent = listenUrl();
    $('live-listeners').textContent = state.phase !== 'live' ? 'Publishes while live'
      : `${state.listeners} listening`;
    $('live-listeners').dataset.on = String(state.phase === 'live' && state.api);
    $('live-api-snippet').textContent = [
      'import asyncio, json, websockets',
      '',
      'async def main():',
      `    url = "${listenUrl()}"`,
      '    headers = {"Authorization": "Bearer rk-..."}  # only needed from other programs / computers',
      '    async with websockets.connect(url, additional_headers=headers) as ws:',
      '        async for message in ws:',
      '            if isinstance(message, bytes):',
      '                pcm = message  # 16-bit little-endian mono at the hello\'s sample_rate',
      '            else:',
      '                print(json.loads(message))  # hello / voice / end / waiting',
      '',
      'asyncio.run(main())'].join('\n');
  }
  function publish() {
    if (state.phase === 'live' && ws) ws.send(JSON.stringify({type: 'publish', channel: state.api ? state.channel : null}));
  }
  $('live-api').addEventListener('click', () => { state.api = !state.api; remember(); showApi(); publish(); });
  $('live-channel').addEventListener('change', () => {
    const name = $('live-channel').value.trim();
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(name)) { $('live-channel').value = state.channel; return status('A channel name is 1-40 letters, digits, - or _.', 'warn'); }
    state.channel = name; remember(); showApi(); publish();
  });
  $('live-api-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(listenUrl()); $('live-api-copy').textContent = 'Copied'; }
    catch { $('live-api-copy').textContent = 'Select and copy'; }
    setTimeout(() => { $('live-api-copy').textContent = 'Copy'; }, 1500);
  });

  // ---- recording ----
  function wav(chunks, length) {
    const view = new DataView(new ArrayBuffer(44 + length * 2));
    const text = (at, s) => [...s].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    text(0, 'RIFF'); view.setUint32(4, 36 + length * 2, true); text(8, 'WAVEfmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    text(36, 'data'); view.setUint32(40, length * 2, true);
    let at = 44;
    for (const chunk of chunks) for (let i = 0; i < chunk.length && at < view.byteLength; i++, at += 2) view.setInt16(at, chunk[i], true);
    return new Blob([view], {type: 'audio/wav'});
  }
  function addRecording(label, chunks) {
    const length = chunks.reduce((n, c) => n + c.length, 0);
    if (!length) return;
    const url = URL.createObjectURL(wav(chunks, length));
    const item = document.createElement('div');
    item.className = 'live-recording';
    const tag = document.createElement('span');
    tag.className = `live-recording-tag is-${label === 'Original' ? 'original' : 'converted'}`;
    tag.textContent = label;
    const audio = document.createElement('audio');
    audio.controls = true; audio.src = url; audio.preload = 'metadata';
    const voice = currentVoice()?.name || 'voice';
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const link = Object.assign(document.createElement('a'), {href: url, className: 'secondary-button live-small', textContent: 'Download',
      download: `live-${label === 'Original' ? 'original' : voice.replace(/[^\w-]+/g, '_')}-${stamp}.wav`});
    const remove = Object.assign(document.createElement('button'), {type: 'button', className: 'secondary-button live-small', textContent: 'Delete'});
    remove.addEventListener('click', () => { URL.revokeObjectURL(url); item.remove(); $('live-recordings-card').hidden = !$('live-recordings').children.length; });
    item.append(tag, audio, link, remove);
    $('live-recordings').prepend(item);
    $('live-recordings-card').hidden = false;
  }
  function stopRecording() {
    const done = recording;
    recording = null;
    $('live-record').textContent = 'Record';
    $('live-record').classList.remove('is-recording');
    $('live-record-time').textContent = '';
    if (!done) return;
    if (done.original) addRecording('Original', done.original);
    addRecording('Converted', done.converted);
  }
  $('live-record').addEventListener('click', () => {
    if (recording) return stopRecording();
    recording = {converted: [], original: $('live-record-mode').value === 'both' ? [] : null, length: 0};
    $('live-record').textContent = 'Stop';
    $('live-record').classList.add('is-recording');
  });
  $('live-record-mode').addEventListener('change', remember);
  $('live-recordings-clear').addEventListener('click', () => {
    $('live-recordings').querySelectorAll('a').forEach(a => URL.revokeObjectURL(a.href));
    $('live-recordings').replaceChildren();
    $('live-recordings-card').hidden = true;
  });

  // ---- voice list controls, shortcuts ----
  $('live-voice-search').addEventListener('input', () => { state.search = $('live-voice-search').value; renderVoices(); });
  root.querySelectorAll('[data-live-kind]').forEach(chip => chip.addEventListener('click', () => {
    state.kind = chip.dataset.liveKind;
    root.querySelectorAll('[data-live-kind]').forEach(c => c.setAttribute('aria-pressed', String(c === chip)));
    renderVoices();
  }));
  $('live-voice-refresh').addEventListener('click', loadVoices);
  $('live-help-link').addEventListener('click', () => { $('live-help').open = true; });
  document.addEventListener('keydown', event => {
    if (document.body.dataset.view !== 'live' || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.closest('input, select, textarea, [contenteditable]')) return;
    if (event.key === 'm' || event.key === 'M') { if (!$('live-mute').disabled) setMuted(!state.muted); }
    else if (event.key === 'r' || event.key === 'R') { if (!$('live-record').disabled) $('live-record').click(); }
  });

  // ---- restore, hooks ----
  $('live-silence').value = Number.isFinite(saved.silence) ? saved.silence : -50;
  $('live-volume').value = Number.isFinite(saved.volume) ? saved.volume : 100;
  $('live-volume-value').textContent = `${$('live-volume').value}%`;
  $('live-ns').checked = saved.ns !== false;
  if (saved.recordMode) $('live-record-mode').value = saved.recordMode;
  $('live-default-mic').checked = !!saved.defaultMic;
  $('live-hear-volume').value = Number.isFinite(saved.hearVolume) ? saved.hearVolume : 80;
  $('live-hear-volume-value').textContent = `${$('live-hear-volume').value}%`;
  showSilence(); showTune(); setPhase('off');
  showRoute(); showApi();

  // voice.js stops a session when you leave this tab, and loads the voices when you open it.
  window.rocsyLiveVoice = {stop, load: () => {
    if (!state.voices.length) loadVoices();
    if (state.route === 'mic' && !state.micReady) prepareMic();
  }};
  window.addEventListener('pagehide', stop);
  navigator.mediaDevices?.addEventListener?.('devicechange', () => loadDevices().catch(() => {}));
  loadDevices().catch(() => {});
  if (document.body.dataset.view === 'live') window.rocsyLiveVoice.load();   // opened straight on #live
})();
