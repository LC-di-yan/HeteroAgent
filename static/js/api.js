/* ────────────────────────────────────────────────────────────────
   API client: SSE stream + JSON endpoints
   - resilient SSE framing (CRLF / multi-line data / trailing chunk)
   - typed errors that the UI turns into inline states, never alert()
   - optional x-api-key so deployments with API_KEYS keep working
   ──────────────────────────────────────────────────────────────── */

export class ApiError extends Error {
  constructor(status, message, extra) {
    super(message);
    this.name = 'ApiError';
    this.status = status || 0;
    const opts = extra || {};
    this.retryAfter = opts.retryAfter || 0;
    this.network = !!opts.network;
    this.requestId = opts.requestId || '';
    this.detail = opts.detail || null;
  }
}

function authHeaders(apiKey) {
  const headers = {};
  if (apiKey) headers['x-api-key'] = apiKey;
  return headers;
}

async function readError(res) {
  let detail = null;
  let message = '';
  try {
    const text = await res.text();
    if (text) {
      try {
        const body = JSON.parse(text);
        detail = body;
        if (typeof body.detail === 'string') message = body.detail;
        else if (Array.isArray(body.detail) && body.detail[0] && body.detail[0].msg) {
          message = body.detail[0].msg;
        } else if (body.detail && typeof body.detail === 'object') {
          message = body.detail.message || JSON.stringify(body.detail);
        }
      } catch (err) {
        message = text.slice(0, 200);
      }
    }
  } catch (err) { /* body already consumed */ }

  const status = res.status;
  const retryAfter = Number(res.headers.get('retry-after') || 0);
  const requestId = res.headers.get('x-request-id') || '';
  if (!message) {
    if (status === 401) message = '缺少或无效的 API Key';
    else if (status === 403) message = '没有访问权限';
    else if (status === 404) message = '接口不存在';
    else if (status === 413) message = '消息超出长度上限';
    else if (status === 429) message = '请求过于频繁，请稍后重试';
    else if (status === 503) message = '服务未就绪：后端模型或向量库未配置';
    else if (status >= 500) message = '服务内部错误（HTTP ' + status + '）';
    else message = '请求失败（HTTP ' + status + '）';
  }
  return new ApiError(status, message, { retryAfter: retryAfter, requestId: requestId, detail: detail });
}

function timeoutSignal(ms, outer) {
  const controller = new AbortController();
  const timer = window.setTimeout(function () { controller.abort(); }, ms);
  const cleanup = function () { window.clearTimeout(timer); };
  if (outer) {
    if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.any === 'function') {
      return { signal: AbortSignal.any([outer, controller.signal]), cleanup: cleanup };
    }
    if (outer.aborted) controller.abort();
    outer.addEventListener('abort', function () { controller.abort(); }, { once: true });
  }
  return { signal: controller.signal, cleanup: cleanup };
}

async function requestJson(path, options) {
  const opts = options || {};
  const apiKey = opts.apiKey || '';
  const timed = timeoutSignal(opts.timeoutMs || 10000, opts.signal);
  let res;
  try {
    res = await fetch(path, {
      method: opts.method || 'GET',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders(apiKey)),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: timed.signal,
      cache: 'no-store',
    });
  } catch (err) {
    timed.cleanup();
    if (opts.signal && opts.signal.aborted) throw err;
    throw new ApiError(0, '无法连接服务，请检查后端是否在运行', { network: true });
  }
  timed.cleanup();
  if (!res.ok) throw await readError(res);
  if (res.status === 204) return null;
  try {
    return await res.json();
  } catch (err) {
    return null;
  }
}

/* ── SSE framing ───────────────────────────────────────────── */

const BOUNDARY_RE = /\r\n\r\n|\n\n|\r\r/;

function dataOf(block) {
  const lines = block.split(/\r\n|\n|\r/);
  const chunks = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.startsWith('data:')) {
      chunks.push(line.slice(5).replace(/^ /, ''));
    }
  }
  return chunks.join('\n');
}

