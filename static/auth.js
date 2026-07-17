// Shared auth helper for both pages — see ARCHITECTURE.md "Auth" for the
// full design. One key protects two layers: the FastAPI /api/* middleware
// (X-Auth-Key header) and mediamtx's own HLS/WebRTC listeners (HTTP Basic,
// user "viewer"), since video flows DVR -> mediamtx -> browser directly and
// never touches FastAPI.
(function () {
  const STORAGE_KEY = 'dvrAuthKey';
  let cachedKey = localStorage.getItem(STORAGE_KEY);
  // Dedupes concurrent 401s (the live grid fires several requests around
  // the same time) into a single login prompt instead of each one
  // independently popping/clearing the form.
  let pendingAuth = null;

  function buildOverlay() {
    if (document.getElementById('authOverlay')) return;

    const style = document.createElement('style');
    style.textContent = `
      #authOverlay { position: fixed; inset: 0; background: rgba(0,0,0,0.92); z-index: 1000;
        display: flex; align-items: center; justify-content: center; font-family: system-ui, sans-serif; }
      #authOverlay .box { background: #1c1c1c; border: 1px solid #333; border-radius: 8px; padding: 24px;
        width: 260px; display: flex; flex-direction: column; gap: 12px; }
      #authOverlay h2 { margin: 0; font-size: 15px; color: #eee; font-weight: 600; }
      #authOverlay input { background: #111; color: #eee; border: 1px solid #333; border-radius: 4px;
        padding: 8px 10px; font-size: 14px; }
      #authOverlay button { background: #2563eb; color: #fff; border: none; border-radius: 4px;
        padding: 8px 10px; font-size: 14px; cursor: pointer; }
      #authOverlay button:hover { background: #1d4ed8; }
      #authOverlay button:disabled { background: #1e3a5f; cursor: default; }
      #authOverlay .err { color: #f87171; font-size: 12px; min-height: 14px; }
    `;
    document.head.appendChild(style);

    const overlay = document.createElement('div');
    overlay.id = 'authOverlay';
    overlay.innerHTML = `
      <div class="box">
        <h2>DVR Window — Login</h2>
        <input id="authKeyInput" type="password" placeholder="Access key" autocomplete="off">
        <div class="err" id="authKeyErr"></div>
        <button id="authKeySubmit">Unlock</button>
      </div>
    `;
    document.body.appendChild(overlay);
  }

  function showLoginForm() {
    return new Promise((resolve) => {
      buildOverlay();
      const overlay = document.getElementById('authOverlay');
      const input = document.getElementById('authKeyInput');
      const err = document.getElementById('authKeyErr');
      const submit = document.getElementById('authKeySubmit');
      overlay.style.display = 'flex';
      err.textContent = '';
      input.value = '';
      input.focus();

      async function attempt() {
        const key = input.value.trim();
        if (!key) return;
        submit.disabled = true;
        err.textContent = '';
        // Validate immediately against a real endpoint rather than only
        // discovering a wrong key on some later, unrelated call.
        try {
          const res = await fetch('/api/device', { headers: { 'X-Auth-Key': key } });
          if (res.status === 401) {
            err.textContent = 'Incorrect key';
            submit.disabled = false;
            return;
          }
          if (!res.ok) {
            err.textContent = `Unexpected error (${res.status})`;
            submit.disabled = false;
            return;
          }
        } catch (e) {
          err.textContent = 'Network error — is the server running?';
          submit.disabled = false;
          return;
        }
        cachedKey = key;
        localStorage.setItem(STORAGE_KEY, key);
        overlay.style.display = 'none';
        submit.disabled = false;
        resolve();
      }

      submit.onclick = attempt;
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') attempt(); });
    });
  }

  function ensureAuthKey() {
    if (cachedKey) return Promise.resolve();
    if (!pendingAuth) {
      pendingAuth = showLoginForm().then(() => { pendingAuth = null; });
    }
    return pendingAuth;
  }

  async function authFetch(url, options = {}) {
    await ensureAuthKey();
    const res = await fetch(url, { ...options, headers: { ...(options.headers || {}), 'X-Auth-Key': cachedKey } });
    if (res.status !== 401) return res;

    // Key stopped working (or was wrong to begin with) — clear it and
    // reprompt, then retry once with the freshly-entered key.
    cachedKey = null;
    localStorage.removeItem(STORAGE_KEY);
    await ensureAuthKey();
    return fetch(url, { ...options, headers: { ...(options.headers || {}), 'X-Auth-Key': cachedKey } });
  }

  function hlsXhrSetup(xhr) {
    xhr.setRequestHeader('Authorization', 'Basic ' + btoa(`viewer:${cachedKey}`));
  }

  window.ensureAuthKey = ensureAuthKey;
  window.authFetch = authFetch;
  window.hlsXhrSetup = hlsXhrSetup;
})();
