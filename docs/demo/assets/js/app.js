/* ────────────────────────────────────────────────────────────────
   HeteroAgent console · application controller
   Responsibilities: chat orchestration, streaming UI, approvals,
   history / metrics / settings panels, health and theme state.
   ──────────────────────────────────────────────────────────────── */
import { icon, stageMeta } from './icons.js';
import { renderMarkdown, copyText } from './markdown.js';
import * as api from './api.js';
import * as store from './store.js';

/* ── tiny DOM helpers ──────────────────────────────────────── */
function $(id) { return document.getElementById(id); }

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach(function (key) {
      const value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else node.setAttribute(key, value === true ? '' : String(value));
    });
  }
  (children || []).forEach(function (child) {
    if (child === null || child === undefined || child === false) return;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  });
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function hydrateIcons(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-icon]').forEach(function (holder) {
    const name = holder.getAttribute('data-icon');
    const size = Number(holder.getAttribute('data-icon-size') || 16);
    holder.replaceWith(icon(name, { size: size }));
  });
}

function fmtTime(ts) {
  const d = new Date(ts);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function fmtRel(ts) {
  const diff = Date.now() - ts;
  if (diff < 60000) return '刚刚';
  if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
  if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前';
  const d = new Date(ts);
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === yesterday.toDateString()) return '昨天';
  return (d.getMonth() + 1) + '-' + d.getDate();
}

function fmtDur(ms) {
  if (!ms && ms !== 0) return '';
  if (ms < 1000) return Math.round(ms) + ' ms';
  if (ms < 10000) return (ms / 1000).toFixed(1) + ' s';
  return Math.round(ms / 1000) + ' s';
}

function fmtNum(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '0';
  return n >= 10000 ? (n / 1000).toFixed(1) + 'k' : String(Math.round(n));
}

function shortId(id) {
  return String(id || '').slice(0, 8);
}

function charLen(text) {
  return Array.from(String(text || '')).length;
}

/* ── refs / state ──────────────────────────────────────────── */
const refs = {
  app: $('app'), rail: $('rail'), railToggle: $('railToggle'), scrim: $('scrim'),
  convList: $('convList'), convEmpty: $('convEmpty'), convCount: $('convCount'),
  newChat: $('newChat'), railSettings: $('railSettings'),
  healthFoot: $('healthFoot'), healthText: $('healthText'), healthVer: $('healthVer'),
  sessionChip: $('sessionChip'), themeToggle: $('themeToggle'), themeIcon: $('themeIcon'),
  approvalsBtn: $('approvalsBtn'), approvalCount: $('approvalCount'), settingsBtn: $('settingsBtn'),
  banner: $('banner'), bannerText: $('bannerText'),
  transcript: $('transcript'), messages: $('messages'), welcome: $('welcome'),
  welcomeGrid: $('welcomeGrid'), jumpLatest: $('jumpLatest'),
  composerForm: $('composerForm'), composerInput: $('composerInput'),
  sendBtn: $('sendBtn'), stopBtn: $('stopBtn'), charCount: $('charCount'),
  drawer: $('drawer'), drawerTitle: $('drawerTitle'), drawerClose: $('drawerClose'),
  panelHistory: $('panel-history'), panelApprovals: $('panel-approvals'),
  panelMetrics: $('panel-metrics'), panelSettings: $('panel-settings'),
  histCount: $('histCount'), apprCount: $('apprCount'),
  toasts: $('toasts'), liveStatus: $('liveStatus'),
};

const state = {
  settings: store.loadSettings(),
  conv: store.loadState(),
  sessionId: '',
  streaming: null,
  approvals: [],
  health: { ok: false, version: '' },
  tab: 'history',
  cooldownUntil: 0,
  lastFocus: null,
  historyCache: [],
};

let stick = true;

/* ── feedback: toasts + live region ────────────────────────── */
function toast(message, tone, action) {
  const toneMap = { ok: 'check', bad: 'alert', warn: 'alert', info: 'info' };
  const node = el('div', { class: 'toast', 'data-tone': tone || 'info' }, [
    icon(toneMap[tone] || 'info', { size: 15 }),
    el('span', { text: message }),
  ]);
  if (action && action.label) {
    const btn = el('button', { class: 'btn btn-sm btn-ghost', type: 'button', text: action.label });
    btn.addEventListener('click', function () { action.onClick(); node.remove(); });
    node.appendChild(btn);
  }
  refs.toasts.appendChild(node);
  window.setTimeout(function () {
    node.setAttribute('data-leaving', 'true');
    window.setTimeout(function () { node.remove(); }, 260);
  }, action ? 6500 : 3600);
  while (refs.toasts.children.length > 3) refs.toasts.firstChild.remove();
}

function setLive(text) {
  refs.liveStatus.textContent = text;
}

/* ── theme ─────────────────────────────────────────────────── */
function resolvedTheme() {
  return state.settings.theme === 'system' ? store.systemTheme() : state.settings.theme;
}

function updateThemeIcon() {
  const dark = resolvedTheme() === 'dark';
  clear(refs.themeIcon);
  refs.themeIcon.appendChild(icon(dark ? 'sun' : 'moon', { size: 19 }));
  refs.themeToggle.setAttribute('aria-label', dark ? '切换到浅色主题' : '切换到深色主题');
  refs.themeToggle.setAttribute('title', dark ? '切换到浅色主题' : '切换到深色主题');
}

function setTheme(theme) {
  state.settings = store.saveSettings({ theme: theme });
  store.applyTheme(theme);
  updateThemeIcon();
  if (state.tab === 'settings' && isDrawerOpen()) renderSettingsPanel();
}

/* ── conversation list ─────────────────────────────────────── */
function conversationIds() {
  return state.conv.order.filter(function (id) { return state.conv.items[id]; });
}

function renderConversations() {
  const ids = conversationIds();
  refs.convCount.textContent = String(ids.length);
  refs.convEmpty.hidden = ids.length > 0;
  clear(refs.convList);
  ids.forEach(function (id) {
    refs.convList.appendChild(convItem(state.conv.items[id]));
  });
}

function convItem(conv) {
  const active = conv.id === state.sessionId;
  const row = el('div', { class: 'conv', role: 'listitem', 'aria-current': active ? 'true' : 'false' });
  const hit = el('button', { class: 'conv-hit', type: 'button', 'aria-label': '打开会话：' + (conv.title || '新对话') }, [
    icon('message', { size: 14 }),
    el('span', { class: 'conv-body' }, [
      el('span', { class: 'conv-title', text: conv.title || '新对话' }),
      el('span', { class: 'conv-meta', text: fmtRel(conv.updatedAt || conv.createdAt) + ' · ' + (conv.messages || []).length + ' 条' }),
    ]),
  ]);
  hit.addEventListener('click', function () { selectConversation(conv.id); });

  const del = el('button', { class: 'conv-del', type: 'button', 'aria-label': '删除会话', title: '删除会话' }, [icon('trash', { size: 14 })]);
  let armed = false;
  let timer = 0;
  del.addEventListener('click', function (event) {
    event.stopPropagation();
    if (!armed) {
      armed = true;
      del.dataset.confirm = 'true';
      del.setAttribute('title', '再次点击确认删除');
      timer = window.setTimeout(function () { armed = false; del.dataset.confirm = 'false'; del.setAttribute('title', '删除会话'); }, 2600);
      return;
    }
    window.clearTimeout(timer);
    removeConversation(conv.id);
  });

  row.appendChild(hit);
  row.appendChild(del);
  return row;
}

