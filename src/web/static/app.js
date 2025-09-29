(function(){
  function $(id){ return document.getElementById(id); }
  var logEl = $('log');
  var msgEl = $('msg');
  var btnEl = $('sendBtn');
  var nameEl = $('name');
  var resetEl = $('resetBtn');
  var bot = 'Bot';
  var defaultUser = 'You';
  var statusEl = $('status');
  var secNoteEl = $('security-note');
  var tokenHelpEl = $('token-help');
  var tokenRowEl = $('token-row');
  var tokenEl = $('token');
  var tokenRequired = false;
  var LS_KEY = 'webchat.bearer_token';
  function setStatus(t){ if(statusEl){ statusEl.textContent = t || ''; } }
  function scroll(){ if (logEl) { logEl.scrollTop = logEl.scrollHeight; } }
  function escapeHtml(s){ return s.replace(/[&<>"']/g, function(c){ return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[c]); }); }
  function isSafeUrl(u){ return /^https?:\/\//i.test(u); }
  function mdInline(s){
    var out = escapeHtml(s);
    // strong **text** or __text__
    out = out.replace(/(\*\*|__)(.*?)\1/g, '<strong>$2</strong>');
    // emphasis *text* or _text_
    out = out.replace(/(\*|_)([^\*_][\s\S]*?)\1/g, '<em>$2</em>');
    // inline code `code`
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    // links [text](url)
    out = out.replace(/\[([^\]]+)\]\(([^\)]+)\)/g, function(_, text, url){
      var t = escapeHtml(text || '');
      var u = (url || '').trim();
      if (!isSafeUrl(u)) return t;
      return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>';
    });
    // images ![alt](url)
    out = out.replace(/!\[([^\]]*)\]\(([^\)]+)\)/g, function(_, alt, url){
      var a = escapeHtml(alt || '');
      var u = (url || '').trim();
      if (!isSafeUrl(u)) return '<span>[image blocked: unsafe url]</span>';
      return '<img alt="' + a + '" src="' + u + '">';
    });
    return out;
  }
  function mdToHtml(markdown){
    // Split into paragraphs and code blocks (``` fenced)
    var text = markdown || '';
    var parts = text.split(/```/);
    var html = '';
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        // code block
        html += '<pre><code>' + escapeHtml(parts[i]) + '</code></pre>';
      } else {
        // regular text: handle headings, lists, paragraphs
        var lines = parts[i].split(/\n/);
        var inList = false;
        for (var j = 0; j < lines.length; j++) {
          var line = lines[j];
          if (/^\s*#\s+/.test(line)) { html += '<h1>' + mdInline(line.replace(/^\s*#\s+/, '')) + '</h1>'; continue; }
          if (/^\s*##\s+/.test(line)) { html += '<h2>' + mdInline(line.replace(/^\s*##\s+/, '')) + '</h2>'; continue; }
          if (/^\s*###\s+/.test(line)) { html += '<h3>' + mdInline(line.replace(/^\s*###\s+/, '')) + '</h3>'; continue; }
          if (/^\s*[-*+]\s+/.test(line)) {
            if (!inList) { html += '<ul>'; inList = true; }
            html += '<li>' + mdInline(line.replace(/^\s*[-*+]\s+/, '')) + '</li>';
            continue;
          } else if (inList) {
            html += '</ul>'; inList = false;
          }
          if (line.trim() === '') { /* collapse blank lines */ }
          else { html += '<p>' + mdInline(line) + '</p>'; }
        }
        if (inList) html += '</ul>';
      }
    }
    return html;
  }
  function append(text){ if (!logEl) return; logEl.insertAdjacentHTML('beforeend', mdToHtml(text)); scroll(); }
  function getUser(){ var v = (nameEl && nameEl.value) ? nameEl.value.trim() : ''; return v || defaultUser; }
  function send(){
    var content = (msgEl && msgEl.value ? msgEl.value : '').trim();
    if (!content) return;
    var user = getUser();
    setStatus('Message sent, awaiting response.');
    var headers = { 'Content-Type': 'application/json' };
    if (tokenRequired && tokenEl && tokenEl.value.trim()) headers['Authorization'] = 'Bearer ' + tokenEl.value.trim();
    fetch('/chat', { method: 'POST', headers: headers, body: JSON.stringify({ content: content, user_name: user, user_id: user.toLowerCase() }) })
      .then(function(res){ if (!res.ok) { append('Error ' + res.status + ' ' + res.statusText + '\n\n'); return res.text().then(function(t){ throw new Error(t); }); } return res.json(); })
  .then(function(json){ if (!json) return; append('\n' + user + ': ' + content + '\n\n' + bot + ': ' + (json.reply ? json.reply : '(no reply)') + '\n'); if (msgEl) msgEl.value = ''; setStatus('Ready for new Message.'); })
      .catch(function(e){ append('Error: ' + e + '\n'); setStatus('Ready for new Message.'); });
  }
  function bind(){
  if (btnEl) btnEl.addEventListener('click', send);
    if (msgEl) msgEl.addEventListener('keydown', function(e){ if (e.key === 'Enter') { e.preventDefault(); send(); }});
    if (nameEl) nameEl.addEventListener('keydown', function(e){ if (e.key === 'Enter') { e.preventDefault(); if (msgEl) msgEl.focus(); }});
    if (tokenEl) tokenEl.addEventListener('change', function(){
      var v = (tokenEl.value || '').trim();
      try {
        if (v) localStorage.setItem(LS_KEY, v); else localStorage.removeItem(LS_KEY);
      } catch(_) {}
    });
    if (resetEl) resetEl.addEventListener('click', function(){
      if (!confirm('Start a brand new chat? This clears the current session.')) return;
      setStatus('Resetting…');
      var headers = {};
      if (tokenRequired && tokenEl && tokenEl.value.trim()) headers['Authorization'] = 'Bearer ' + tokenEl.value.trim();
      fetch('/reset', { method: 'POST', headers: headers })
        .then(function(res){ if (!res.ok) { return res.text().then(function(t){ throw new Error(t); }); } return res.json(); })
        .then(function(){ if (logEl) logEl.textContent = ''; setStatus('Ready for new Message.'); })
        .catch(function(e){ append('Error: ' + e + '\n'); setStatus('Ready for new Message.'); });
    });
  }
  function init(){
    if (!logEl || !msgEl) { console.log('[ui-error] elements missing'); return; }
  append('[ui-ready]\n');
  setStatus('Ready for new Message.');
    bind();
    fetch('/web-config').then(function(r){ return r.json(); }).then(function(j){
      if (j && j.bot_name) bot = j.bot_name;
      if (j && j.default_user_name) { defaultUser = j.default_user_name; if (nameEl && !nameEl.value) nameEl.value = defaultUser; }
      if (j && j.token_required && secNoteEl) {
        secNoteEl.style.display = '';
        secNoteEl.textContent = 'Note: This server requires an API bearer token. The built-in web UI works only when no token is set.';
        if (tokenHelpEl) {
          tokenHelpEl.style.display = '';
          tokenHelpEl.textContent = 'Tip: If you want, I can add a small "API token" input to this page so it includes Authorization: Bearer <token> on requests.';
        }
        tokenRequired = true;
        if (tokenRowEl) tokenRowEl.style.display = '';
        try {
          var saved = localStorage.getItem(LS_KEY);
          if (saved && tokenEl && !tokenEl.value) tokenEl.value = saved;
        } catch(_) {}
      }
    }).catch(function(){});
    console.log('[ui-bound]');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.__sendMsg = send;
})();
