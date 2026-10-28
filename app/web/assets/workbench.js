(() => {
  'use strict';
  const {$,el,clear,message,error,date,badge,label,link,facts}=EchoUI;
  const state={me:null,selected:null,rows:[],caseCursor:null,todoCursor:null,controllers:{},materialSeq:0};
  async function api(path,options={},slot){
    if(slot){state.controllers[slot]?.abort();const controller=new AbortController();state.controllers[slot]=controller;options.signal=controller.signal;}
    const result=await EchoSession.json('/api/v1'+path,options);
    if(options.signal?.aborted)throw new DOMException('请求已取消','AbortError');
    return result;
  }
  function markSelected(){document.querySelectorAll('[data-case]').forEach(b=>b.classList.toggle('selected',b.dataset.case===state.selected?.case_id));}
  function drawList(){
    clear($('caseList'));
    for(const c of state.rows){const b=el('button',null,'case-entry');b.type='button';b.dataset.case=c.case_id;b.append(el('span',c.case_id,'entry-id'));const row=el('span',null,'entry-meta');row.append(badge(c.status),el('span',label(c.case_type)));b.append(row,el('span',date(c.opened_at),'entry-date'));b.onclick=()=>loadDetail(c.case_id);$('caseList').append(b);}
    $('caseCount').textContent=state.rows.length+' 条已加载';markSelected();
  }
  async function loadCases(append=false,autoSelect=false){
    message('caseMessage','读取案件…');$('moreCases').hidden=true;
    if(!append){state.caseCursor=null;state.rows=[];drawList();}
    try{const q=new URLSearchParams({limit:'20'});if($('caseState').value)q.set('state',$('caseState').value);if(append&&state.caseCursor)q.set('cursor',state.caseCursor);
      const {data}=await api('/workbench/cases?'+q,{},'cases');state.rows.push(...data.cases);state.caseCursor=data.next_cursor;drawList();$('moreCases').hidden=!data.next_cursor;message('caseMessage',state.rows.length?'':'没有符合条件的案件。');
      if(autoSelect&&!state.selected&&state.rows.length)loadDetail((state.rows.find(c=>c.status==='open')||state.rows[0]).case_id);
    }catch(e){error('caseMessage',e);}
  }
  async function loadDetail(id){
    state.controllers.material?.abort();state.materialSeq++;state.selected=null;markSelected();
    $('caseDetail').hidden=true;$('caseSide').hidden=true;$('detailEmpty').hidden=false;$('detailEmpty').textContent='读取案件记录…';
    clear($('materialOutput'));$('materialOutput').hidden=true;message('materialMessage','');message('detailMessage','');$('generateMaterial').disabled=false;$('generateMaterial').textContent='✧ 整理审核材料';$('noteContent').value='';
    try{const {data}=await api('/workbench/cases/'+encodeURIComponent(id),{},'detail');state.selected=data;markSelected();$('caseDetail').hidden=false;$('caseSide').hidden=false;$('detailEmpty').hidden=true;
      history.replaceState(null,'','/workbench?case_id='+encodeURIComponent(id));
      const summary=$('caseSummary');clear(summary);const heading=el('div',null,'section-heading');heading.append(el('h2',data.case_id,'case-title'),badge(data.status));summary.append(heading,el('p',label(data.case_type)+' · 客户 '+data.customer_ref+' · '+date(data.opened_at),'case-subtitle'));
      $('contactCount').textContent=data.tickets.length+' 次联系';clear($('contacts'));
      for(const t of data.tickets){const n=el('article',null,'record'),head=el('div',null,'record-head');head.append(el('span','工单 #'+t.ticket_id),badge(t.status));n.append(head,el('h3',t.title));if(t.description)n.append(el('p',t.description,'text'));if(t.resolution)n.append(el('p',t.resolution,'text record-result'));$('contacts').append(n);}
      if(!data.tickets.length)$('contacts').append(el('p','暂无联系记录。','empty'));
      const order=data.order;facts($('caseFacts'),order?[['关联订单',order.order_id],['订单状态',label(order.status)],['订单金额','¥ '+order.legacy_amount]]:[['关联订单',data.order_id||'尚未关联'],['订单事实','暂未找到可核验的订单']]);
      clear($('reviews'));
      for(const r of data.reviews){const n=el('div',null,'item');n.append(badge(r.status),el('p','申请金额 ¥ '+r.requested_amount),el('p',label(r.execution_status),'muted'));
        if(r.decision_reason)n.append(el('p',r.decision_reason,'muted text'));
        if(state.me.role==='admin')n.append(link('核对审核详情 →','/review?review_id='+encodeURIComponent(r.review_id),'text-link'));
        else if(r.status==='pending')n.append(el('p','等待主管核对并决定。','hint'));
        $('reviews').append(n);}
      if(!data.reviews.length)$('reviews').append(el('p','暂无退款审核申请。','hint'));
      $('editType').value=data.case_type;$('editReason').value=data.reason_code||'';$('editStatus').value=data.status;$('editDecision').value='';$('caseForm').querySelector('button').disabled=data.status==='closed';
      clear($('caseTodos'));const terminal=['resolved','closed'].includes(data.status);
      for(const item of data.work_items){const n=el('div',null,'item');n.append(el('h3',label(item.kind)),el('p',(item.status==='done'?'已完成':'未完成')+' · '+(item.assigned_to||'待领取'),'muted'),el('p','接单目标 '+date(item.due_at),'muted'));
        if(!terminal){const actions=el('div',null,'actions');const take=el('button','领取给我'),done=el('button',item.status==='open'?'标记完成':'重新跟进');take.type=done.type='button';take.onclick=()=>updateItem(item,{assigned_to:state.me.username},take);done.onclick=()=>updateItem(item,{status:item.status==='open'?'done':'open'},done);actions.append(take,done);n.append(actions);}$('caseTodos').append(n);}
      clear($('notes'));for(const note of data.notes){const n=el('div',null,'item');n.append(el('div',date(note.created_at)+' · 人员 '+note.author_ref,'muted'),el('p',note.content,'text'));$('notes').append(n);}
      if(!data.notes.length)$('notes').append(el('p','暂无人工备注。','hint'));
      clear($('events'));for(const event of data.events){const n=el('div',null,'audit-item');n.append(el('span',date(event.created_at)+' · '+label(event.action)));if(event.payload.reason)n.append(el('p',event.payload.reason,'text'));$('events').append(n);}
      $('truncation').textContent=Object.values(data.truncated).some(Boolean)?'仅展示最近记录，完整历史仍保存在台账中。':'';
    }catch(e){if(e.name!=='AbortError'){$('detailEmpty').textContent=e.message;$('detailEmpty').classList.add('error');}}
  }
  async function updateItem(item,changes,button){
    const id=state.selected?.case_id;if(!id)return;button.disabled=true;
    try{await api('/workbench/work-items/'+item.id,{method:'PATCH',body:{expected_version:item.version,...changes}});if(state.selected?.case_id===id)await loadDetail(id);}
    catch(e){error('detailMessage',e);}finally{button.disabled=false;}
  }
  $('caseForm').onsubmit=async event=>{
    event.preventDefault();const c=state.selected;if(!c)return;const button=event.submitter;button.disabled=true;
    try{await api('/workbench/cases/'+encodeURIComponent(c.case_id),{method:'PATCH',body:{expected_version:c.version,case_type:$('editType').value,reason_code:$('editReason').value||null,status:$('editStatus').value,reason:$('editDecision').value.trim()}});if(state.selected?.case_id===c.case_id){await loadDetail(c.case_id);message('detailMessage','处理信息已保存。');}loadCases();}
    catch(e){error('detailMessage',e);}finally{button.disabled=state.selected?.status==='closed';}
  };
  $('noteForm').onsubmit=async event=>{
    event.preventDefault();const id=state.selected?.case_id;if(!id)return;const button=event.submitter;button.disabled=true;
    try{await api('/workbench/cases/'+encodeURIComponent(id)+'/notes',{method:'POST',body:{content:$('noteContent').value}});if(state.selected?.case_id===id){await loadDetail(id);message('detailMessage','备注已保存。');}}
    catch(e){error('detailMessage',e);}finally{button.disabled=false;}
  };
  $('generateMaterial').onclick=async()=>{
    const selected=state.selected;if(!selected)return;const seq=++state.materialSeq,button=$('generateMaterial');button.disabled=true;button.textContent='正在整理…';message('materialMessage','核对订单与本商户政策，整理审核草稿…');$('materialOutput').hidden=true;
    try{const {data}=await api('/workbench/cases/'+encodeURIComponent(selected.case_id)+'/material',{method:'POST',body:{use_model:true}},'material');
      if(seq!==state.materialSeq||state.selected?.case_id!==selected.case_id||state.selected.version!==selected.version)return;
      const out=$('materialOutput');clear(out);out.append(badge(data.mode==='ai_draft'?'approved':'open',data.mode==='ai_draft'?'AI 辅助草稿':data.mode==='template_fallback'?'事实模板 · 模型暂不可用':'事实材料'),el('h3','客户诉求'),el('p',data.draft.customer_summary,'text'),el('h3','建议下一步'),el('p',data.draft.suggested_next_step));
      data.draft.attention_points.forEach(t=>out.append(el('p',t,'text')));if(data.missing_info.length)out.append(el('p','待补充：'+data.missing_info.join('、'),'notice amber'));
      out.append(el('h3','政策依据'));for(const p of data.policy_evidence){const item=el('details',null,'policy-item');item.append(el('summary',p.evidence_id+' · '+p.section),el('p',p.text,'text'),el('p',p.source+' · 版本 '+p.version.slice(0,8),'hint'));out.append(item);}
      if(!data.policy_evidence.length)out.append(el('p','暂无本商户可引用的政策，请人工核实。','hint'));
      if(data.contacts_truncated)out.append(el('p','材料仅包含最近五次联系，请同时核对完整联系记录。','hint'));
      if(data.stale)out.append(el('p','案件在生成期间已更新，请重新整理。','error'));
      out.hidden=false;message('materialMessage','已整理 · '+date(data.generated_at));
    }catch(e){error('materialMessage',e);}finally{if(seq===state.materialSeq){button.disabled=false;button.textContent='✧ 重新整理材料';}}
  };
  async function loadTodos(append=false){
    if(!append){clear($('todoList'));state.todoCursor=null;}message('todoMessage','读取待办…');$('moreTodos').hidden=true;
    try{const q=new URLSearchParams({state:$('todoState').value,overdue_only:String($('overdueOnly').checked)});if(append&&state.todoCursor)q.set('cursor',state.todoCursor);const {data}=await api('/workbench/todos?'+q,{},'todos');
      for(const item of data.items){const n=el('div',null,'todo-entry'),body=el('div');body.append(el('h3',item.case_id),el('p',label(item.kind)+' · '+(item.assigned_to||'待领取')+' · 接单目标 '+date(item.due_at)));if(item.overdue)body.append(badge('pending','已超过内部接单目标'));n.append(body,link('查看案件 →','/workbench?case_id='+encodeURIComponent(item.case_id)));$('todoList').append(n);}
      state.todoCursor=data.next_cursor;$('moreTodos').hidden=!data.next_cursor;message('todoMessage',$('todoList').childElementCount?'':'暂无符合条件的待办。');
    }catch(e){error('todoMessage',e);}
  }
  function countTable(id,rows){clear($(id));if(!rows.length){$(id).append(el('p','此区间暂无记录。','empty'));return;}const table=el('table'),head=el('tr');head.append(el('th','分类'),el('th','案件数'));table.append(head);for(const row of rows){const tr=el('tr');tr.append(el('td',label(row.value)),el('td',row.count));table.append(tr);}$(id).append(table);}
  async function loadAnalytics(){
    $('analyticsData').hidden=true;message('analyticsMessage','正在查询…');
    try{const q=new URLSearchParams({timezone:'Asia/Shanghai',start:$('startDate').value+'T00:00:00+08:00',end:$('endDate').value+'T00:00:00+08:00'});const {data,meta}=await api('/analytics/overview?'+q,{},'analytics');$('metricAll').textContent=data.cases_opened_all;$('metricClassified').textContent=data.after_sales_opened_classified;$('metricCurrent').textContent=data.open_cases_current;countTable('typesTable',data.by_case_type);countTable('reasonsTable',data.by_reason);$('analysisMeta').textContent='读取时间 '+date(meta.snapshot_at)+' · 业务时区 Asia/Shanghai。当前未结束案件为当前存量；未确认原因保留为未知。';$('analyticsData').hidden=false;message('analyticsMessage','');}
    catch(e){error('analyticsMessage',e);}
  }
  function switchTab(tab,first=false){
    const allowed=state.me.role==='analyst'?['analytics']:state.me.role==='admin'?['cases','todos','analytics']:['cases','todos'];if(!allowed.includes(tab))tab=allowed[0];
    document.querySelectorAll('[data-panel]').forEach(p=>p.hidden=p.dataset.panel!==tab);document.querySelectorAll('[data-tab]').forEach(b=>{b.hidden=!allowed.includes(b.dataset.tab);b.setAttribute('aria-selected',String(b.dataset.tab===tab));});EchoUI.activeNav(tab);
    if(tab==='cases'){const id=first?new URLSearchParams(location.search).get('case_id'):state.selected?.case_id;loadCases(false,!id);if(id)loadDetail(id);else history.replaceState(null,'','/workbench?tab=cases');}
    else{history.replaceState(null,'','/workbench?tab='+tab);if(tab==='todos')loadTodos();else loadAnalytics();}
  }
  document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
  $('reloadCases').onclick=()=>{loadCases();if(state.selected)loadDetail(state.selected.case_id);};$('caseState').onchange=()=>loadCases();$('moreCases').onclick=()=>loadCases(true);
  $('reloadTodos').onclick=()=>loadTodos();$('todoState').onchange=()=>loadTodos();$('overdueOnly').onchange=()=>loadTodos();$('moreTodos').onclick=()=>loadTodos(true);$('reloadAnalytics').onclick=loadAnalytics;
  const day=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}),now=new Date();$('startDate').value=day.format(new Date(now.getTime()-29*86400000));$('endDate').value=day.format(new Date(now.getTime()+86400000));
  document.addEventListener('echo:logout',()=>{Object.values(state.controllers).forEach(c=>c.abort());state.selected=null;state.rows=[];clear($('caseList'));clear($('caseSummary'));clear($('contacts'));clear($('caseFacts'));clear($('reviews'));clear($('materialOutput'));clear($('notes'));clear($('todoList'));clear($('events'));$('noteContent').value='';});
  EchoSession.requireSession().then(me=>{if(!me)return;state.me=me;EchoUI.shell(me,'cases');const q=new URLSearchParams(location.search);switchTab(q.get('case_id')?'cases':q.get('tab')||'cases',true);}).catch(EchoUI.bootError);
})();