function migrateConversation(fromId, toId) {
  if (!fromId || !toId || fromId === toId) return;
  const from = state.conv.items[fromId];
  if (!from) return;
  const target = state.conv.items[toId];
  if (target) {
    target.messages = (from.messages || []).concat(target.messages || []);
    target.title = target.title || from.title;
    target.updatedAt = Date.now();
  } else {
    state.conv.items[toId] = Object.assign({}, from, { id: toId, updatedAt: Date.now() });
  }
  delete state.conv.items[fromId];
  state.conv.order = state.conv.order.map(function (id) { return id === fromId ? toId : id; });
  state.conv.order = state.conv.order.filter(function (id, index, arr) { return arr.indexOf(id) === index; });
  store.persistNow(state.conv);
}

function selectConversation(id) {
  if (state.streaming) { toast('请等当前回复结束再切换会话', 'warn'); return; }
  state.sessionId = id;
  renderTranscript();
  renderConversations();
  closeRail();
  if (isDrawerOpen() && state.tab === 'history') renderHistoryPanel();
}

function removeConversation(id) {
  store.removeConversation(state.conv, id);
  if (state.sessionId === id) {
    state.sessionId = conversationIds()[0] || store.newSessionId();
    renderTranscript();
  }
  renderConversations();
  toast('会话已删除', 'info');
}

function startNewChat() {
  if (state.streaming) { state.streaming.controller.abort(); }
  state.sessionId = store.newSessionId();
  renderTranscript();
  renderConversations();
  closeRail();
  refs.composerInput.focus();
  setLive('已开始新对话');
}

/* ── transcript ────────────────────────────────────────────── */
function currentMessages() {
  const conv = state.conv.items[state.sessionId];
  return conv ? conv.messages || [] : [];
}

function updateSessionChip() {
  refs.sessionChip.textContent = 'session: ' + shortId(state.sessionId) + '…';
  refs.sessionChip.setAttribute('title', state.sessionId);
}

function renderTranscript() {
  clear(refs.messages);
  const messages = currentMessages();
  messages.forEach(function (msg) { refs.messages.appendChild(messageNode(msg)); });
  refs.welcome.hidden = messages.length > 0;
  updateSessionChip();
  stick = true;
  scrollToBottom(true);
}

function hideWelcome() {
  refs.welcome.hidden = true;
}

function intentMeta(intent) {
  const map = {
    knowledge_rag: { icon: 'book', label: '知识问答' },
    ticket_handler: { icon: 'ticket', label: '工单处理' },
    compliance: { icon: 'shield', label: '合规审查' },
    unknown: { icon: 'info', label: '未识别意图' },
    error: { icon: 'alert', label: '异常' },
  };
  return map[intent] || { icon: 'info', label: intent || '未识别意图' };
}

function tag(tone, iconName, label, mono) {
  const attrs = { class: 'tag' };
  if (tone) attrs['data-tone'] = tone;
  if (mono) attrs['data-mono'] = 'true';
  return el('span', attrs, [icon(iconName, { size: 11 }), el('span', { text: label })]);
}

function intentTag(intent) {
  const meta = intentMeta(intent);
  return tag('accent', meta.icon, meta.label);
}

function toolButton(iconName, label, handler) {
  const btn = el('button', { class: 'msg-tool', type: 'button', 'aria-label': label, title: label }, [icon(iconName, { size: 14 })]);
  btn.addEventListener('click', handler);
  return btn;
}

function messageNode(msg) {
  const role = msg.role || 'assistant';
  const ts = msg.ts || Date.now();
  const row = el('article', {
    class: 'msg',
    'data-role': role,
    'data-message-id': msg.id,
    'data-state': msg.state || 'done',
  });
  const avatar = el('div', { class: 'msg-avatar', 'aria-hidden': 'true' }, [
    icon(role === 'user' ? 'user' : role === 'error' ? 'alert' : 'sparkle', { size: 15 }),
  ]);
  const main = el('div', { class: 'msg-main' });
  main.appendChild(el('div', { class: 'msg-head' }, [
    el('span', { class: 'msg-author', text: role === 'user' ? '你' : role === 'error' ? '请求未完成' : 'HeteroAgent' }),
    el('time', { text: fmtTime(ts), datetime: new Date(ts).toISOString() }),
  ]));

  if (role === 'user') {
    main.appendChild(el('div', { class: 'bubble', text: msg.text }));
  } else {
    const content = el('div', { class: 'msg-content prose' });
    content.appendChild(renderMarkdown(msg.text || ''));
    main.appendChild(content);
  }

  const foot = el('div', { class: 'msg-foot' });
  const meta = msg.meta || {};
  if (role !== 'user' && state.settings.showMeta) {
    if (msg.intent) foot.appendChild(intentTag(msg.intent));
    if (msg.compliance === true) foot.appendChild(tag('ok', 'shieldCheck', '合规通过'));
    if (msg.compliance === false) {
      foot.appendChild(meta.approvalId
        ? tag('warn', 'clock', '转人工审批')
        : tag('bad', 'shieldX', '合规拦截'));
    }
    if (meta.cacheHit) foot.appendChild(tag('accent', 'zap', '语义缓存命中'));
    if (meta.stopped) foot.appendChild(tag('warn', 'stop', '已停止'));
    if (meta.latencyMs) {
      foot.appendChild(tag(null, 'clock', fmtDur(meta.latencyMs) + ' · ' + charLen(msg.text) + ' 字', true));
    }
  }

  const tools = el('div', { class: 'msg-tools' });
  tools.appendChild(toolButton('copy', '复制内容', function () {
    copyText(msg.text || '').then(function (ok) { toast(ok ? '已复制到剪贴板' : '复制失败', ok ? 'ok' : 'bad'); });
  }));
  if (role !== 'user') {
    tools.appendChild(toolButton('refresh', '重新生成', function () { retryMessage(msg); }));
  }
  foot.appendChild(tools);
  main.appendChild(foot);

  row.appendChild(avatar);
  row.appendChild(main);
  return row;
}

function retryMessage(msg) {
  if (state.streaming) { toast('正在生成中，稍后再试', 'warn'); return; }
  let text = (msg.meta && msg.meta.retryText) || '';
  if (!text) {
    const list = currentMessages();
    const idx = list.findIndex(function (m) { return m.id === msg.id; });
    for (let i = idx - 1; i >= 0; i -= 1) {
      if (list[i].role === 'user') { text = list[i].text; break; }
    }
  }
  if (!text) { toast('找不到对应的提问内容', 'warn'); return; }
  if (msg.role !== 'error') {
    store.dropMessage(state.conv, state.sessionId, msg.id);
    const node = refs.messages.querySelector('[data-message-id="' + msg.id + '"]');
    if (node) node.remove();
  } else {
    const node = refs.messages.querySelector('[data-message-id="' + msg.id + '"]');
    if (node) node.remove();
  }
  sendMessage(text);
}