/* AG-UI 子集 → 内部事件。后端 events/agui.py 输出 RUN_/STEP_/TEXT_MESSAGE_/STATE_DELTA/RUN_ERROR。 */
function createAguiTranslator() {
  const state = { text: '', intent: 'unknown', compliance: true, approvalId: null };
  return function translate(event) {
    if (!event || typeof event.type !== 'string') return null;
    if (event.type === 'RUN_STARTED') {
      return { type: 'session', session_id: event.threadId };
    }
    if (event.type === 'STEP_STARTED') {
      const label = (event.metadata && event.metadata.label) || event.stepName;
      return { type: 'stage', node: event.stepName, label: label };
    }
    if (event.type === 'TEXT_MESSAGE_CONTENT') {
      const delta = event.delta || '';
      state.text += delta;
      return delta ? { type: 'token', text: delta } : null;
    }
    if (event.type === 'STATE_DELTA') {
      (event.delta || []).forEach(function (patch) {
        if (patch.path === '/intent') state.intent = patch.value;
        if (patch.path === '/compliancePassed') state.compliance = !!patch.value;
      });
      return null;
    }
    if (event.type === 'CUSTOM') {
      if (event.name === 'approval_required' && event.value) {
        state.approvalId = event.value.approvalId || null;
        if (event.value.intent) state.intent = event.value.intent;
      }
      return null;
    }
    if (event.type === 'RUN_ERROR') {
      return { type: 'error', message: event.message || '运行失败', status: event.code || 500 };
    }
    if (event.type === 'RUN_FINISHED') {
      return {
        type: 'response',
        response: state.text,
        intent: state.intent,
        compliance_passed: state.compliance,
        approval_id: state.approvalId || undefined,
      };
    }
    return null;
  };
}

export async function streamChat(opts) {
  const path = '/api/chat/stream' + (opts.protocol === 'agui' ? '?protocol=agui' : '');
  let res;
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json', Accept: 'text/event-stream' }, authHeaders(opts.apiKey)),
      body: JSON.stringify({
        message: opts.message,
        user_id: opts.userId || 'web-user',
        session_id: opts.sessionId,
      }),
      signal: opts.signal,
      cache: 'no-store',
    });
  } catch (err) {
    if (opts.signal && opts.signal.aborted) throw err;
    throw new ApiError(0, '无法连接服务，请检查后端是否在运行', { network: true });
  }
  if (!res.ok) throw await readError(res);
  if (!res.body) throw new ApiError(res.status, '当前浏览器不支持流式响应');

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let seen = 0;
  const translate = opts.protocol === 'agui' ? createAguiTranslator() : null;

  const dispatch = function (block) {
    const payload = dataOf(block);
    if (!payload || payload === '[DONE]') return;
    let event;
    try {
      event = JSON.parse(payload);
    } catch (err) {
      return;
    }
    seen += 1;
    const internal = translate ? translate(event) : event;
    if (internal && opts.onEvent) opts.onEvent(internal);
  };

  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let match = BOUNDARY_RE.exec(buffer);
      while (match) {
        const block = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        dispatch(block);
        match = BOUNDARY_RE.exec(buffer);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) dispatch(buffer);
  } finally {
    try { reader.releaseLock(); } catch (err) { /* noop */ }
  }
  return { events: seen };
}

/* ── Endpoints ─────────────────────────────────────────────── */

export function health() {
  return requestJson('/health/live', { timeoutMs: 5000 });
}

export function history(sessionId, apiKey) {
  return requestJson('/api/history/' + encodeURIComponent(sessionId), { apiKey: apiKey, timeoutMs: 8000 });
}

export function pendingApprovals(apiKey) {
  return requestJson('/api/approvals', { apiKey: apiKey, timeoutMs: 8000 });
}

export function approvalDetail(approvalId, apiKey) {
  return requestJson('/api/approvals/' + encodeURIComponent(approvalId), { apiKey: apiKey, timeoutMs: 8000 });
}

export function decideApproval(approvalId, action, reason, apiKey) {
  const verb = action === 'approve' ? 'approve' : 'reject';
  return requestJson('/api/approvals/' + encodeURIComponent(approvalId) + '/' + verb, {
    method: 'POST',
    body: { reason: reason || '' },
    apiKey: apiKey,
    timeoutMs: 15000,
  });
}

export function auditChain(apiKey) {
  return requestJson('/api/approvals/audit/chain', { apiKey: apiKey, timeoutMs: 10000 });
}

export function metrics(apiKey) {
  return requestJson('/api/metrics', { apiKey: apiKey, timeoutMs: 8000 });
}

export function userFacts(userId, apiKey) {
  return requestJson('/api/users/' + encodeURIComponent(userId) + '/facts', { apiKey: apiKey, timeoutMs: 8000 });
}
