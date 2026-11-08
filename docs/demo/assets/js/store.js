/* ────────────────────────────────────────────────────────────────
   Local persistence: settings + per-session transcripts
   Everything is defensive: localStorage can be disabled, full or
   corrupted; failures must never break the chat.
   ──────────────────────────────────────────────────────────────── */

const SETTINGS_KEY = 'ha.settings.v2';
const STATE_KEY = 'ha.conversations.v2';
const MAX_CONVERSATIONS = 24;
const MAX_MESSAGES = 160;

export const DEFAULT_SETTINGS = {
  theme: 'system',            // 'system' | 'dark' | 'light'
  apiKey: '',
  userId: 'web-user',
  protocol: 'legacy',         // 'legacy' | 'agui'
  showMeta: true,             // badges under assistant replies
  autoScroll: true,
};

function read(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : fallback;
  } catch (err) {
    return fallback;
  }
}

function write(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (err) {
    return false;
  }
}

export function loadSettings() {
  const stored = read(SETTINGS_KEY, {});
  return Object.assign({}, DEFAULT_SETTINGS, stored);
}

export function saveSettings(patch) {
  const next = Object.assign({}, loadSettings(), patch || {});
  write(SETTINGS_KEY, next);
  return next;
}

export function systemTheme() {
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  } catch (err) {
    return 'dark';
  }
}

export function applyTheme(theme) {
  const resolved = theme === 'system' ? systemTheme() : theme;
  document.documentElement.setAttribute('data-theme', resolved);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', resolved === 'light' ? '#f4f6f8' : '#0a0c10');
  return resolved;
}

/* ── Conversations ─────────────────────────────────────────── */

function emptyState() {
  return { order: [], items: {} };
}

export function loadState() {
  const state = read(STATE_KEY, null);
  if (!state || !Array.isArray(state.order) || typeof state.items !== 'object') {
    return emptyState();
  }
  return state;
}

let saveTimer = 0;

export function saveState(state) {
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(function () {
    persistNow(state);
  }, 120);
}

export function persistNow(state) {
  const order = state.order.slice(0, MAX_CONVERSATIONS);
  const kept = {};
  order.forEach(function (id) {
    const conv = state.items[id];
    if (!conv) return;
    kept[id] = Object.assign({}, conv, {
      messages: (conv.messages || []).slice(-MAX_MESSAGES),
    });
  });
  state.order = order;
  state.items = kept;
  if (!write(STATE_KEY, state)) {
    // Quota or private mode: drop the oldest half and retry once.
    state.order = order.slice(0, Math.max(4, Math.floor(order.length / 2)));
    write(STATE_KEY, state);
  }
}

export function ensureConversation(state, id) {
  let conv = state.items[id];
  if (!conv) {
    conv = { id: id, title: '', createdAt: Date.now(), updatedAt: Date.now(), messages: [] };
    state.items[id] = conv;
  }
  state.order = [id].concat(state.order.filter(function (x) { return x !== id; }));
  state.updatedAt = Date.now();
  return conv;
}

export function appendMessage(state, sessionId, message) {
  const conv = ensureConversation(state, sessionId);
  conv.messages.push(message);
  conv.updatedAt = Date.now();
  state.order = [sessionId].concat(state.order.filter(function (x) { return x !== sessionId; }));
  if (message.role === 'user' && !conv.title) {
    conv.title = makeTitle(message.text);
  }
  saveState(state);
  return conv;
}

export function patchMessage(state, sessionId, messageId, patch) {
  const conv = state.items[sessionId];
  if (!conv) return;
  for (let i = 0; i < conv.messages.length; i += 1) {
    if (conv.messages[i].id === messageId) {
      conv.messages[i] = Object.assign({}, conv.messages[i], patch);
      conv.updatedAt = Date.now();
      saveState(state);
      return;
    }
  }
}

export function dropMessage(state, sessionId, messageId) {
  const conv = state.items[sessionId];
  if (!conv) return;
  conv.messages = conv.messages.filter(function (m) { return m.id !== messageId; });
  saveState(state);
}

export function removeConversation(state, sessionId) {
  delete state.items[sessionId];
  state.order = state.order.filter(function (x) { return x !== sessionId; });
  persistNow(state);
  return state;
}

export function makeTitle(text) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return '新对话';
  return clean.length > 20 ? clean.slice(0, 20) + '…' : clean;
}

export function newSessionId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
  } catch (err) { /* fall through */ }
  return 'sess-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

export function uid() {
  return 'm-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}