/* ── scrolling ─────────────────────────────────────────────── */
function nearBottom() {
  const t = refs.transcript;
  return t.scrollHeight - t.scrollTop - t.clientHeight < 160;
}

function scrollToBottom(force) {
  if (!force && !stick) {
    refs.jumpLatest.dataset.visible = 'true';
    return;
  }
  stick = true;
  window.requestAnimationFrame(function () {
    refs.transcript.scrollTop = refs.transcript.scrollHeight;
    refs.jumpLatest.dataset.visible = 'false';
  });
}

function scrollAfterAppend() {
  if (stick || nearBottom()) scrollToBottom(true);
  else refs.jumpLatest.dataset.visible = 'true';
}

refs.transcript.addEventListener('scroll', function () {
  stick = nearBottom();
  refs.jumpLatest.dataset.visible = stick ? 'false' : 'true';
}, { passive: true });

refs.jumpLatest.addEventListener('click', function () { scrollToBottom(true); });

/* ── streaming ─────────────────────────────────────────────── */
function setBusy(busy) {
  refs.sendBtn.disabled = busy || Date.now() < state.cooldownUntil;
  refs.stopBtn.dataset.visible = busy ? 'true' : 'false';
  refs.composerInput.setAttribute('aria-busy', busy ? 'true' : 'false');
}

function createStreamRow(id) {
  const row = el('article', {
    class: 'msg',
    'data-role': 'assistant',
    'data-state': 'streaming',
    'data-message-id': id,
  });
  const main = el('div', { class: 'msg-main' });
  main.appendChild(el('div', { class: 'msg-head' }, [
    el('span', { class: 'msg-author', text: 'HeteroAgent' }),
    el('time', { text: fmtTime(Date.now()), datetime: new Date().toISOString() }),
  ]));
  const status = el('div', { class: 'stream-status' }, [
    el('span', { class: 'stage-icon' }, [icon('route', { size: 13 })]),
    el('span', { class: 'status-text', text: '正在分析意图…' }),
    el('span', { class: 'dots' }, [el('i'), el('i'), el('i')]),
  ]);
  const track = el('div', { class: 'stage-track' });
  const content = el('div', { class: 'msg-content prose streaming-content', 'aria-live': 'off' });
  const foot = el('div', { class: 'msg-foot' });
  main.appendChild(status);
  main.appendChild(track);
  main.appendChild(content);
  main.appendChild(foot);
  row.appendChild(el('div', { class: 'msg-avatar', 'aria-hidden': 'true' }, [icon('sparkle', { size: 15 })]));
  row.appendChild(main);
  return { row: row, status: status, track: track, content: content };
}

function onStage(event) {
  const s = state.streaming;
  if (!s) return;
  if (s.activeNode === event.node) return;
  const meta = stageMeta(event.node);

  // The previous stage leaves the status line and joins the completed track.
  if (s.activeNode) {
    const previous = stageMeta(s.activeNode);
    let pill = s.pills[s.activeNode];
    if (!pill) {
      pill = el('span', { class: 'stage-pill', 'data-node': s.activeNode, 'data-state': 'done' }, [
        icon(previous.icon, { size: 11 }),
        el('span', { text: previous.label }),
      ]);
      s.track.appendChild(pill);
      s.pills[s.activeNode] = pill;
    }
    pill.dataset.state = 'done';
  }
  s.activeNode = event.node;

  const iconSlot = s.status.querySelector('.stage-icon');
  if (iconSlot) iconSlot.replaceWith(el('span', { class: 'stage-icon' }, [icon(meta.icon, { size: 13 })]));
  const label = s.status.querySelector('.status-text');
  if (label) label.textContent = meta.label + '…';
  setLive(meta.label);
  syncPipelineRail(event.node);
  if (stick) scrollToBottom();
}

function scheduleFlush() {
  const s = state.streaming;
  if (!s || s.raf) return;
  s.raf = window.requestAnimationFrame(function () {
    s.raf = 0;
    if (!state.streaming) return;
    s.content.textContent = s.text;
    if (stick) scrollToBottom();
  });
}

function finalizeAssistant(payload) {
  const s = state.streaming;
  if (!s || s.finalised) return;
  s.finalised = true;
  if (s.raf) window.cancelAnimationFrame(s.raf);
  const data = payload || {};
  const text = String(data.text || s.text || '').trim() || '（本次没有返回内容）';
  const meta = {
    latencyMs: Math.round(performance.now() - s.startedAt),
    cacheHit: !!data.cacheHit,
    stopped: !!data.stopped,
  };
  if (data.approvalId) meta.approvalId = data.approvalId;
  if (data.retryText) meta.retryText = data.retryText;
  const msg = {
    id: s.id,
    role: 'assistant',
    text: text,
    intent: data.intent || '',
    compliance: data.compliance,
    ts: Date.now(),
    meta: meta,
  };
  store.appendMessage(state.conv, state.sessionId, msg);
  const node = messageNode(msg);
  s.row.replaceWith(node);
  if (data.approvalId) {
    const main = node.querySelector('.msg-main');
    if (main) main.appendChild(approvalCard(data.approvalId, data.intent));
  }
  if (data.approvalId) refreshApprovals({ silent: true });
  setLive('回复已生成');
  renderConversations();
  scrollAfterAppend();
}

function describeError(err) {
  if (err instanceof api.ApiError) {
    if (err.network) {
      return {
        title: '连接不上后端服务',
        hint: '确认 uvicorn 正在运行（make run），或检查反向代理是否放行 /api/chat/stream。',
        action: 'retry',
      };
    }
    if (err.status === 401) {
      return {
        title: '缺少或无效的 API Key',
        hint: '在设置里填入与 API_KEYS 一致的密钥，或改用 REQUIRE_AUTH=false 的本地环境。',
        action: 'settings',
      };
    }
    if (err.status === 400) {
      return { title: '请求被安全护栏拦截', hint: err.message + '。可以换一种表述后重试。', action: 'retry' };
    }
    if (err.status === 413) {
      return { title: '消息过长', hint: '单条消息上限 8000 字符，精简后再发送。', action: 'retry' };
    }
    if (err.status === 429) {
      return { title: '触发限流', hint: '请求过于频繁，' + Math.max(1, err.retryAfter || 10) + ' 秒后可以重试。', action: 'retry' };
    }
    if (err.status === 503) {
      return {
        title: '服务未就绪',
        hint: '后端没有可用模型或向量库：设置 OPENAI_API_KEY 后重启，或以 DEMO_MODE=true 演示。',
        action: 'retry',
      };
    }
    return { title: '请求失败（HTTP ' + err.status + '）', hint: err.message, action: 'retry' };
  }
  return { title: '请求出错', hint: (err && err.message) || '未知错误', action: 'retry' };
}

