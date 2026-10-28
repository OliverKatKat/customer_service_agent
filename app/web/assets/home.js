(() => {
  'use strict';
  EchoSession.verify().then(me=>{
    if(!me)return;
    document.getElementById('publicIdentity').textContent=me.username+' · '+EchoSession.roles[me.role];
    const enter=document.getElementById('enterWorkspace');enter.href=EchoSession.home(me.role);enter.textContent='继续工作 ↗';
  }).catch(()=>{/* Public scene descriptions stay available when the API is offline. */});
})();
