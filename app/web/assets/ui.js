(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {user:'客户',staff:'售后专员',admin:'主管',analyst:'分析员',open:'待接手',in_progress:'处理中',resolved:'已解决',closed:'已关闭',pending:'待审核',approved:'已批准',rejected:'已驳回',done:'已完成',other:'其他／待分类',refund:'退款',return:'退货',complaint:'投诉',consultation:'咨询',product_fault:'商品故障',delivery_delay:'履约延迟',changed_mind:'改变意愿',unknown:'未确认',intake:'申请接单',created:'建立案件',contact_linked:'关联补充联系',case_updated:'更新处理信息',note_added:'新增备注',work_item_updated:'更新待办',succeeded:'模拟执行完成',processing:'批准后处理中',not_started:'尚未开始执行',paid:'已付款',shipped:'已发货',refunded:'已模拟退款',out_of_stock:'缺货',escalated:'待人工处理',submitted:'提交审核',recovery_attempt:'尝试恢复',completed:'已完成'};
  const label = value => labels[value] || value || '—';
  function el(tag,text,cls) { const node=document.createElement(tag); if(text!=null)node.textContent=String(text);if(cls)node.className=cls;return node; }
  const clear = node => node.replaceChildren();
  function message(id,text,bad=false) { const node=$(id);if(!node)return;node.textContent=text;node.classList.toggle('error',bad); }
  function error(id,e) { if(e.name!=='AbortError')message(id,e.message || '连接失败，请稍后重试。',true); }
  function date(value) { if(!value)return '—';return new Date(/[Zz]|[+-]\d\d:\d\d$/.test(value)?value:value+'Z').toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
  function badge(value,text) { return el('span',text || label(value),'badge badge-'+value); }
  function link(text,href,cls='button') { const a=el('a',text,cls);a.href=href;return a; }
  function facts(node,rows) { clear(node);const dl=el('dl',null,'facts');for(const [name,value] of rows)dl.append(el('dt',name),el('dd',value ?? '—'));node.append(dl); }
  function shell(me,page) {
    $('boot')?.remove();
    document.querySelectorAll('[data-private]').forEach(node=>node.hidden=false);
    if($('identity'))$('identity').textContent=me.username;
    if($('roleLabel'))$('roleLabel').textContent=label(me.role);
    if($('tenantLabel'))$('tenantLabel').textContent=me.tenant_id==='demo_echo'?'回声耳机店':me.tenant_id;
    const nav=$('sideNav');
    if(nav) {
      clear(nav);
      const items=me.role==='user'?[['客户对话','/chat','chat']]:me.role==='analyst'?[['台账统计','/workbench?tab=analytics','analytics']]:[
        ['案件处理','/workbench?tab=cases','cases'],['待办列表','/workbench?tab=todos','todos'],
        ...(me.role==='admin'?[['退款审核','/review','review'],['台账统计','/workbench?tab=analytics','analytics']]:[])];
      for(const [title,url,key] of items){const a=link(title,url,'nav-link');a.dataset.nav=key;if(key===page)a.setAttribute('aria-current','page');nav.append(a);}
      nav.append(link('演示场景','/demo','nav-link nav-secondary'));
    }
    $('logout')?.addEventListener('click',EchoSession.logout);
  }
  function activeNav(page) {document.querySelectorAll('[data-nav]').forEach(a=>{if(a.dataset.nav===page)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});}
  function bootError(e) {
    if(e.name==='AbortError')return;
    const node=$('boot');if(!node)return;clear(node);node.append(el('h2','暂时无法进入工作区'),el('p',e.message || '请检查服务连接后重试。','muted'));
    const retry=el('button','重新连接','primary');retry.onclick=()=>location.reload();node.append(retry,link('返回登录','/login','button'));
  }
  window.EchoUI={$,label,el,clear,message,error,date,badge,link,facts,shell,activeNav,bootError};
})();