function finalizeError(err, retryText) {
  const s = state.streaming;
  if (!s || s.finalised) return;
  s.finalised = true;
  if (s.raf) window.cancelAnimationFrame(s.raf);
  const info = describeError(err);
  const row = el('article', {
    class: 'msg',
    'data-role': 'error',
    'data-state': 'done',
    'data-message-id': s.id,
  });
  const main = el('div', { class: 'msg-main' });
  main.appendChild(el('div', { class: 'msg-head' }, [
    el('span', { class: 'msg-author', text: '请求未完成' }),
    el('time', { text: fmtTime(Date.now()), datetime: new Date().toISOString() }),
  ]));
  const content = el('div', { class: 'msg-content prose' }, [
    el('p', {}, [el('strong', { text: info.title })]),
    el('p', { text: info.hint }),
  ]);
  main.appendChild(content);
  const foot = el('div', { class: 'msg-foot' });
  const tools = el('div', { class: 'msg-tools' });
  tools.appendChild(toolButton('refresh', '重试', function () { retryMessage({ id: s.id, role: 'error', text: info.title, meta: { retryText: retryText } }); }));
  if (info.action === 'settings') {
    const btn = el('button', { class: 'btn btn-sm btn-ghost', type: 'button' }, [icon('sliders', { size: 13 }), el('span', { text: '打开设置' })]);
    btn.addEventListener('click', function () { openDrawer('settings'); });
    tools.appendChild(btn);
  }
  foot.appendChild(tools);
  main.appendChild(foot);
  row.appendChild(el('div', { class: 'msg-avatar', 'aria-hidden': 'true' }, [icon('alert', { size: 15 })]));
  row.appendChild(main);
  s.row.replaceWith(row);
  setLive(info.title);
  if (err instanceof api.ApiError && err.status === 429) startCooldown(err.retryAfter || 10);
  scrollAfterAppend();
}

function handleStreamEvent(event) {
  const s = state.streaming;
  if (!s || s.finalised) return;
  if (event.type === 'session' && event.session_id) {
    if (event.session_id !== state.sessionId) {
      migrateConversation(state.sessionId, event.session_id);
      state.sessionId = event.session_id;
      updateSessionChip();
      renderConversations();
    }
    return;
  }
  if (event.type === 'stage') { onStage(event); return; }
  if (event.type === 'token') {
    s.text += event.text || '';
    scheduleFlush();
    return;
  }
  if (event.type === 'response') {
    finalizeAssistant({
      text: event.response,
      intent: event.intent,
      compliance: event.compliance_passed,
      approvalId: event.approval_id,
      cacheHit: event.cache_hit,
      retryText: s.retryText,
    });
    return;
  }
  if (event.type === 'error') {
    finalizeError(new api.ApiError(event.status || 500, event.message || '服务端异常'), s.retryText);
    return;
  }
}

async function runStream(text) {
  const s = state.streaming;
  try {
    await api.streamChat({
      message: text,
      sessionId: state.sessionId,
      userId: state.settings.userId || 'web-user',
      protocol: state.settings.protocol,
      apiKey: state.settings.apiKey,
      signal: s.controller.signal,
      onEvent: handleStreamEvent,
    });
    if (state.streaming && !state.streaming.finalised) {
      finalizeAssistant({ text: s.text, intent: '', compliance: null, retryText: s.retryText });
    }
  } catch (err) {
    if (err && err.name === 'AbortError') {
      finalizeAssistant({
        text: s.text + (s.text ? '\n\n' : '') + '_（已停止生成）_',
        intent: '',
        compliance: null,
        stopped: true,
        retryText: s.retryText,
      });
      toast('已停止生成', 'info');
      return;
    }
    finalizeError(err, s.retryText);
  }
}

async function sendMessage(rawText) {
  const text = String(rawText || '').trim();
  if (!text) return;
  if (state.streaming) { toast('上一次回复还在生成中', 'warn'); return; }
  if (Date.now() < state.cooldownUntil) { toast('请求过于频繁，请稍后重试', 'warn'); return; }

  hideWelcome();
  const userMsg = { id: store.uid(), role: 'user', text: text, ts: Date.now() };
  store.appendMessage(state.conv, state.sessionId, userMsg);
  refs.messages.appendChild(messageNode(userMsg));
  renderConversations();
  resetComposer();
  if (state.settings.autoScroll !== false) stick = true;
  scrollToBottom(true);
  markPipelineRunning();

  const assistantId = store.uid();
  const stream = createStreamRow(assistantId);
  refs.messages.appendChild(stream.row);
  scrollToBottom(true);

  state.streaming = {
    id: assistantId,
    controller: new AbortController(),
    startedAt: performance.now(),
    text: '',
    finalised: false,
    row: stream.row,
    status: stream.status,
    track: stream.track,
    content: stream.content,
    pills: {},
    activeNode: '',
    raf: 0,
    retryText: text,
  };
  setBusy(true);
  setLive('正在生成回复');
  try {
    await runStream(text);
  } finally {
    state.streaming = null;
    setBusy(false);
    clearPipelineRail();
    refs.composerInput.focus();
  }
}

/* ── pipeline highlight in the rail ────────────────────────── */
function markPipelineRunning() {
  document.querySelectorAll('.pipeline-node').forEach(function (node) {
    delete node.dataset.active;
    delete node.dataset.done;
  });
}

function syncPipelineRail(activeNode) {
  const nodes = Array.prototype.slice.call(document.querySelectorAll('.pipeline-node'));
  let reached = false;
  nodes.forEach(function (node) {
    if (node.dataset.node === activeNode) {
      reached = true;
      node.dataset.active = 'true';
      delete node.dataset.done;
    } else if (!reached) {
      node.dataset.done = 'true';
      delete node.dataset.active;
    } else {
      delete node.dataset.done;
      delete node.dataset.active;
    }
  });
}

function clearPipelineRail() {
  document.querySelectorAll('.pipeline-node').forEach(function (node) {
    delete node.dataset.active;
    delete node.dataset.done;
  });
}

/* ── approval card ─────────────────────────────────────────── */
function riskTag(level) {
  const value = String(level || '').toLowerCase();
  if (value === 'high' || value === 'critical') return tag('bad', 'alert', '高风险');
  if (value === 'medium') return tag('warn', 'alert', '中风险');
  return tag(null, 'info', '低风险');
}

