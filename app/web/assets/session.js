/* One login for the application. Roles always come from /users/me. */
(() => {
  'use strict';
  const KEY = 'echo.session.v1', NOTICE = 'echo.session.notice', LOGOUT = 'echo.logout.v1';
  const roles = {user:'客户', staff:'售后专员', admin:'主管', analyst:'分析员'};
  const pages = {user:['/chat'], staff:['/workbench'], admin:['/review','/workbench'], analyst:['/workbench']};
  let token = '', identity = null, generation = 0;
  const pending = new Set();
  try { token = sessionStorage.getItem(KEY) || ''; } catch { /* Memory-only sessions still work. */ }
  const home = role => role === 'user' ? '/chat' : role === 'admin' ? '/review' : '/workbench';
  function safeNext(value) {
    if (!value || !value.startsWith('/') || value.startsWith('//')) return null;
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || !['/chat','/workbench','/review'].includes(url.pathname)) return null;
      const clean = new URL(url.pathname, location.origin);
      for (const key of ['order_id','case_id','review_id','tab','scene']) {
        const value = url.searchParams.get(key);
        if (value && value.length <= 120) clean.searchParams.set(key, value);
      }
      return clean.pathname + clean.search;
    } catch { return null; }
  }
  function destination(role, requested) {
    const safe = safeNext(requested);
    return safe && pages[role]?.includes(new URL(safe, location.origin).pathname) ? safe : home(role);
  }
  function rememberNotice(message) { try { sessionStorage.setItem(NOTICE,message); } catch {} }
  function takeNotice() {
    try { const value = sessionStorage.getItem(NOTICE) || ''; sessionStorage.removeItem(NOTICE); return value; } catch { return ''; }
  }
  function clear() {
    generation++; token = ''; identity = null;
    for (const controller of pending) controller.abort();
    pending.clear();
    try { sessionStorage.removeItem(KEY); } catch {}
    document.querySelectorAll('[data-private]').forEach(node => node.hidden = true);
    document.dispatchEvent(new Event('echo:logout'));
  }
  function goLogin(message = '', next = location.pathname + location.search) {
    clear(); if (message) rememberNotice(message);
    const safe = safeNext(next);
    location.replace('/login' + (safe ? '?next=' + encodeURIComponent(safe) : ''));
  }
  function logout() {
    clear();
    try { localStorage.setItem(LOGOUT, Date.now() + ':' + Math.random()); } catch {}
    rememberNotice('已退出登录。选择其他账号可以切换身份。');
    location.replace('/login');
  }
  async function request(path, options = {}, credential = token) {
    if (!path.startsWith('/api/v1/')) throw new Error('请求路径不合法');
    const controller = new AbortController(), at = generation;
    const signal = options.signal;
    const cancel = () => controller.abort();
    if (signal?.aborted) controller.abort(); else signal?.addEventListener('abort',cancel,{once:true});
    pending.add(controller);
    const headers = new Headers(options.headers || {});
    if (credential) headers.set('Authorization','Bearer ' + credential);
    let body = options.body;
    if (body && Object.getPrototypeOf(body) === Object.prototype) {
      headers.set('Content-Type','application/json'); body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path,{...options,body,headers,signal:controller.signal,cache:'no-store'});
      if (at !== generation) throw new DOMException('会话已变化','AbortError');
      if (response.status === 401 && credential === token && token) {
        goLogin('登录已过期或账号状态已变化，请重新登录。');
        throw new DOMException('会话已失效','AbortError');
      }
      return response;
    } finally { pending.delete(controller); signal?.removeEventListener('abort',cancel); }
  }
  async function json(path, options = {}, credential = token) {
    const at = generation;
    const response = await request(path,options,credential);
    let body; try { body = await response.json(); } catch { body = {}; }
    if (at !== generation) throw new DOMException('会话已变化','AbortError');
    if (!response.ok) {
      const detail = body.error?.message || (typeof body.detail === 'string' ? body.detail : null);
      const error = new Error(detail || (response.status === 403 ? '当前账号没有此操作权限。' : '请求未完成，请稍后重试。'));
      error.status = response.status; throw error;
    }
    return body;
  }
  async function verify() {
    if (!token) return null;
    const currentToken = token, at = generation;
    const me = await json('/api/v1/users/me');
    if (at !== generation || token !== currentToken) throw new DOMException('会话已变化','AbortError');
    if (!pages[me.role]) throw new Error('当前账号角色暂不支持此演示。');
    identity = me;
    return me;
  }
  async function login(username,password) {
    const at = generation;
    const result = await json('/api/v1/auth/login',{method:'POST',body:new URLSearchParams({username,password})},'');
    const me = await json('/api/v1/users/me',{},result.access_token);
    if (at !== generation) throw new DOMException('会话已变化','AbortError');
    if (!pages[me.role]) throw new Error('当前账号角色暂不支持此演示。');
    token = result.access_token; identity = me;
    try { sessionStorage.setItem(KEY,token); } catch { throw new Error('浏览器无法保存登录状态，请允许此站点使用会话存储。'); }
    return me;
  }
  async function requireSession() {
    if (!token) { goLogin(); return null; }
    const me = await verify();
    if (!me) return null;
    if (!pages[me.role].includes(location.pathname)) {
      rememberNotice('已进入与你的账号角色对应的工作区。');
      location.replace(home(me.role)); return null;
    }
    return me;
  }
  window.addEventListener('storage',event => { if (event.key === LOGOUT && event.newValue) goLogin('其他窗口已退出登录，请重新登录。'); });
  window.addEventListener('pageshow',event => { if (event.persisted) location.reload(); });
  window.EchoSession = {json,request,verify,login,requireSession,logout,home,safeNext,destination,roles,takeNotice,get identity(){return identity;}};
})();
