
// === THEME: light/dark toggle (non-invasive) ===
(() => {
  document.addEventListener('DOMContentLoaded', () => {
    const ROOT = document.documentElement;
    const STORAGE_KEY = 'mlr-theme'; // Multi-language Runner theme preference
    const btn = document.getElementById('themeToggle');

    // Detect if system prefers dark
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

    function applyTheme(theme) {
      // Normalize/untrusted input -> light/dark
      const t = theme === 'light' ? 'light' : theme === 'dark' ? 'dark' : (prefersDark ? 'dark' : 'light');
      ROOT.setAttribute('data-theme', t);
      if (btn) {
        btn.textContent = t === 'dark' ? '🌙 Dark' : '☀️ Light';
        btn.setAttribute('aria-pressed', t === 'dark' ? 'true' : 'false');
        btn.title = `Switch to ${t === 'dark' ? 'light' : 'dark'} theme`;
      }
    }

    // Initialize from saved preference or system default
    const saved = localStorage.getItem(STORAGE_KEY);
    applyTheme(saved || (prefersDark ? 'dark' : 'light'));

    // Watch for system theme changes only if user hasn't explicitly chosen
    if (window.matchMedia) {
      try {
        const mq = window.matchMedia('(prefers-color-scheme: dark)');
        // Use addEventListener when available; older Safari uses addListener
        (mq.addEventListener || mq.addListener).call(mq, 'change', (e) => {
          const explicit = localStorage.getItem(STORAGE_KEY);
          if (!explicit) applyTheme(e.matches ? 'dark' : 'light');
        });
      } catch {
        /* no-op */
      }
    }

    // Wire the toggle button (if present)
    if (btn) {
      btn.addEventListener('click', () => {
        const next = ROOT.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        localStorage.setItem(STORAGE_KEY, next);
        applyTheme(next);
      });
    }
  });
})();

// === YOUR EXISTING FRONTEND SCRIPT (unchanged logic) ===
(() => {
  document.addEventListener('DOMContentLoaded', () => {
    const $ = (id) => document.getElementById(id);

    // Elements present in your current index.html (one-shot UI)
    const els = {
      language: $('language'),
      code: $('code'),
      input: $('input'),       // stdin textarea
      runBtn: $('runBtn'),
      status: $('status'),
      stdout: $('stdout'),
      stderr: $('stderr'),
      meta: $('meta'),
    };

    // Guard: ensure required IDs exist
    const requiredIds = ['language','code','input','runBtn','status','stdout','stderr','meta'];
    const missing = requiredIds.filter(id => !$(id));
    if (missing.length) {
      console.error('Missing required elements:', missing);
      if (els.status) els.status.textContent = 'Page setup error: missing ' + missing.join(', ');
      return;
    }

    // Status color helper
    function setStatus(text) {
      els.status.textContent = text;
      if (/error|failed/i.test(text)) {
        els.status.style.color = '#ef4444';
      } else if (/running|starting/i.test(text)) {
        els.status.style.color = '#f59e0b';
      } else {
        els.status.style.color = '#10b981';
      }
    }

    // Normalize UI language values to backend tokens
    function normalizeLanguage(raw) {
      const s = (raw || '').trim().toLowerCase();
      if (s === 'postgresql') return 'postgres';
      if (s === 'javascript (node)') return 'javascript';
      return s; // python/java/javascript/postgres already fine
    }

    // Detect if code likely needs input (to warn when stdin is empty)
    function codeNeedsInput(lang, src) {
      const s = (src || '').toLowerCase();
      if (lang === 'python') return /\binput\s*\(/.test(s);
      if (lang === 'javascript') return /readline\.createinterface|process\.stdin|rl\.question\s*\(/.test(s);
      if (lang === 'java') return /\bnew\s+scanner\s*\(\s*system\.in\s*\)/.test(s);
      return false;
    }

    // Optional: inject a default snippet for convenience (Python)
    function ensureDefaultCode() {
      if (!els.code.value.trim() && normalizeLanguage(els.language.value) === 'python') {
        els.code.value = [
          'print("Enter a number: ", end="", flush=True)',
          'x = int(input())',
          'print("the value:", x)',
        ].join('\n');
      }
    }

    // One-shot runner for all supported languages (python/java/javascript/postgres)
    async function runOneShot() {
      const lang = normalizeLanguage(els.language.value);
      const src  = els.code.value;

      // Auto-append newline so readline-style programs consume the line
      const rawStdin = els.input.value || '';
      const stdin = rawStdin ? (rawStdin.endsWith('\n') ? rawStdin : rawStdin + '\n') : null;

      // If code needs input but none provided, show friendly hint
      if (codeNeedsInput(lang, src) && (!stdin || !stdin.trim())) {
        setStatus('Error');
        els.stderr.textContent = 'This program expects input. Please provide text in "stdin input" before running.';
        return;
      }

      setStatus('Running...');
      els.runBtn.disabled = true;
      els.stdout.textContent = '';
      els.stderr.textContent = '';
      els.meta.textContent = '';

      try {
        const res = await fetch('/api/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ language: lang, code: src, input: stdin }),
        });

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const detail = (data && data.detail) ? data.detail : res.statusText;
          // Helpful hint for common "no stdin" errors
          let hint = '';
          if (/EOFError: EOF when reading a line/.test(detail) || /NoSuchElementException/.test(detail)) {
            hint = '\nHint: Your program expects input. Provide "stdin input" before running.';
          }
          els.stderr.textContent = detail + hint;
          setStatus('Error');
          return;
        }

        els.stdout.textContent = data.stdout || '';
        els.stderr.textContent = data.stderr || '';
        els.meta.textContent = `exit_code=${data.exit_code}  runtime_ms=${data.runtime_ms}  req=${data.request_id}`;
        setStatus(data.ok ? 'Completed' : 'Failed');
      } catch (e) {
        els.stderr.textContent = String(e);
        setStatus('Error');
      } finally {
        els.runBtn.disabled = false;
      }
    }

    // Wire the Run button
    els.runBtn.addEventListener('click', async () => {
      if (!els.language || !els.code) {
        alert('Missing required inputs on the page. Please refresh.');
        return;
      }
      ensureDefaultCode();
      await runOneShot();
    });

    // Initial status
    setStatus('Idle');
  });
})();