function approvalCard(approvalId, intent) {
  const card = el('div', { class: 'approval' });
  card.appendChild(el('div', { class: 'approval-head' }, [
    icon('shield', { size: 14 }),
    el('span', { text: '需要人工确认' }),
    el('span', { class: 'mono', text: shortId(approvalId) + '…' }),
  ]));
  const body = el('div', { class: 'approval-body' });
  const metaRow = el('div', { class: 'approval-meta' });
  if (intent) metaRow.appendChild(intentTag(intent));
  const riskSlot = el('span');
  metaRow.appendChild(riskSlot);
  const reason = el('textarea', {
    class: 'approval-reason',
    rows: '2',
    placeholder: '审批意见（可选，会写入审计链）',
    'aria-label': '审批意见',
  });
  const actions = el('div', { class: 'approval-actions' });
  const approve = el('button', { class: 'btn btn-ok', type: 'button' }, [icon('check', { size: 14 }), el('span', { text: '通过' })]);
  const reject = el('button', { class: 'btn btn-bad', type: 'button' }, [icon('close', { size: 14 }), el('span', { text: '拒绝' })]);
  const status = el('span', { class: 'approval-note' });
  actions.appendChild(approve);
  actions.appendChild(reject);
  actions.appendChild(status);

  body.appendChild(metaRow);
  body.appendChild(reason);
  body.appendChild(actions);
  body.appendChild(el('p', {
    class: 'approval-note',
    text: '通过后会继续生成回复；拒绝则终止本次请求。两种结果都会写入哈希链审计。',
  }));
  card.appendChild(body);

  api.approvalDetail(approvalId, state.settings.apiKey).then(function (detail) {
    if (!detail) return;
    const tags = el('span');
    tags.appendChild(riskTag(detail.risk_level));
    if (detail.violations && detail.violations.length) {
      tags.appendChild(tag('bad', 'alert', detail.violations.length + ' 条违规'));
    }
    riskSlot.replaceWith(tags);
  }).catch(function () { /* detail is optional */ });

  const decide = async function (action) {
    approve.disabled = true;
    reject.disabled = true;
    status.textContent = '提交中…';
    try {
      const data = await api.decideApproval(approvalId, action, reason.value.trim(), state.settings.apiKey);
      approve.remove();
      reject.remove();
      status.textContent = action === 'approve' ? '已通过' : '已拒绝';
      if (data && data.response) {
        const msg = {
          id: store.uid(),
          role: 'assistant',
          text: data.response,
          intent: intent,
          compliance: action === 'approve',
          ts: Date.now(),
          meta: {},
        };
        store.appendMessage(state.conv, state.sessionId, msg);
        refs.messages.appendChild(messageNode(msg));
        scrollAfterAppend();
      }
      toast(action === 'approve' ? '审批已通过，回复已送达' : '已拒绝该请求', action === 'approve' ? 'ok' : 'warn');
      refreshApprovals({ silent: true });
    } catch (err) {
      status.textContent = '提交失败：' + ((err && err.message) || '未知错误');
      approve.disabled = false;
      reject.disabled = false;
    }
  };

  approve.addEventListener('click', function () { decide('approve'); });
  reject.addEventListener('click', function () { decide('reject'); });
  return card;
}

/* ── composer ──────────────────────────────────────────────── */
function autoGrow() {
  const input = refs.composerInput;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 168) + 'px';
}

function updateCounter() {
  const length = charLen(refs.composerInput.value);
  if (!length) { refs.charCount.textContent = ''; delete refs.charCount.dataset.warn; delete refs.charCount.dataset.bad; return; }
  refs.charCount.textContent = length + ' / 8000';
  if (length > 7600) { refs.charCount.dataset.bad = 'true'; delete refs.charCount.dataset.warn; }
  else if (length > 6000) { refs.charCount.dataset.warn = 'true'; delete refs.charCount.dataset.bad; }
  else { delete refs.charCount.dataset.warn; delete refs.charCount.dataset.bad; }
}

function resetComposer() {
  refs.composerInput.value = '';
  refs.composerInput.style.height = 'auto';
  updateCounter();
}

function submitComposer() {
  const value = refs.composerInput.value;
  if (!value.trim()) return;
  sendMessage(value);
}

function startCooldown(seconds) {
  const total = Math.max(1, Math.ceil(seconds || 10));
  state.cooldownUntil = Date.now() + total * 1000;
  refs.sendBtn.disabled = true;
  const timer = window.setInterval(function () {
    const left = Math.ceil((state.cooldownUntil - Date.now()) / 1000);
    if (left <= 0) {
      window.clearInterval(timer);
      delete refs.charCount.dataset.warn;
      updateCounter();
      setBusy(!!state.streaming);
      return;
    }
    refs.charCount.textContent = '限流冷却 ' + left + 's';
    refs.charCount.dataset.warn = 'true';
  }, 300);
}

/* ── drawer + tabs ─────────────────────────────────────────── */
function isDrawerOpen() {
  return refs.app.getAttribute('data-drawer') === 'open';
}

function showScrim(show) {
  refs.scrim.hidden = !show;
  refs.scrim.dataset.visible = show ? 'true' : 'false';
}

function syncDrawerA11y(open) {
  [refs.approvalsBtn, refs.settingsBtn].forEach(function (btn) {
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
}

function openDrawer(tab) {
  state.lastFocus = document.activeElement;
  refs.app.setAttribute('data-drawer', 'open');
  syncDrawerA11y(true);
  selectTab(tab || 'history');
  if (window.innerWidth <= 960) showScrim(true);
  refs.drawerClose.focus();
}

function closeDrawer() {
  if (!isDrawerOpen()) return;
  refs.app.setAttribute('data-drawer', 'closed');
  syncDrawerA11y(false);
  if (refs.rail.getAttribute('data-open') !== 'true') showScrim(false);
  if (state.lastFocus && typeof state.lastFocus.focus === 'function') state.lastFocus.focus();
}

function closeRail() {
  if (refs.rail.getAttribute('data-open') !== 'true') return;
  refs.rail.setAttribute('data-open', 'false');
  refs.railToggle.setAttribute('aria-expanded', 'false');
  if (!isDrawerOpen()) showScrim(false);
}

const TAB_TITLES = { history: '会话历史', approvals: '人工审批', metrics: '运行指标', settings: '设置' };

function selectTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.setAttribute('aria-selected', btn.dataset.tab === tab ? 'true' : 'false');
    btn.tabIndex = btn.dataset.tab === tab ? 0 : -1;
  });
  document.querySelectorAll('.panel').forEach(function (panel) {
    panel.dataset.active = panel.dataset.panel === tab ? 'true' : 'false';
  });
  refs.drawerTitle.textContent = TAB_TITLES[tab] || '';
  if (tab === 'history') renderHistoryPanel();
  if (tab === 'approvals') { renderApprovalsPanel(); refreshApprovals({ silent: true }); }
  if (tab === 'metrics') renderMetricsPanel();
  if (tab === 'settings') renderSettingsPanel();
}

/* ── panels: shared bits ───────────────────────────────────── */
function iconButton(name, label, handler) {
  const btn = el('button', { class: 'icon-btn', type: 'button', 'aria-label': label, title: label }, [icon(name, { size: 16 })]);
  btn.addEventListener('click', handler);
  return btn;
}

function emptyState(iconName, title, hint) {
  return el('div', { class: 'hist-empty' }, [
    icon(iconName, { size: 22 }),
    el('strong', { text: title }),
    el('span', { text: hint }),
  ]);
}

function skeletonRows(count) {
  const wrap = el('div', { class: 'kv' });
  for (let i = 0; i < count; i += 1) {
    wrap.appendChild(el('div', { class: 'skeleton', style: 'height:52px;width:100%' }));
  }
  return wrap;
}

