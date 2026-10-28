(() => {
  'use strict';
  const {$,message,error} = EchoUI;
  const params=new URLSearchParams(location.search), next=EchoSession.safeNext(params.get('next'));
  const notice=EchoSession.takeNotice();
  if(notice)message('loginError',notice);
  if(next){$('loginContext').hidden=false;$('loginContext').textContent='登录后继续所选场景；系统会核对账号的操作权限。';}
  function pick(account){$('username').value=account.slice(0,100);document.querySelectorAll('[data-account]').forEach(b=>b.classList.toggle('active',b.dataset.account===account));$('password').focus();}
  const account=params.get('account');if(account && /^demo_[a-z_]{1,70}$/.test(account))pick(account);
  document.querySelectorAll('[data-account]').forEach(b=>b.onclick=()=>pick(b.dataset.account));
  $('loginForm').onsubmit=async event=>{
    event.preventDefault();const button=$('loginButton');button.disabled=true;button.textContent='正在确认身份…';message('loginError','');
    try{const me=await EchoSession.login($('username').value.trim(),$('password').value);$('password').value='';location.replace(EchoSession.destination(me.role,next));}
    catch(e){error('loginError',e);}
    finally{button.disabled=false;button.textContent='登录并进入 →';}
  };
  EchoSession.verify().then(me=>{if(me)location.replace(EchoSession.destination(me.role,next));}).catch(e=>error('loginError',e));
})();
