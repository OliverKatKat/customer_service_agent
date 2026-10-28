
(() => {
  const $ = id => document.getElementById(id);
  let token = '', role = '', selectedId = null, selectedVersion = null, lastTicket = '', busy = false, detailLoading = false;
  const el = (tag, value, className) => {
    const node = document.createElement(tag);
    if (value != null) node.textContent = String(value);
    if (className) node.className = className;
    return node;
  };
  const clear = node => { while (node.firstChild) node.removeChild(node.firstChild); };
  function feedback(message, error = false) {
    $('feedback').textContent = message;
    $('feedback').classList.toggle('error', error);
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (token) headers.delete('Authorization');
    const response = await EchoSession.request(`/api/v1${path}`, {...options, headers});
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`;
      const error = new Error(detail); error.status = response.status; throw error;
    }
    return data;
  }
  function field(container, label, value) {
    const box = el('div'); box.append(el('small', label), el('strong', value ?? '—')); container.append(box);
  }
  async function loadList() {
    if (!token || role !== 'admin' || busy) return;
    try {
      const data = await api(`/reviews?state=${encodeURIComponent($('filter').value)}&limit=50`);
      clear($('reviewList'));
      if (!data.reviews.length) $('reviewList').append(el('p', '当前没有此状态的审核请求。'));
      for (const review of data.reviews) {
        const button = el('button', null, `review-entry${review.review_id === selectedId ? ' selected' : ''}`);
        button.type = 'button';
        button.append(el('b', `#${review.review_id} · 工单 ${review.ticket_id}`),
          el('span', `${review.order_id} · ¥${review.amount.toFixed(2)} · ${EchoUI.label(review.status)}`));
        button.addEventListener('click', () => loadDetail(review.review_id));
        $('reviewList').append(button);
      }
    } catch (error) { feedback(error.message, true); }
  }
  async function loadDetail(id) {
    if (detailLoading || busy) return;
    detailLoading = true;
    try {
      const data = await api(`/reviews/${id}`);
      selectedId = id;
      selectedVersion = data.review.version;
      const panel = $('detailCard'); clear(panel);
      panel.dataset.reviewStatus = data.review.status;
      panel.append(el('h2', `审核 #${id}`));
      const details = el('div', null, 'review-details');
      field(details, '状态', EchoUI.label(data.review.status));
      field(details, '工单号', data.review.ticket_id);
      field(details, '客户', data.review.customer_id);
      field(details, '订单号', data.review.order_id);
      field(details, '申请金额', `¥${data.review.amount.toFixed(2)}`);
      field(details, '订单状态', EchoUI.label(data.evidence.order_status));
      field(details, '工单状态', EchoUI.label(data.evidence.ticket_status));
      field(details, '审核版本', data.review.version);
      field(details, '处理进度', data.processing_state === 'completed' ? '原型退款已完成'
        : data.processing_state === 'needs_attention' ? '自动恢复未完成，请人工核查'
        : data.processing_state === 'processing' ? '批准后处理中'
        : (data.review.status === 'rejected' ? '已驳回' : '等待人工审核'));
      if (data.recovery_attempts) field(details, '恢复尝试', data.recovery_attempts);
      panel.append(details);
      const evidence = el('div', null, 'evidence-box');
      evidence.append(el('h3', '核对依据'));
      evidence.append(el('div', `申请说明：${data.review.reason || '—'}`));
      evidence.append(el('div', `工单标题：${data.evidence.ticket_title || '—'}`));
      evidence.append(el('div', `客户描述：${data.evidence.ticket_description || '—'}`));
      evidence.append(el('div', `订单金额：¥${data.evidence.order_amount ?? '—'}`));
      panel.append(evidence);
      if(data.case_id)panel.append(EchoUI.link('查看关联案件与政策 →','/workbench?case_id='+encodeURIComponent(data.case_id),'text-link'));

      if (data.review.status === 'pending') {
        const form = el('div', null, 'review-actions');
        const reasonLabel = el('label', '审核理由（驳回时必填）');
        const reason = el('textarea'); reason.id='decisionReason'; reasonLabel.htmlFor=reason.id; reason.maxLength = 500; reason.placeholder = '核对订单、证据后填写结论';
        const codeLabel = el('label', '管理员二次审批凭据');
        const code = el('input'); code.id='approvalCode'; codeLabel.htmlFor=code.id; code.type = 'password'; code.autocomplete = 'off';
        const actions = el('div', null, 'row');
        const approve = el('button', '批准申请','primary'); approve.type='button';
        const reject = el('button', '驳回申请', 'danger-button'); reject.type='button';
        approve.addEventListener('click', () => decide('approve', reason.value, code.value));
        reject.addEventListener('click', () => decide('reject', reason.value, code.value));
        actions.append(approve, reject);
        form.append(reasonLabel, reason, codeLabel, code, actions);
        panel.append(form);
      } else {
        panel.append(el('p', `处理人：${data.review.decision_actor_id || '—'}；原因：${data.review.decision_reason || '—'}`));
      }
      panel.append(el('h3', '审计记录'));
      for (const event of data.audit) panel.append(el('div',
        `${event.created_at} · ${event.action} · ${event.actor_id}${event.reason ? ' · ' + event.reason : ''}`, 'audit-item'));
      await loadList();
    } catch (error) { feedback(error.message, true); }
    finally { detailLoading = false; }
  }
  async function decide(action, reason, code) {
    if (busy || selectedId == null) return;
    if (code.length < 16) { feedback('请填写管理员二次审批凭据。', true); return; }
    if (action === 'reject' && reason.trim().length < 3) { feedback('驳回理由至少 3 个字符。', true); return; }
    busy = true; $('detailCard').querySelectorAll('button').forEach(b=>b.disabled=true);
    try {
      const result = await api(`/reviews/${selectedId}/${action}`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({approval_code:code, expected_version:selectedVersion, reason:reason.trim()})
      });
      feedback(result.status === 'approved' ? '已批准，后台正在继续处理。' : '已驳回，订单未修改。');
      busy=false; await loadDetail(selectedId);
    } catch (error) {
      feedback(error.message + ' 请刷新审核详情后再试。', true);
      if (error.status === 409) {busy=false; await loadDetail(selectedId);}
    }
    finally { busy = false; $('detailCard').querySelectorAll('button').forEach(b=>b.disabled=false); await loadList(); }
  }
  EchoSession.requireSession().then(async me=>{
    if(!me)return;token='shared';role=me.role;EchoUI.shell(me,'review');await loadList();
    const id=new URLSearchParams(location.search).get('review_id');if(id && /^\d+$/.test(id))await loadDetail(Number(id));
  }).catch(EchoUI.bootError);
  document.addEventListener('echo:logout',()=>{token='';role='';selectedId=null;clear($('detailCard'));clear($('reviewList'));});
  $('refresh').addEventListener('click', loadList);
  $('filter').addEventListener('change', loadList);

  setInterval(() => {
    if (document.hidden || busy) return;
    if (role === 'admin') {
      if (selectedId != null && $('detailCard').dataset.reviewStatus === 'approved') loadDetail(selectedId);
      else loadList();
    }
  }, 8000);
})();