/* ── panel: history ────────────────────────────────────────── */
async function renderHistoryPanel() {
  const panel = refs.panelHistory;
  clear(panel);
  const head = el('div', { class: 'section-head' }, [
    el('span', { text: '服务端短期记忆' }),
    el('span', { class: 'spacer' }),
    iconButton('refresh', '刷新历史', function () { renderHistoryPanel(); }),
    iconButton('download', '导出当前会话', exportConversation),
  ]);
  panel.appendChild(head);
  panel.appendChild(skeletonRows(3));
  try {
    const data = await api.history(state.sessionId, state.settings.apiKey);
    const messages = (data && data.messages) || [];
    state.historyCache = messages;
    refs.histCount.textContent = messages.length ? String(messages.length) : '';
    clear(panel);
    panel.appendChild(head);
    if (!messages.length) {
      panel.appendChild(emptyState('history', '服务端还没有这个会话', '发送一条消息后，短期记忆会在这里出现。'));
      return;
    }
    const list = el('div', { class: 'panel-list' });
    messages.slice().reverse().forEach(function (msg) {
      const role = msg.role || 'assistant';
      const raw = msg.content != null ? msg.content : (msg.text != null ? msg.text : '');
      const text = typeof raw === 'string' ? raw : JSON.stringify(raw);
      list.appendChild(el('div', { class: 'hist-item' }, [
        el('span', { class: 'hist-role', 'data-role': role, text: role === 'user' ? '用户' : '助手' }),
        el('span', { class: 'hist-body', text: text }),
      ]));
    });
    panel.appendChild(list);
    panel.appendChild(el('p', { class: 'approval-note', text: '按 session_id 隔离，最多保留最近若干轮；仅用于服务端上下文拼接。' }));
  } catch (err) {
    clear(panel);
    panel.appendChild(head);
    const hint = err instanceof api.ApiError && err.status === 401
      ? '服务端启用了鉴权，请在设置中填写 API Key。'
      : (err && err.message) || '读取失败';
    panel.appendChild(emptyState('alert', '读不到服务端历史', hint));
  }
}

function exportConversation() {
  const messages = currentMessages();
  if (!messages.length) { toast('当前会话还没有内容', 'warn'); return; }
  const lines = ['# HeteroAgent 会话记录', '', '会话 ID：' + state.sessionId, '导出时间：' + new Date().toLocaleString('zh-CN'), ''];
  messages.forEach(function (msg) {
    lines.push('## ' + (msg.role === 'user' ? '用户' : '助手') + ' · ' + fmtTime(msg.ts || Date.now()));
    lines.push('');
    lines.push(msg.text || '');
    lines.push('');
  });
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: 'heteroagent-' + shortId(state.sessionId) + '.md' });
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  toast('已导出 Markdown', 'ok');
}

/* ── panel: approvals ──────────────────────────────────────── */
function updateApprovalBadges() {
  const count = state.approvals.length;
  refs.approvalCount.hidden = count === 0;
  refs.approvalCount.textContent = count > 9 ? '9+' : String(count);
  refs.apprCount.textContent = String(count);
  refs.apprCount.dataset.zero = count === 0 ? 'true' : 'false';
  document.getElementById('tab-approvals').dataset.alert = count > 0 ? 'true' : 'false';
}

async function refreshApprovals(options) {
  const opts = options || {};
  try {
    const data = await api.pendingApprovals(state.settings.apiKey);
    state.approvals = (data && data.reviews) || [];
    updateApprovalBadges();
    if (state.tab === 'approvals' && isDrawerOpen()) renderApprovalsPanel();
  } catch (err) {
    state.approvals = [];
    updateApprovalBadges();
    if (!opts.silent) toast((err && err.message) || '读取审批队列失败', 'bad');
  }
}

async function renderApprovalsPanel() {
  const panel = refs.panelApprovals;
  clear(panel);
  const head = el('div', { class: 'section-head' }, [
    el('span', { text: '待人工审批' }),
    el('span', { class: 'spacer' }),
    iconButton('refresh', '刷新审批队列', function () { renderApprovalsPanel(); refreshApprovals({ silent: true }); }),
    iconButton('link', '校验审计链', verifyAuditChain),
  ]);
  panel.appendChild(head);
  panel.appendChild(el('p', { class: 'approval-note', text: '合规不通过的请求会进入这里；审批结果写入哈希链，可校验完整性。' }));

  const reviews = state.approvals;
  if (!reviews.length) {
    panel.appendChild(emptyState('shieldCheck', '没有待处理审批', '合规审查通过时不会产生审批单。'));
    return;
  }
  reviews.forEach(function (review) { panel.appendChild(approvalItem(review)); });
}

function approvalItem(review) {
  const item = el('div', { class: 'approval-item' });
  const reason = el('textarea', { class: 'approval-reason', rows: '2', placeholder: '审批意见（可选）', 'aria-label': '审批意见' });
  const status = el('span', { class: 'approval-note' });
  const row = el('div', { class: 'row' }, [
    el('span', { class: 'id', text: shortId(review.approval_id) + '…' }),
    riskTag(review.risk_level),
    review.intent ? intentTag(review.intent) : null,
    el('span', { class: 'approval-note', text: fmtRel(review.created_at ? review.created_at * 1000 : Date.now()) }),
  ]);
  item.appendChild(row);
  if (review.violations && review.violations.length) {
    const chips = el('div', { class: 'violations' });
    review.violations.slice(0, 4).forEach(function (v) {
      chips.appendChild(tag('bad', 'alert', String(v)));
    });
    item.appendChild(chips);
  }
  item.appendChild(reason);
  const actions = el('div', { class: 'actions' });
  const approve = el('button', { class: 'btn btn-sm btn-ok', type: 'button' }, [icon('check', { size: 13 }), el('span', { text: '通过' })]);
  const reject = el('button', { class: 'btn btn-sm btn-bad', type: 'button' }, [icon('close', { size: 13 }), el('span', { text: '拒绝' })]);
  actions.appendChild(approve);
  actions.appendChild(reject);
  actions.appendChild(status);
  item.appendChild(actions);

  const decide = async function (action) {
    approve.disabled = true;
    reject.disabled = true;
    status.textContent = '提交中…';
    try {
      await api.decideApproval(review.approval_id, action, reason.value.trim(), state.settings.apiKey);
      toast(action === 'approve' ? '审批已通过' : '已拒绝该请求', action === 'approve' ? 'ok' : 'warn');
      refreshApprovals({ silent: true });
      renderApprovalsPanel();
    } catch (err) {
      status.textContent = (err && err.message) || '提交失败';
      approve.disabled = false;
      reject.disabled = false;
    }
  };
  approve.addEventListener('click', function () { decide('approve'); });
  reject.addEventListener('click', function () { decide('reject'); });
  return item;
}

async function verifyAuditChain() {
  const panel = refs.panelApprovals;
  let slot = panel.querySelector('[data-audit]');
  if (!slot) {
    slot = el('div', { class: 'approval-note', 'data-audit': 'true' });
    panel.insertBefore(slot, panel.children[1] || null);
  }
  slot.textContent = '正在校验哈希链…';
  try {
    const data = await api.auditChain(state.settings.apiKey);
    const rows = (data && data.rows) || [];
    slot.textContent = (data && data.valid ? '审计链完整' : '审计链校验失败') + ' · ' + rows.length + ' 条记录';
    toast(data && data.valid ? '审计链完整，未被篡改' : '审计链校验失败', data && data.valid ? 'ok' : 'bad');
  } catch (err) {
    slot.textContent = '校验失败：' + ((err && err.message) || '未知错误');
  }
}

/* ── panel: metrics ────────────────────────────────────────── */
function statCard(label, value, unit) {
  return el('div', { class: 'stat' }, [
    el('div', { class: 'k', text: label }),
    el('div', { class: 'v' }, [el('span', { text: value }), unit ? el('small', { text: unit }) : null]),
  ]);
}

