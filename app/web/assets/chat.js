
(() => {
  const $ = id => document.getElementById(id);
  let token = '', role = '', tenantId = '', lastTicket = '', busy = false, activeThinking = null;
  let epoch = 0, activeController = null;
  const initial = new URLSearchParams(location.search);

  const el = (tag, text, className) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = String(text);
    if (className) n.className = className;
    return n;
  };
  function feedback(message, error = false) { $('feedback').textContent = message; $('feedback').classList.toggle('error', error); }

  async function api(path, options = {}) {
    const requestEpoch = epoch;
    const headers = new Headers(options.headers || {});
    if (token) headers.delete('Authorization');
    const response = await EchoSession.request(`/api/v1${path}`, {...options, headers});
    let data; try { data = await response.json(); } catch { data = {}; }
    if (requestEpoch !== epoch) throw new DOMException('会话已变化', 'AbortError');
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`;
      const err = new Error(detail); err.status = response.status; throw err;
    }
    return data;
  }

  function addBubble(kind, text, extra) {
    const node = el('div', text, `msg ${kind}`);
    if (extra) node.append(el('small', extra));
    $('chatWelcome')?.remove();
    $('messages').append(node);
    $('messages').scrollTop = $('messages').scrollHeight;
    return node;
  }
  function startThinking() {
    const node = el('div', null, 'thinking');
    const head = el('div', null, 'head');
    head.append(el('span', null, 'spinner'), el('span', '客服 Agent 处理中…'));
    const steps = el('ul', null, 'steps');
    node.append(head, steps);
    $('chatWelcome')?.remove();
    $('messages').append(node);
    $('messages').scrollTop = $('messages').scrollHeight;
    activeThinking = { node, steps };
  }
  function addStep(message, state = '') {
    if (!activeThinking) return;
    const li = el('li', message, state);
    activeThinking.steps.append(li);
    $('messages').scrollTop = $('messages').scrollHeight;
  }
  function finishThinking() {
    if (activeThinking) {
      const record=el('details',null,'thinking');
      record.append(el('summary','本次处理记录'),activeThinking.steps);
      activeThinking.node.replaceWith(record); activeThinking = null;
    }
  }

  async function streamTask(taskId, onStep, onDone, signal, requestEpoch) {
    const response = await EchoSession.request(`/api/v1/task/${taskId}/events`, {
      headers: { 'Accept': 'text/event-stream' }, signal
    });
    if (!response.ok || !response.body) throw new Error(`进度连接失败（HTTP ${response.status}）`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let completed = false;
    while (true) {
      const { done, value } = await reader.read();
      if (done || requestEpoch !== epoch) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop();
      for (const part of parts) {
        let event = 'message', data = '';
        for (const line of part.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;
        let payload; try { payload = JSON.parse(data); } catch { continue; }
        if (event === 'completed' || payload.status === 'completed' || payload.status === 'degraded') { completed=true; onDone(payload); await reader.cancel(); return; }
        if (event === 'failed' || payload.status === 'failed') { completed=true; onDone(payload); await reader.cancel(); return; }
        onStep(payload);
      }
    }
    if (!completed && requestEpoch === epoch) throw new Error('进度连接已结束，请稍后查询审核进度或重试。');
  }

  async function send() {
    const topic = $('message').value.trim();
    const orderId = $('orderId').value.trim();
    const caseId = $('caseId').value.trim();
    if (busy || !topic) return;
    const requestEpoch = epoch;
    activeController = new AbortController();
    busy = true; $('send').disabled = true; $('orderId').disabled=true; $('caseId').disabled=true; feedback('');
    const orderTag = orderId ? `（订单 ${orderId}）` : '';
    addBubble('user', topic, orderTag || '');
    $('message').value = '';
    startThinking();
    try {
      const res = await api('/submit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, order_id: orderId, ...(caseId ? { case_id: caseId } : {}) }),
        signal: activeController.signal
      });
      lastTicket = res.ticket_id;
      if (lastTicket) {
        $('ticketChip').hidden=false;
        $('ticketChip').replaceChildren();
        $('ticketChip').append(el('span', `工单 #${lastTicket}`));
        const btn = el('button', '查审核进度');
        btn.addEventListener('click', () => lookupReview(lastTicket));
        $('ticketChip').append(btn);
        if (res.case_id) {
          $('ticketChip').append(el('span', `申请 ${res.case_id}`));
          const keep = el('button', '继续此申请');
          keep.type = 'button';
          keep.addEventListener('click', () => { $('caseId').value = res.case_id; $('orderId').value = orderId; updateAssociation(); feedback('后续消息将关联此申请；不同诉求请点击新建申请。'); });
          $('ticketChip').append(keep);
        }
      }
      await streamTask(res.task_id,
        (payload) => addStep(payload.message || payload.status || '处理中'),
        (payload) => {
          finishThinking();
          if (payload.status === 'failed') addBubble('error', payload.message || payload.result || '处理失败');
          else addBubble('agent', payload.result || payload.message || '处理完成');
        }, activeController.signal, requestEpoch
      );
    } catch (error) {
      if (error.name === 'AbortError' || requestEpoch !== epoch) return;
      finishThinking();
      addBubble('error', error.message);
      feedback(error.message, true);
    } finally {
      if (requestEpoch === epoch) { busy = false; $('send').disabled = false; $('orderId').disabled=false; $('caseId').disabled=false; activeController = null; }
    }
  }

  async function lookupReview(ticketId) {
    try {
      const data = await api(`/tickets/${encodeURIComponent(ticketId)}/review`);
      addBubble('agent', `${data.message}`, `工单 ${data.ticket_id} · 审核状态 ${data.review_status}`);
    } catch (error) {
      if (error.name === 'AbortError') return;
      if (error.status === 404) addBubble('agent', '该工单还没有退款审核（普通咨询不进入人工审核）。');
      else { feedback(error.message, true); addBubble('error', error.message); }
    }
  }

  const scenes={
    refund:{order:'DEMO-ECHO-001',case:'CASE-DEMO-REFUND',text:'我买错型号了，想取消未发货的订单，请核对退款申请。'},
    shipped:{order:'DEMO-ECHO-002',case:'',text:'我的订单已经发货了，现在想退款，该怎么处理？'},
    repeat:{order:'DEMO-ECHO-003',case:'CASE-DEMO-REPEAT',text:'补充上次申请：耳机右侧没有声音，重新配对后依然如此。'}
  };
  function updateAssociation(){ $('associationHint').textContent=$('caseId').value.trim()?'后续消息将关联此申请，请核对订单与申请编号。':'未填写申请编号时，将创建新的售后申请。'; }
  $('caseId').addEventListener('input',updateAssociation);
  function chooseScene(key,initialLoad=false){
    if(busy)return;const scene=scenes[key];if(!scene)return;
    if(!initialLoad || !initial.has('order_id'))$('orderId').value=scene.order;
    if(!initialLoad || !initial.has('case_id'))$('caseId').value=scene.case;
    $('message').value=scene.text;updateAssociation();feedback('示例已填入，核对后点击发送。');
  }
  document.querySelectorAll('[data-scene]').forEach(b=>b.onclick=()=>chooseScene(b.dataset.scene));
  EchoSession.requireSession().then(me => {
    if (!me) return;
    token = 'shared'; role = me.role; tenantId = me.tenant_id;
    EchoUI.shell(me,'chat');
    $('orderId').value = (initial.get('order_id') || '').slice(0,100);
    $('caseId').value = (initial.get('case_id') || '').slice(0,64);
    const scene=initial.get('scene'); if(scene && scenes[scene]) chooseScene(scene, true);
    updateAssociation();
  }).catch(EchoUI.bootError);
  document.addEventListener('echo:logout', () => {
    epoch++; activeController?.abort(); token=''; busy=false;
    $('messages').replaceChildren(); $('ticketChip').replaceChildren();
    $('caseId').value=''; $('orderId').value=''; $('message').value='';
  });
  $('newRequest').addEventListener('click', () => { if(busy)return; $('caseId').value = ''; updateAssociation(); feedback('已切换为新申请；后续提交不会自动合并到上一申请。'); });
  $('orderId').addEventListener('input', () => { $('caseId').value = ''; updateAssociation(); feedback('订单已切换；如需继续原申请，请重新核对申请编号。'); });
  $('sendForm').addEventListener('submit', e=>{e.preventDefault();send();});
  $('message').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
})();
