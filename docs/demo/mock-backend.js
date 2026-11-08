/* ────────────────────────────────────────────────────────────────
   HeteroAgent · 静态演示的“后端”
   在浏览器里拦截 fetch，用脚本化数据模拟 /api/*：
   - SSE 阶段 + token 流
   - 合规拦截与人工审批
   - 服务端历史 / 指标 / 审计链
   只用于 GitHub Pages 上的 UI 演示，不调用真实模型，也不含任何密钥。
   ──────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  var sessions = Object.create(null);
  var approvals = Object.create(null);
  var auditRows = [];
  var stats = { calls: 0, tokens: 0, byNode: {} };

  function sleep(ms) {
    return new Promise(function (resolve) { window.setTimeout(resolve, ms); });
  }

  function json(data, status) {
    return new Response(JSON.stringify(data), {
      status: status || 200,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  }

  function sse(items) {
    var encoder = new TextEncoder();
    var stream = new ReadableStream({
      start: function (controller) {
        var index = 0;
        function step() {
          if (index >= items.length) { controller.close(); return; }
          var item = items[index];
          index += 1;
          window.setTimeout(function () {
            controller.enqueue(encoder.encode('data: ' + JSON.stringify(item.payload) + '\n\n'));
            step();
          }, item.delay || 0);
        }
        step();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache' },
    });
  }

  function touchNode(node) {
    stats.byNode[node] = (stats.byNode[node] || 0) + 1;
  }

  function history(sid) {
    if (!sessions[sid]) sessions[sid] = [];
    return sessions[sid];
  }

  function makeApproval(sid, intent) {
    var stamp = Date.now().toString(36).toUpperCase();
    var id = 'demo-' + stamp + '-' + Math.random().toString(16).slice(2, 10);
    approvals[id] = {
      approval_id: id,
      session_id: sid,
      risk_level: 'high',
      violations: ['承诺收益', '未提示风险', '绝对化用语', '缺少免责声明'],
      intent: intent,
      created_at: Math.floor(Date.now() / 1000),
      expires_at: Math.floor(Date.now() / 1000) + 900,
    };
    return id;
  }

  var SCRIPTS = {
    guarantee: {
      intent: 'knowledge_rag',
      stages: ['supervisor_route', 'knowledge_rag', 'compliance_check'],
      tokens: [],
      notice: true,
    },
    refund: {
      intent: 'ticket_handler',
      stages: ['supervisor_route', 'ticket_handler', 'compliance_check', 'synthesize'],
      text: '工单已创建。\n\n- 工单号：**TK-20261108-7F3A9C**\n- 类型：退款申请\n- 优先级：中\n- 状态：待受理\n\n购买后 7 天内可申请无理由退款，超过 7 天需说明原因；退款会在 3–5 个工作日原路退回。',
      tokens: ['工单已创建。\n\n', '- 工单号：**TK-20261108-7F3A9C**\n', '- 类型：退款申请\n', '- 优先级：中\n', '- 状态：待受理\n\n', '购买后 7 天内可申请无理由退款，超过 7 天需说明原因；', '退款会在 3–5 个工作日原路退回。'],
    },
    account: {
      intent: 'knowledge_rag',
      stages: ['supervisor_route', 'knowledge_rag', 'compliance_check', 'synthesize'],
      text: '开户需要准备：\n\n1. 身份证原件\n2. 本人银行卡\n3. 完成视频认证\n4. 设置交易密码\n5. 填写风险测评问卷\n\n整个流程约 15–30 分钟，全程线上办理。\n\n> 来源：account_guide.md',
      tokens: ['开户需要准备：\n\n', '1. 身份证原件\n2. 本人银行卡\n', '3. 完成视频认证\n', '4. 设置交易密码\n', '5. 填写风险测评问卷\n\n', '整个流程约 15–30 分钟，全程线上办理。\n\n', '> 来源：account_guide.md'],
    },
    order: {
      intent: 'ticket_handler',
      stages: ['supervisor_route', 'ticket_handler', 'compliance_check', 'synthesize'],
      text: '订单 **SO-20261106-58D2** 当前状态：已发货。\n\n- 承运商：顺丰速运\n- 预计送达：11 月 9 日\n- 收货地址：已按用户档案脱敏显示\n\n如需修改地址，请在签收前提交工单。',
      tokens: ['订单 **SO-20261106-58D2** 当前状态：已发货。\n\n', '- 承运商：顺丰速运\n', '- 预计送达：11 月 9 日\n', '- 收货地址：已按用户档案脱敏显示\n\n', '如需修改地址，请在签收前提交工单。'],
    },
    product: {
      intent: 'knowledge_rag',
      stages: ['supervisor_route', 'knowledge_rag', 'compliance_check', 'synthesize'],
      text: '## 结论\n\n理财产品 A 的年化收益率为 **3.5%–5.2%**，期限 6 个月至 3 年，最低 10,000 元起投。\n\n- 收益为业绩比较基准，不代表实际收益\n- 理财非存款，产品有风险\n- 以上信息仅供参考，具体以合同条款为准\n\n> 来源：product_faq.md',
      tokens: ['## 结论\n\n', '理财产品 A 的年化收益率为 ', '**3.5%–5.2%**，', '期限 6 个月至 3 年，', '最低 10,000 元起投。\n\n', '- 收益为业绩比较基准，不代表实际收益\n', '- 理财非存款，产品有风险\n', '- 以上信息仅供参考，具体以合同条款为准\n\n', '> 来源：product_faq.md'],
    },
  };

  function pick(message) {
    if (/保证|稳赚|零风险|保本|保息/.test(message)) return 'guarantee';
    if (/退款|退货|售后/.test(message)) return 'refund';
    if (/开户|开通|材料/.test(message)) return 'account';
    if (/订单|物流|发货/.test(message)) return 'order';
    return 'product';
  }

  function chat(body) {
    var message = String((body && body.message) || '');
    var sid = (body && body.session_id) || 'demo-session';
    var userId = (body && body.user_id) || 'demo-user';
    var list = history(sid);
    list.push({ role: 'user', content: message });
    var key = pick(message);
    var script = SCRIPTS[key];
    var items = [];
    var intent = script.intent;

    items.push({ delay: 120, payload: { type: 'session', session_id: sid } });
    script.stages.forEach(function (node, index) {
      items.push({
        delay: index === 0 ? 260 : 420,
        payload: { type: 'stage', node: node, label: node },
      });
      touchNode(node);
    });

    if (key === 'guarantee') {
      var approvalId = makeApproval(sid, 'knowledge_rag');
      var notice = '您的请求涉及敏感内容或需要人工确认，已转入人工审核流程。审批编号：' + approvalId + '。人工确认后我们会尽快回复您。';
      list.push({ role: 'assistant', content: notice });
      items.push({ delay: 700, payload: { type: 'response', response: notice, intent: 'knowledge_rag', compliance_passed: false, approval_id: approvalId } });
      items.push({ delay: 40, payload: { type: 'done' } });
      stats.calls += 1;
      return items;
    }

    var tokens = script.tokens || [];
    tokens.forEach(function (chunk) {
      items.push({ delay: 90 + Math.random() * 80, payload: { type: 'token', text: chunk } });
      stats.tokens += chunk.length;
    });
    list.push({ role: 'assistant', content: script.text });
    items.push({
      delay: 500,
      payload: { type: 'response', response: script.text, intent: intent, compliance_passed: true, cache_hit: false },
    });
    items.push({ delay: 40, payload: { type: 'done' } });
    stats.calls += 1;
    return items;
  }

  function metrics() {
    var nodes = ['supervisor_route', 'knowledge_rag', 'ticket_handler', 'compliance_check', 'synthesize'];
    var calls = Math.max(stats.calls, 1);
    var summary = {};
    nodes.forEach(function (node, index) {
      summary[node] = {
        total_calls: stats.byNode[node] || Math.max(1, Math.round(calls * (1 - index * 0.12))),
        p95_duration_ms: [412, 1830, 960, 640, 2210][index],
        error_rate: node === 'compliance_check' ? 0.06 : 0,
        input_tokens: 0,
        output_tokens: 0,
        total_tokens: [0, 0, 0, 0, 0][index],
        estimated_cost_usd: 0,
      };
    });
    return {
      agent_metrics: summary,
      circuit_breaker: { knowledge_search: { state: 'closed', consecutive_failures: 0, failure_threshold: 3, cooldown_remaining_s: 0 } },
      tool_call_log: [{ name: 'knowledge_search' }],
    };
  }

  function pending() {
    var now = Math.floor(Date.now() / 1000);
    var reviews = Object.keys(approvals)
      .map(function (id) { return approvals[id]; })
      .filter(function (item) { return !item.decided && item.expires_at > now; });
    return { total: reviews.length, reviews: reviews };
  }

  function decide(id, action, body) {
    var record = approvals[id];
    if (!record) return json({ detail: '审批单不存在' }, 404);
    if (record.decided) return json({ detail: '审批单已处理' }, 409);
    record.decided = action;
    record.reason = (body && body.reason) || '';
    auditRows.push({ approval_id: id, action: action, at: Date.now(), reason: record.reason });
    var text = action === 'approve'
      ? '抱歉，我们无法保证收益。产品 A 的年化收益率为 3.5%–5.2%，理财非存款，存在本金波动风险，具体以合同条款为准。\n\n> 人工审批：已确认（演示）'
      : '该请求已被人工拒绝，相关内容不会再发送给用户。\n\n> 人工审批：已拒绝（演示）';
    var list = history(record.session_id);
    list.push({ role: 'assistant', content: text });
    return json({ response: text, approval_id: id, decision: action });
  }

  function strip(path) {
    return path.replace(/^https?:\/\/[^/]+/, '');
  }

  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var path = strip(url);
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();

    if (path.indexOf('/health/live') === 0) {
      return Promise.resolve(json({ status: 'live', version: '0.4.0-demo' }));
    }
    if (path.indexOf('/api/chat/stream') === 0) {
      var body = {};
      try { body = JSON.parse((init && init.body) || '{}'); } catch (err) { /* noop */ }
      if (method === 'POST') return Promise.resolve(sse(chat(body)));
    }
    if (path.indexOf('/api/history/') === 0) {
      var sid = decodeURIComponent(path.slice('/api/history/'.length));
      return Promise.resolve(json({ session_id: sid, messages: history(sid) }));
    }
    if (path === '/api/approvals' || path.indexOf('/api/approvals?') === 0) {
      return Promise.resolve(json(pending()));
    }
    if (path.indexOf('/api/approvals/audit/chain') === 0) {
      return Promise.resolve(json({ valid: true, rows: auditRows }));
    }
    var decision = path.match(/^\/api\/approvals\/([^/]+)\/(approve|reject)$/);
    if (decision) {
      var payload = {};
      try { payload = JSON.parse((init && init.body) || '{}'); } catch (err) { /* noop */ }
      return Promise.resolve(decide(decodeURIComponent(decision[1]), decision[2], payload));
    }
    var detail = path.match(/^\/api\/approvals\/([^/]+)$/);
    if (detail) {
      var record = approvals[decodeURIComponent(detail[1])];
      return Promise.resolve(record ? json(record) : json({ detail: '审批单不存在' }, 404));
    }
    if (path === '/api/metrics') {
      return Promise.resolve(json(metrics()));
    }
    if (path.indexOf('/api/users/') === 0) {
      return Promise.resolve(json({ user_id: 'demo-user', facts: [] }));
    }
    return realFetch ? realFetch(input, init) : Promise.reject(new Error('network disabled'));
  };
})();