async function renderMetricsPanel() {
  const panel = refs.panelMetrics;
  clear(panel);
  const head = el('div', { class: 'section-head' }, [
    el('span', { text: '运行指标' }),
    el('span', { class: 'spacer' }),
    iconButton('refresh', '刷新指标', function () { renderMetricsPanel(); }),
  ]);
  panel.appendChild(head);
  panel.appendChild(skeletonRows(2));
  try {
    const data = await api.metrics(state.settings.apiKey);
    const summary = (data && data.agent_metrics) || {};
    const nodes = Object.keys(summary);
    clear(panel);
    panel.appendChild(head);

    let calls = 0;
    let tokens = 0;
    let cost = 0;
    let p95 = 0;
    nodes.forEach(function (name) {
      const entry = summary[name] || {};
      calls += entry.total_calls || 0;
      tokens += entry.total_tokens || 0;
      cost += entry.estimated_cost_usd || 0;
      if ((entry.p95_duration_ms || 0) > p95) p95 = entry.p95_duration_ms || 0;
    });

    panel.appendChild(el('div', { class: 'stat-grid' }, [
      statCard('总调用', fmtNum(calls), '次'),
      statCard('最高 P95', p95 ? Math.round(p95) : 0, 'ms'),
      statCard('Token 用量', fmtNum(tokens), ''),
      statCard('估算成本', cost ? cost.toFixed(4) : '0', 'USD'),
    ]));

    if (!nodes.length) {
      panel.appendChild(emptyState('gauge', '还没有指标数据', '完成一次对话后，节点耗时与 token 会出现在这里。'));
      return;
    }

    panel.appendChild(el('div', { class: 'section-head' }, [el('span', { text: '按节点' })]));
    const list = el('div', { class: 'kv' });
    Object.keys(summary)
      .sort(function (a, b) { return (summary[b].total_calls || 0) - (summary[a].total_calls || 0); })
      .slice(0, 10)
      .forEach(function (name) {
        const entry = summary[name];
        const errRate = Math.round((entry.error_rate || 0) * 1000) / 10;
        list.appendChild(el('div', { class: 'kv-row' }, [
          el('dt', { text: name }),
          el('dd', { text: (entry.total_calls || 0) + ' 次 · p95 ' + Math.round(entry.p95_duration_ms || 0) + 'ms · 错误 ' + errRate + '%' }),
        ]));
      });
    panel.appendChild(list);

    const breaker = data && data.circuit_breaker;
    if (breaker) {
      panel.appendChild(el('div', { class: 'section-head' }, [el('span', { text: 'MCP 熔断器' })]));
      const breakerList = el('div', { class: 'kv' });
      const breakerRow = function (name, value) {
        breakerList.appendChild(el('div', { class: 'kv-row' }, [
          el('dt', { text: name }),
          el('dd', { text: value }),
        ]));
      };
      if (typeof breaker === 'string') {
        breakerRow('状态', breaker);
      } else {
        const tools = Object.keys(breaker);
        tools.forEach(function (tool) {
          const item = breaker[tool] || {};
          const failures = item.consecutive_failures != null ? item.consecutive_failures : 0;
          const threshold = item.failure_threshold != null ? item.failure_threshold : '-';
          const cooldown = item.cooldown_remaining_s != null ? item.cooldown_remaining_s : 0;
          breakerRow(tool, (item.state || 'unknown') + ' · 连续失败 ' + failures + '/' + threshold + ' · 冷却 ' + cooldown + 's');
        });
        if (!tools.length) breakerRow('状态', '未启用');
      }
      panel.appendChild(breakerList);
    }
    const calls2 = (data && data.tool_call_log) || [];
    if (calls2.length) {
      panel.appendChild(el('p', { class: 'approval-note', text: '最近工具调用：' + calls2.length + ' 条（详见后端 /api/metrics）' }));
    }
  } catch (err) {
    clear(panel);
    panel.appendChild(head);
    const hint = err instanceof api.ApiError && err.status === 401
      ? '指标接口需要鉴权，请在设置里填写 API Key。'
      : (err && err.message) || '读取失败';
    panel.appendChild(emptyState('gauge', '读不到指标', hint));
  }
}

/* ── panel: settings ───────────────────────────────────────── */
function field(label, control, hint) {
  const labelAttrs = control && control.id ? { for: control.id, text: label } : { text: label };
  return el('div', { class: 'field' }, [
    el('label', labelAttrs),
    control,
    hint ? el('span', { class: 'hint', text: hint }) : null,
  ]);
}

function textInput(id, value, placeholder, type) {
  const input = el('input', { id: id, type: type || 'text', placeholder: placeholder || '' });
  input.value = value || '';
  return input;
}

function switchRow(label, hint, checked, onChange) {
  const knob = el('button', { class: 'switch', type: 'button', role: 'switch', 'aria-checked': checked ? 'true' : 'false', 'aria-label': label });
  knob.addEventListener('click', function () {
    const next = knob.getAttribute('aria-checked') !== 'true';
    knob.setAttribute('aria-checked', String(next));
    onChange(next);
  });
  return el('div', { class: 'switch-row' }, [
    el('div', { class: 'txt' }, [el('span', { text: label }), hint ? el('small', { text: hint }) : null]),
    knob,
  ]);
}

