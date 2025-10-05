(function(){
  function $(id){ return document.getElementById(id); }
  var logEl = $('log');
  var msgEl = $('msg');
  var btnEl = $('sendBtn');
  var nameEl = $('name');
  var themeToggleEl = $('themeToggle');
  var resetEl = $('resetBtn');
  var jumpEl = $('jumpBtn');
  var bot = 'Bot';
  var defaultUser = 'You';
  var statusEl = $('status');
  var secNoteEl = $('security-note');
  var tokenHelpEl = $('token-help');
  var tokenRowEl = $('token-row');
  var tokenEl = $('token');
  var tokenRequired = false;
  var LS_KEY = 'webchat.bearer_token';
  var LS_NAME = 'webchat.name';
  var LS_THEME = 'webchat.theme';
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
    // images ![alt](url)
    out = out.replace(/!\[([^\]]*)\]\(([^\)]+)\)/g, function(_, alt, url){
      var a = escapeHtml(alt || '');
      var u = (url || '').trim();
      if (!isSafeUrl(u)) return '<span>[image blocked: unsafe url]</span>';
      return '<img alt="' + a + '" src="' + u + '">';
    });
    // links [text](url)
    out = out.replace(/\[([^\]]+)\]\(([^\)]+)\)/g, function(_, text, url){
      var t = escapeHtml(text || '');
      var u = (url || '').trim();
      if (!isSafeUrl(u)) return t;
      return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>';
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
  function appendRawHtml(html){ if (!logEl) return; logEl.insertAdjacentHTML('beforeend', html); scroll(); }
  function bubble(author, contentHtml, who){
    var cls = who === 'user' ? 'user' : 'bot';
    var safeAuthor = escapeHtml(author || '');
    return '<div class="bubble ' + cls + '">' +
             '<div class="author">' + safeAuthor + '</div>' +
             '<div class="content">' + contentHtml + '</div>' +
           '</div>';
  }
  function append(text){
    // Back-compat: render a simple block as a bot bubble
    appendRawHtml(bubble(bot, mdToHtml(text), 'bot'));
  }
  function getUser(){ var v = (nameEl && nameEl.value) ? nameEl.value.trim() : ''; return v || defaultUser; }
  function send(){
    var content = (msgEl && msgEl.value ? msgEl.value : '').trim();
    if (!content) return;
    var user = getUser();
    setStatus('Message sent, awaiting response.');
    var headers = { 'Content-Type': 'application/json' };
    if (tokenRequired && tokenEl && tokenEl.value.trim()) headers['Authorization'] = 'Bearer ' + tokenEl.value.trim();
    // Render the user bubble immediately
    appendRawHtml(bubble(user, mdToHtml(content), 'user'));
    fetch('/chat', { method: 'POST', headers: headers, body: JSON.stringify({ content: content, user_name: user, user_id: user.toLowerCase() }) })
      .then(function(res){ if (!res.ok) { appendRawHtml(bubble('Error', mdToHtml('Error ' + res.status + ' ' + res.statusText), 'bot')); return res.text().then(function(t){ throw new Error(t); }); } return res.json(); })
      .then(function(json){ if (!json) return; var reply = (json.reply ? json.reply : '(no reply)'); appendRawHtml(bubble(bot, mdToHtml(reply), 'bot')); if (msgEl) msgEl.value = ''; setStatus('Ready for new Message.'); })
      .catch(function(e){ appendRawHtml(bubble('Error', mdToHtml(String(e)), 'bot')); setStatus('Ready for new Message.'); });
  }
  function autoGrow(){
    if (!msgEl) return;
    msgEl.style.height = 'auto';
    msgEl.style.height = Math.min(msgEl.scrollHeight, 160) + 'px';
  }
  function bind(){
  if (btnEl) btnEl.addEventListener('click', send);
    if (msgEl) {
      msgEl.addEventListener('keydown', function(e){
        if (e.key === 'Enter') {
          if (e.shiftKey) { return; } // allow newline
          e.preventDefault();
          send();
          return;
        }
      });
      msgEl.addEventListener('input', autoGrow);
      setTimeout(autoGrow, 0);
    }
    if (nameEl) {
      nameEl.addEventListener('keydown', function(e){ if (e.key === 'Enter') { e.preventDefault(); if (msgEl) msgEl.focus(); }});
      try { var savedName = localStorage.getItem(LS_NAME); if (savedName && !nameEl.value) nameEl.value = savedName; } catch(_) {}
      nameEl.addEventListener('change', function(){ try { if (nameEl.value) localStorage.setItem(LS_NAME, nameEl.value); else localStorage.removeItem(LS_NAME);} catch(_) {} });
    }
    if (themeToggleEl) {
      themeToggleEl.addEventListener('click', function(){
        var dark = document.body.classList.toggle('dark');
        try { localStorage.setItem(LS_THEME, dark ? 'dark' : 'light'); } catch(_) {}
      });
      try { var pref = localStorage.getItem(LS_THEME); if (pref === 'dark') document.body.classList.add('dark'); } catch(_) {}
    }
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
    if (jumpEl && logEl) {
      jumpEl.addEventListener('click', function(){ scroll(); });
      logEl.addEventListener('scroll', function(){
        var nearBottom = (logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight) < 40;
        jumpEl.style.display = nearBottom ? 'none' : '';
      });
    }
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
          tokenHelpEl.textContent = '';
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
