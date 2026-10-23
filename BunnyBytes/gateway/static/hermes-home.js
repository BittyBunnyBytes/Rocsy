(() => {
  // Gateway-injected into the Hermes dashboard: a Rocsy mark before the
  // "Hermes Agent" brand in the sidebar that goes back to the Rocsy home page
  // (every other service has it in its top bar). Re-added if React re-renders.
  const MARK = '<svg viewBox="40 140 432 260" width="26" height="17" aria-hidden="true">'
    + '<circle cx="256" cy="268" r="124" fill="var(--bb-amber, #e1a744)"/><path fill="#f2f7fb" d="M256 186 C269 186 276 197 274 210 '
    + 'C320 182 380 162 464 156 C418 200 358 234 292 254 L308 314 L256 294 L204 314 L220 254 C154 234 94 200 48 156 C132 162 192 182 '
    + '238 210 C236 197 243 186 256 186 Z"/></svg>';
  function addHome() {
    const brand = document.querySelector('#app-sidebar > div:first-child > div');
    if (!brand || brand.querySelector('[data-rocsy-home]')) return;
    const link = document.createElement('a');
    link.href = '/';
    link.dataset.rocsyHome = '1';
    link.title = 'Back to the Rocsy home page';
    link.setAttribute('aria-label', 'Rocsy home');
    link.innerHTML = MARK;
    link.style.cssText = 'display:inline-grid;place-items:center;width:34px;height:34px;margin-left:-6px;border-radius:8px';
    link.addEventListener('mouseenter', () => { link.style.background = 'color-mix(in srgb, currentColor 12%, transparent)'; });
    link.addEventListener('mouseleave', () => { link.style.background = ''; });
    brand.prepend(link);
  }
  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; addHome(); });
  }).observe(document.documentElement, {childList: true, subtree: true});
  addHome();
})();