function renderSettingsPanel() {
  const panel = refs.panelSettings;
  clear(panel);

  const themeSeg = el('div', { class: 'seg' });
  [['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']].forEach(function (pair) {
    const btn = el('button', { type: 'button', 'aria-pressed': state.settings.theme === pair[0] ? 'true' : 'false' }, [
      el('span', { text: pair[1] }),
    ]);
    btn.addEventListener('click', function () {
      setTheme(pair[0]);
      themeSeg.querySelectorAll('button').forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
      btn.setAttribute('aria-pressed', 'true');
    });
    themeSeg.appendChild(btn);
  });
  panel.appendChild(el('div', { class: 'section-head' }, [el('span', { text: '外观' })]));
  panel.appendChild(field('主题', themeSeg, '默认跟随系统，切换后写入本地存储。'));

  panel.appendChild(el('div', { class: 'section-head' }, [el('span', { text: '连接' })]));

  const userId = textInput('setUserId', state.settings.userId, 'web-user');
  userId.addEventListener('change', function () {
    state.settings = store.saveSettings({ userId: userId.value.trim() || 'web-user' });
    toast('已保存用户标识', 'ok');
  });
  panel.appendChild(field('用户标识 · user_id', userId, '用于短期记忆、跨会话事实与用量预算的隔离。'));

  const apiKey = textInput('setApiKey', state.settings.apiKey, '留空表示服务端未开启鉴权', 'password');
  apiKey.addEventListener('change', function () {
    state.settings = store.saveSettings({ apiKey: apiKey.value.trim() });
    toast(apiKey.value.trim() ? '已保存 API Key' : '已清空 API Key', 'ok');
    refreshApprovals({ silent: true });
  });
  panel.appendChild(field('API Key', apiKey, '对应服务端 API_KEYS（x-api-key 请求头），只保存在本机。'));

  const protocol = el('select', { id: 'setProtocol' });
  [['legacy', 'legacy（默认）'], ['agui', 'agui（AG-UI 事件）']].forEach(function (pair) {
    const option = el('option', { value: pair[0], text: pair[1] });
    if (state.settings.protocol === pair[0]) option.selected = true;
    protocol.appendChild(option);
  });
  protocol.addEventListener('change', function () {
    state.settings = store.saveSettings({ protocol: protocol.value });
    toast('流式协议已切换', 'ok');
  });
  panel.appendChild(field('流式协议', protocol, 'agui 走 AG-UI 事件子集（RUN/STEP/TEXT_MESSAGE/STATE_DELTA），前端已做映射。'));

  const testBtn = el('button', { class: 'btn', type: 'button' }, [icon('zap', { size: 14 }), el('span', { text: '测试连接' })]);
  testBtn.addEventListener('click', async function () {
    testBtn.disabled = true;
    try {
      const data = await api.health();
      toast('服务正常 · 版本 ' + ((data && data.version) || '未知'), 'ok');
    } catch (err) {
      toast((err && err.message) || '连接失败', 'bad');
    } finally {
      testBtn.disabled = false;
    }
  });
  panel.appendChild(field('连通性', testBtn, 'GET /health/live，不走鉴权。'));

  panel.appendChild(el('div', { class: 'section-head' }, [el('span', { text: '界面' })]));
  panel.appendChild(switchRow('显示回复元信息', '意图、合规结果、耗时与字数', state.settings.showMeta, function (next) {
    state.settings = store.saveSettings({ showMeta: next });
    renderTranscript();
  }));
  panel.appendChild(switchRow('自动跟随最新消息', '向下滚动时保持吸底；向上翻阅时不打断', state.settings.autoScroll, function (next) {
    state.settings = store.saveSettings({ autoScroll: next });
    stick = next;
  }));

  panel.appendChild(el('div', { class: 'section-head' }, [el('span', { text: '快捷键' })]));
  panel.appendChild(el('div', { class: 'kv' }, [
    el('div', { class: 'kv-row' }, [el('dt', { text: 'Ctrl K' }), el('dd', { text: '新建对话' })]),
    el('div', { class: 'kv-row' }, [el('dt', { text: 'Enter' }), el('dd', { text: '发送消息' })]),
    el('div', { class: 'kv-row' }, [el('dt', { text: 'Shift Enter' }), el('dd', { text: '换行' })]),
    el('div', { class: 'kv-row' }, [el('dt', { text: 'Esc' }), el('dd', { text: '关闭面板' })]),
  ]));

  panel.appendChild(el('div', { class: 'section-head' }, [el('span', { text: '本地数据' })]));
  const clearBtn = el('button', { class: 'btn btn-bad', type: 'button' }, [icon('trash', { size: 14 }), el('span', { text: '清空本地会话与设置' })]);
  let armed = false;
  clearBtn.addEventListener('click', function () {
    if (!armed) {
      armed = true;
      clearBtn.lastChild.textContent = '再次点击确认清空';
      window.setTimeout(function () { armed = false; clearBtn.lastChild.textContent = '清空本地会话与设置'; }, 3200);
      return;
    }
    try {
      window.localStorage.removeItem('ha.conversations.v2');
      window.localStorage.removeItem('ha.settings.v2');
    } catch (err) { /* ignore */ }
    window.location.reload();
  });
  panel.appendChild(field('重置', clearBtn, '清除本机保存的会话、主题与 API Key。'));
}

/* ── health ────────────────────────────────────────────────── */
async function checkHealth(initial) {
  try {
    const data = await api.health();
    state.health = { ok: true, version: (data && data.version) || '' };
    refs.healthFoot.dataset.health = 'ok';
    refs.healthText.textContent = '服务运行中 · 合规审查已启用';
    refs.healthVer.textContent = state.health.version ? 'v' + state.health.version : '';
    setBanner(false);
  } catch (err) {
    state.health = { ok: false, version: '' };
    refs.healthFoot.dataset.health = 'down';
    refs.healthText.textContent = '服务不可达';
    refs.healthVer.textContent = '';
    if (navigator.onLine === false) {
      setBanner(true, '网络已断开，恢复后可以继续发送。');
    } else {
      setBanner(true, '后端暂时不可达：确认服务已启动，页面每 30 秒自动重试。');
    }
  }
}

function setBanner(show, text) {
  refs.banner.dataset.visible = show ? 'true' : 'false';
  if (text) refs.bannerText.textContent = text;
  else if (!show) refs.bannerText.textContent = '网络已断开，恢复后可以继续发送。';
}

/* ── events ────────────────────────────────────────────────── */
function bindEvents() {
  refs.newChat.addEventListener('click', startNewChat);
  refs.themeToggle.addEventListener('click', function () {
    setTheme(resolvedTheme() === 'dark' ? 'light' : 'dark');
  });
  refs.settingsBtn.addEventListener('click', function () { openDrawer('settings'); });
  refs.railSettings.addEventListener('click', function () { openDrawer('settings'); });
  refs.approvalsBtn.addEventListener('click', function () { openDrawer('approvals'); });
  refs.drawerClose.addEventListener('click', closeDrawer);
  refs.railToggle.addEventListener('click', function () {
    const open = refs.rail.getAttribute('data-open') !== 'true';
    refs.rail.setAttribute('data-open', open ? 'true' : 'false');
    refs.railToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    showScrim(open);
  });
  refs.scrim.addEventListener('click', function () {
    closeRail();
    if (window.innerWidth <= 960) closeDrawer();
  });

  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () { selectTab(btn.dataset.tab); });
  });

  refs.welcomeGrid.addEventListener('click', function (event) {
    const card = event.target.closest('.prompt-card');
    if (!card) return;
    sendMessage(card.dataset.prompt || '');
  });

  refs.composerForm.addEventListener('submit', function (event) {
    event.preventDefault();
    submitComposer();
  });
  refs.composerInput.addEventListener('input', function () {
    autoGrow();
    updateCounter();
  });
  refs.composerInput.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' || event.shiftKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submitComposer();
  });
  refs.stopBtn.addEventListener('click', function () {
    if (state.streaming) state.streaming.controller.abort();
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Tab' && isDrawerOpen() && window.innerWidth <= 960) {
      const items = refs.drawer.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (items.length) {
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    const meta = event.ctrlKey || event.metaKey;
    if (meta && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      startNewChat();
    } else if (event.key === 'Escape') {
      closeRail();
      closeDrawer();
    }
  });

  window.addEventListener('resize', function () {
    if (window.innerWidth > 960) {
      refs.rail.setAttribute('data-open', 'false');
      showScrim(false);
    }
  });

  window.addEventListener('online', function () { setBanner(false); checkHealth(); });
  window.addEventListener('offline', function () { setBanner(true); });
  window.addEventListener('beforeunload', function () { store.persistNow(state.conv); });

  try {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function () {
      if (state.settings.theme === 'system') {
        store.applyTheme('system');
        updateThemeIcon();
      }
    });
  } catch (err) { /* older browsers */ }
}

/* ── boot ──────────────────────────────────────────────────── */
function init() {
  hydrateIcons(document);
  store.applyTheme(state.settings.theme);
  updateThemeIcon();
  state.sessionId = conversationIds()[0] || store.newSessionId();
  stick = state.settings.autoScroll !== false;
  renderConversations();
  renderTranscript();
  bindEvents();
  checkHealth(true);
  refreshApprovals({ silent: true });
  window.setInterval(function () { checkHealth(); }, 30000);
  window.setInterval(function () { refreshApprovals({ silent: true }); }, 60000);
  if (!conversationIds().length) refs.composerInput.focus();
  setLive('界面已就绪');
}

init();
