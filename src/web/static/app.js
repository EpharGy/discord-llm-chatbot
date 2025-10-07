(function(){
  function $(id){ return document.getElementById(id); }
  var logEl = $('log');
  var msgEl = $('msg');
  var btnEl = $('sendBtn');
  var nameEl = $('name');
  var themeToggleEl = $('themeToggle');
  var providerEl = $('provider');
  var resetEl = $('resetBtn');
  var deleteRoomBtn = $('deleteRoomBtn');
  var jumpEl = $('jumpBtn');
  var bot = 'Bot';
  var defaultUser = 'You';
  var statusEl = $('status');
  var secNoteEl = null;
  var tokenHelpEl = null;
  var tokenRowEl = $('token-row');
  var tokenEl = $('token');
  var tokenRequired = false;
  var headerTitleEl = $('headerTitle');
  var controlsToggleEl = $('controlsToggle');
  var controlsWrapperEl = $('controlsWrapper');
  var roomSelectEl = $('roomSelect');
  var createRoomBtn = $('createRoomBtn');
  var LS_KEY = 'webchat.bearer_token';
  var LS_NAME = 'webchat.name';
  var LS_THEME = 'webchat.theme';
  var LS_PROVIDER = 'webchat.provider';
  var LS_ROOM_ID = 'webchat.room_id';
  var LS_ROOM_PASS = 'webchat.room_passes';
  var providerClasses = ['provider-openrouter', 'provider-openai'];
  var currentProviderTheme = null;
  var roomsById = {};
  var passCache = {};
  var currentRoom = null;
  var currentRoomName = '';
  var currentPasscode = null;
  var savedRoomId = null;
  try { savedRoomId = localStorage.getItem(LS_ROOM_ID) || null; } catch(_) { savedRoomId = null; }
  function setStatus(t, cls){ if(statusEl){ statusEl.textContent = t || ''; statusEl.classList.remove('ok','busy'); if (cls) statusEl.classList.add(cls); } }
  function scroll(){ if (logEl) { logEl.scrollTop = logEl.scrollHeight; } }
  var mobileQuery = window.matchMedia ? window.matchMedia('(max-width: 768px)') : { matches: false, addListener: function(){}, addEventListener: function(){} };
  var controlsCollapsed = mobileQuery.matches;
  function getProviderDisplayName(val){
    if (!val) return 'Auto';
    var normalized = val.toLowerCase();
    if (normalized === 'openrouter') return 'OpenRouter';
    if (normalized === 'openai') return 'OpenAI';
    return val;
  }
  function updateControlsToggleLabel(){
    if (!controlsToggleEl) return;
    controlsToggleEl.textContent = controlsCollapsed ? 'Show Controls' : 'Hide Controls';
  }
  function applyControlsCollapsedState(){
    if (!controlsWrapperEl) return;
    controlsWrapperEl.classList.toggle('collapsed', controlsCollapsed);
    if (controlsToggleEl) controlsToggleEl.setAttribute('aria-expanded', controlsCollapsed ? 'false' : 'true');
    updateControlsToggleLabel();
  }
  function handleMediaChange(e){
    controlsCollapsed = !!(e && e.matches);
    applyControlsCollapsedState();
  }
  function buildHeaders(includeJson){
    var headers = {};
    if (includeJson) headers['Content-Type'] = 'application/json';
    if (tokenEl && tokenEl.value && tokenEl.value.trim()) headers['Authorization'] = 'Bearer ' + tokenEl.value.trim();
    return headers;
  }
  function requestJson(url, options){
    options = options || {};
    return fetch(url, options).then(function(res){
      if (res.ok) {
        if (res.status === 204) return {};
        return res.json().catch(function(){ return {}; });
      }
      return res.text().then(function(text){
        var msg = '';
        if (text) {
          try {
            var data = JSON.parse(text);
            msg = data && (data.detail || data.error) || '';
          } catch(_) {
            msg = text;
          }
        }
        if (!msg) msg = 'Error ' + res.status;
        var err = new Error(msg);
        err.status = res.status;
        throw err;
      });
    });
  }
  function loadPassCache(){
    try {
      var raw = localStorage.getItem(LS_ROOM_PASS);
      if (raw) passCache = JSON.parse(raw) || {};
      else passCache = {};
    } catch(_) {
      passCache = {};
    }
  }
  function persistPassCache(){
    try { localStorage.setItem(LS_ROOM_PASS, JSON.stringify(passCache)); } catch(_) {}
  }
  function getStoredPass(roomId){
    if (!roomId) return '';
    return (passCache && typeof passCache === 'object') ? (passCache[roomId] || '') : '';
  }
  function storePass(roomId, passcode){
    if (!roomId) return;
    if (passcode) passCache[roomId] = passcode;
    else if (passCache && passCache.hasOwnProperty(roomId)) delete passCache[roomId];
    persistPassCache();
  }
  function updateHeader(){
    if (!headerTitleEl) return;
    var parts = ['Web Chat'];
    if (bot) parts.push(bot);
    var roomLabel = currentRoomName ? currentRoomName : 'None';
    parts.push('Room: ' + roomLabel);
    var providerVal = '';
    try { if (providerEl && providerEl.value) providerVal = providerEl.value.trim(); } catch(_) {}
    if (!providerVal && currentProviderTheme) providerVal = currentProviderTheme;
    parts.push('Provider: ' + getProviderDisplayName(providerVal));
    headerTitleEl.textContent = parts.join(' — ');
  }
  function applyProviderTheme(provider){
    var body = document.body;
    if (!body) return;
    providerClasses.forEach(function(cls){ body.classList.remove(cls); });
    var normalized = (provider || '').toLowerCase();
    if (normalized === 'openrouter' || normalized === 'openai') {
      body.classList.add('provider-' + normalized);
      currentProviderTheme = normalized;
    } else {
      currentProviderTheme = null;
    }
  }
  function ensureProviderOption(value){
    if (!providerEl || !value) return;
    for (var i = 0; i < providerEl.options.length; i++) {
      if (providerEl.options[i].value === value) return;
    }
    var opt = document.createElement('option');
    opt.value = value;
    opt.textContent = value;
    providerEl.appendChild(opt);
  }
  function normalizeRoomMeta(meta){
    if (!meta) return null;
    var rid = meta.room_id || (meta.room_id === '' ? '' : meta['room_id']);
    if (!rid) return null;
    var name = meta.name || meta['name'] || rid;
    var lastActive = meta.last_active || meta['last_active'] || '';
    var locked = meta.locked;
    if (typeof locked === 'undefined') locked = meta['locked'];
    var provider = meta.provider || meta['provider'] || null;
    if (provider) provider = provider.toLowerCase();
    return {
      room_id: rid,
      name: name,
      last_active: lastActive,
      locked: Boolean(locked),
      provider: provider
    };
  }
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
  function clearEmptyState(){
    if (!logEl || !logEl.firstChild) return;
    if (logEl.firstChild.classList && logEl.firstChild.classList.contains('empty-state')) {
      logEl.innerHTML = '';
      if (logEl.dataset) delete logEl.dataset.empty;
    }
  }
  function showEmptyState(message){
    if (!logEl) return;
    var text = message || 'Create or join a room to start chatting.';
    logEl.innerHTML = '<div class="empty-state"><strong>' + escapeHtml(text) + '</strong></div>';
    if (logEl.dataset) logEl.dataset.empty = '1';
  }
  function appendRawHtml(html){
    if (!logEl) return;
    clearEmptyState();
    logEl.insertAdjacentHTML('beforeend', html);
    scroll();
  }
  function bubble(author, contentHtml, who){
    var cls = who === 'user' ? 'user' : 'bot';
    var safeAuthor = escapeHtml(author || '');
    return '<div class="bubble ' + cls + '">' +
             '<div class="author">' + safeAuthor + '</div>' +
             '<div class="content">' + contentHtml + '</div>' +
           '</div>';
  }
  function renderTranscript(messages){
    if (!logEl) return;
    if (!Array.isArray(messages) || messages.length === 0) {
      showEmptyState('No messages yet. Say hello!');
      return;
    }
    var html = '';
    for (var i = 0; i < messages.length; i++) {
      var msg = messages[i] || {};
      var role = msg.role === 'user' ? 'user' : 'bot';
      var author = msg.author_name || (role === 'user' ? getUser() : bot);
      var content = mdToHtml(msg.content || '');
      html += bubble(author, content, role);
    }
    logEl.innerHTML = html;
    if (logEl.dataset) delete logEl.dataset.empty;
    scroll();
  }
  function setCurrentRoom(roomId, roomName, passcode, provider){
    currentRoom = roomId || null;
    currentRoomName = roomName || '';
    if (roomId) {
      try { localStorage.setItem(LS_ROOM_ID, roomId); } catch(_) {}
    } else {
      try { localStorage.removeItem(LS_ROOM_ID); } catch(_) {}
    }
    savedRoomId = roomId || null;
    if (typeof passcode === 'string') {
      currentPasscode = passcode || null;
      if (roomId) storePass(roomId, passcode);
    } else {
      currentPasscode = getStoredPass(roomId) || null;
    }
    if (roomSelectEl) {
      roomSelectEl.value = roomId || '';
    }
    if (roomId && roomsById[roomId]) {
      roomsById[roomId].provider = provider || roomsById[roomId].provider || null;
    }
    var providerValue = provider || (roomId && roomsById[roomId] ? roomsById[roomId].provider : null);
    if (providerValue) {
      ensureProviderOption(providerValue);
      if (providerEl) providerEl.value = providerValue;
      applyProviderTheme(providerValue);
      try { localStorage.setItem(LS_PROVIDER, providerValue); } catch(_) {}
    } else if (providerEl && providerEl.value) {
      applyProviderTheme(providerEl.value);
    }
    updateHeader();
  }
  function populateRoomSelect(rooms){
    roomsById = {};
    if (!roomSelectEl) return;
    roomSelectEl.innerHTML = '';
    var fallbackOption = document.createElement('option');
    fallbackOption.value = '';
    fallbackOption.textContent = rooms && rooms.length ? 'Select a room' : 'No rooms yet';
    roomSelectEl.appendChild(fallbackOption);
    if (Array.isArray(rooms)) {
      rooms.forEach(function(metaRaw){
        var meta = normalizeRoomMeta(metaRaw);
        if (!meta) return;
        roomsById[meta.room_id] = meta;
        var opt = document.createElement('option');
        opt.value = meta.room_id;
        opt.textContent = meta.name + (meta.locked ? ' 🔒' : '');
        roomSelectEl.appendChild(opt);
      });
    }
  }
  function refreshRooms(selectId){
    setStatus('Loading rooms…');
    return requestJson('/rooms', { method: 'GET', headers: buildHeaders(false) })
      .then(function(data){
        var rooms = (data && data.rooms) || [];
        populateRoomSelect(rooms);
        setStatus('Ready for new Message.', 'ok');
        var desired = selectId || currentRoom || savedRoomId;
        if (desired && roomsById[desired]) {
          if (roomSelectEl) roomSelectEl.value = desired;
          var desiredProvider = roomsById[desired].provider;
          if (desiredProvider) {
            ensureProviderOption(desiredProvider);
            if (providerEl) providerEl.value = desiredProvider;
            applyProviderTheme(desiredProvider);
          }
        }
        if (!rooms.length) {
          showEmptyState('No rooms found. Create one to get started.');
        }
        updateHeader();
        return rooms;
      })
      .catch(function(err){
        setStatus('Ready for new Message.', 'ok');
        showEmptyState('Unable to load rooms: ' + (err.message || String(err)));
        return [];
      });
  }
  function revertRoomSelection(){
    if (!roomSelectEl) return;
    if (currentRoom) roomSelectEl.value = currentRoom;
    else roomSelectEl.value = '';
  }
  function promptForPasscode(meta, message){
    if (!meta) return null;
    var existing = getStoredPass(meta.room_id) || '';
    var promptText = message || ('Enter passcode for "' + (meta.name || meta.room_id) + '":');
    var result = window.prompt(promptText, existing);
    if (result === null) return null;
    return result.trim();
  }
  function attemptRoomSwitch(roomId){
    var meta = roomsById[roomId];
    if (!meta) {
      showEmptyState('Room not found.');
      revertRoomSelection();
      return;
    }
    var pass = getStoredPass(roomId) || '';
    if (meta.locked && !pass) {
      var entered = promptForPasscode(meta);
      if (entered === null) {
        revertRoomSelection();
        return;
      }
      pass = entered;
    }
    joinRoomWithRetries(roomId, pass, meta, { allowProviderOverride: false });
  }
  function joinRoomWithRetries(roomId, passcode, meta, opts){
    joinRoom(roomId, passcode, opts).catch(function(err){
      setStatus('Ready for new Message.', 'ok');
      var status = err && err.status ? err.status : 0;
      if (status === 403 && meta && meta.locked) {
        var retry = promptForPasscode(meta, 'Incorrect passcode for "' + (meta.name || meta.room_id) + '". Try again:');
        if (retry === null) {
          revertRoomSelection();
          return;
        }
        joinRoomWithRetries(roomId, retry, meta, opts);
        return;
      }
      revertRoomSelection();
      showEmptyState('Unable to join room: ' + (err && err.message ? err.message : String(err)));
    });
  }
  function deleteRoomFlow(){
    if (!roomSelectEl) return;
    var rid = roomSelectEl.value || '';
    if (!rid) {
      appendRawHtml(bubble('System', mdToHtml('Select a room before deleting.'), 'bot'));
      return;
    }
    var meta = roomsById[rid];
    if (!meta) {
      appendRawHtml(bubble('System', mdToHtml('Room not found.'), 'bot'));
      return;
    }
    var label = meta.name || rid;
    if (!window.confirm('Delete room "' + label + '"? This will remove all stored history.')) return;
    var pass = getStoredPass(rid) || '';
    if (meta.locked && !pass) {
      var entered = promptForPasscode(meta, 'Enter the passcode to delete "' + label + '":');
      if (entered === null) return;
      pass = entered;
    }
    if (meta.locked) {
      if (pass) storePass(rid, pass);
      else storePass(rid, null);
    }
    setStatus('Deleting room…', 'busy');
    fetch('/rooms/' + encodeURIComponent(rid), {
      method: 'DELETE',
      headers: buildHeaders(true),
      body: JSON.stringify({ passcode: pass || null })
    }).then(function(res){
      if (!res.ok) {
        return res.text().then(function(text){
          var message = text;
          try {
            var data = JSON.parse(text);
            message = data && (data.detail || data.error) || message;
          } catch(_) {}
          var error = new Error(message || ('Error ' + res.status));
          error.status = res.status;
          throw error;
        });
      }
      return res.json();
    }).then(function(){
      storePass(rid, null);
      if (currentRoom && currentRoom === rid) {
        setCurrentRoom(null, '', null, null);
        showEmptyState('Room deleted. Select another room to continue.');
      }
      delete roomsById[rid];
      return refreshRooms().then(function(){
        setStatus('Room deleted.', 'ok');
      });
    }).catch(function(err){
      if (err && err.status === 403) storePass(rid, null);
      var msg = err && err.message ? err.message : String(err);
      appendRawHtml(bubble('Error', mdToHtml('Delete failed: ' + msg), 'bot'));
      setStatus('Ready for new Message.', 'ok');
    });
  }
  function joinRoom(roomId, passcode, options){
    if (!roomId) {
      showEmptyState('Select a room from the list or create a new one.');
      return Promise.reject(new Error('No room selected'));
    }
    options = options || {};
    var allowProviderOverride = options.allowProviderOverride !== false;
    var providerValue = '';
    if (allowProviderOverride && providerEl && providerEl.value) providerValue = providerEl.value.trim();
    if (!providerValue && roomsById[roomId] && roomsById[roomId].provider) {
      providerValue = roomsById[roomId].provider;
      if (allowProviderOverride) {
        ensureProviderOption(providerValue);
        if (providerEl) providerEl.value = providerValue;
      }
    }
    if (allowProviderOverride && providerValue) {
      applyProviderTheme(providerValue);
    }
    var payload = { passcode: passcode || '' };
    if (providerValue) payload.provider = providerValue;
    setStatus('Joining room…', 'busy');
    return requestJson('/rooms/' + encodeURIComponent(roomId) + '/join', {
      method: 'POST',
      headers: buildHeaders(true),
      body: JSON.stringify(payload)
    }).then(function(resp){
      setStatus('Ready for new Message.', 'ok');
      var meta = normalizeRoomMeta(resp && resp.room);
      if (!meta || !meta.room_id) throw new Error('Invalid join response');
      roomsById[meta.room_id] = meta;
      populateRoomSelect(Object.values(roomsById));
      if (typeof passcode === 'string' && passcode) storePass(meta.room_id, passcode);
      var storedPass = passcode || getStoredPass(meta.room_id);
      setCurrentRoom(meta.room_id, meta.name, storedPass, meta.provider || providerValue || null);
      renderTranscript(resp.messages || []);
      return resp;
    }).catch(function(err){
      setStatus('Ready for new Message.', 'ok');
      throw err;
    });
  }
  function createRoomFlow(){
    var name = window.prompt('Enter a room name:');
    if (!name || !name.trim()) return;
    var passcode = null;
    while (true) {
      passcode = window.prompt('Set a passcode (required):', '') || '';
      if (!passcode.trim()) {
        if (!window.confirm('Passcode is required. Cancel room creation?')) {
          continue;
        } else {
          return;
        }
      }
      break;
    }
    setStatus('Creating room…', 'busy');
    passcode = passcode.trim();
  requestJson('/rooms', {
      method: 'POST',
      headers: buildHeaders(true),
      body: JSON.stringify({ name: name, passcode: passcode || null })
    }).then(function(metaObj){
      var meta = normalizeRoomMeta(metaObj);
      if (!meta) {
        throw new Error('Room creation failed.');
      }
      roomsById[meta.room_id] = meta;
      populateRoomSelect(Object.values(roomsById));
      storePass(meta.room_id, passcode);
      return joinRoom(meta.room_id, passcode);
    }).then(function(){
      setStatus('Ready for new Message.', 'ok');
    }).catch(function(err){
      setStatus('Ready for new Message.', 'ok');
      showEmptyState('Room operation failed: ' + (err.message || String(err)));
    });
  }
  function append(text){
    // Back-compat: render a simple block as a bot bubble
    appendRawHtml(bubble(bot, mdToHtml(text), 'bot'));
  }
  function getUser(){ var v = (nameEl && nameEl.value) ? nameEl.value.trim() : ''; return v || defaultUser; }
  function send(){
    var content = (msgEl && msgEl.value ? msgEl.value : '').trim();
    if (!content) return;
    if (!currentRoom){
      showEmptyState('Create or join a room before sending messages.');
      return;
    }
    var user = getUser();
    setStatus('Message sent, awaiting response.', 'busy');
    var headers = buildHeaders(true);
    // Render the user bubble immediately
    appendRawHtml(bubble(user, mdToHtml(content), 'user'));
    var provider = null; try { if (providerEl && providerEl.value) provider = providerEl.value; } catch(_) {}
    var passcode = currentPasscode || getStoredPass(currentRoom) || '';
    currentPasscode = passcode || null;
    fetch('/chat', { method: 'POST', headers: headers, body: JSON.stringify({
        content: content,
        user_name: user,
        user_id: user.toLowerCase(),
        provider: provider,
        channel_id: currentRoom,
        passcode: passcode
      }) })
      .then(function(res){ if (!res.ok) { appendRawHtml(bubble('Error', mdToHtml('Error ' + res.status + ' ' + res.statusText), 'bot')); return res.text().then(function(t){ throw new Error(t); }); } return res.json(); })
  .then(function(json){ if (!json) return; var reply = (json.reply ? json.reply : '(no reply)'); appendRawHtml(bubble(bot, mdToHtml(reply), 'bot')); if (msgEl) msgEl.value = ''; setStatus('Ready for new Message.', 'ok'); })
  .catch(function(e){ appendRawHtml(bubble('Error', mdToHtml(String(e)), 'bot')); setStatus('Ready for new Message.', 'ok'); });
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
    if (providerEl) providerEl.addEventListener('change', function(){
      var val = (providerEl.value || '').trim();
      applyProviderTheme(val);
      try {
        if (val) localStorage.setItem(LS_PROVIDER, val);
        else localStorage.removeItem(LS_PROVIDER);
      } catch(_) {}
      if (currentRoom && roomsById[currentRoom]) {
        roomsById[currentRoom].provider = val || null;
      }
      updateHeader();
    });
    if (resetEl) resetEl.addEventListener('click', function(){
      if (!currentRoom){
        showEmptyState('Join a room before resetting history.');
        return;
      }
      if (!confirm('Clear the conversation history for this room?')) return;
      setStatus('Resetting…');
      fetch('/reset', {
        method: 'POST',
        headers: buildHeaders(true),
        body: JSON.stringify({ room_id: currentRoom })
      })
        .then(function(res){ if (!res.ok) { return res.text().then(function(t){ throw new Error(t); }); } return res.json(); })
        .then(function(){ showEmptyState('History cleared. Start a new conversation!'); setStatus('Ready for new Message.'); })
        .catch(function(e){ append('Error: ' + e + '\n'); setStatus('Ready for new Message.'); });
    });
    if (jumpEl && logEl) {
      jumpEl.addEventListener('click', function(){ scroll(); });
      logEl.addEventListener('scroll', function(){
        var nearBottom = (logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight) < 40;
        jumpEl.style.display = nearBottom ? 'none' : '';
      });
    }
    if (roomSelectEl) {
      roomSelectEl.addEventListener('change', function(){
        var rid = roomSelectEl.value || '';
        if (!rid) {
          revertRoomSelection();
          return;
        }
        if (rid === currentRoom) return;
        attemptRoomSwitch(rid);
      });
    }
    if (controlsToggleEl) {
      controlsToggleEl.addEventListener('click', function(){
        controlsCollapsed = !controlsCollapsed;
        applyControlsCollapsedState();
      });
    }
    if (deleteRoomBtn) {
      deleteRoomBtn.addEventListener('click', deleteRoomFlow);
    }
    if (createRoomBtn) {
      createRoomBtn.addEventListener('click', function(){
        createRoomFlow();
      });
    }
  }
  function init(){
    if (!logEl || !msgEl) { console.log('[ui-error] elements missing'); return; }
    setStatus('Ready for new Message.', 'ok');
    loadPassCache();
    controlsCollapsed = mobileQuery.matches;
    applyControlsCollapsedState();
    if (mobileQuery.addEventListener) mobileQuery.addEventListener('change', handleMediaChange);
    else if (mobileQuery.addListener) mobileQuery.addListener(handleMediaChange);
    updateHeader();
  showEmptyState('Create or join a room to start chatting.');
    bind();
    fetch('/web-config').then(function(r){ return r.json(); }).then(function(j){
      if (j && j.bot_name) { bot = j.bot_name; updateHeader(); }
      if (j && j.default_user_name) { defaultUser = j.default_user_name; if (nameEl && !nameEl.value) nameEl.value = defaultUser; }
      // Provider select
      try {
        var options = (j && Array.isArray(j.providers)) ? j.providers : [];
        var savedProvider = null; try { savedProvider = localStorage.getItem(LS_PROVIDER); } catch(_) {}
        var defaultProvider = savedProvider || (j && j.default_provider) || 'openrouter';
        if (providerEl) {
          providerEl.innerHTML = '';
          options.forEach(function(p){
            var opt = document.createElement('option');
            opt.value = p; opt.textContent = p;
            if (p === defaultProvider) opt.selected = true;
            providerEl.appendChild(opt);
          });
          if (options.indexOf(defaultProvider) === -1) {
            var opt = document.createElement('option');
            opt.value = defaultProvider; opt.textContent = defaultProvider;
            opt.selected = true;
            providerEl.appendChild(opt);
          }
        }
        var activeProvider = (providerEl && providerEl.value) ? providerEl.value : defaultProvider;
    applyProviderTheme(activeProvider);
    updateHeader();
      } catch(_) {}
      try { var saved = localStorage.getItem(LS_KEY); if (saved && tokenEl && !tokenEl.value) tokenEl.value = saved; } catch(_) {}
    }).catch(function(){});
    refreshRooms().then(function(rooms){
      var target = null;
      var pass = '';
      if (savedRoomId && roomsById[savedRoomId]) {
        var savedPass = getStoredPass(savedRoomId) || '';
        if (savedPass) {
          target = savedRoomId;
          pass = savedPass;
        }
      }
      if (!target && Array.isArray(rooms)) {
        for (var i = 0; i < rooms.length; i++) {
          var meta = normalizeRoomMeta(rooms[i]);
          if (!meta) continue;
          var rid = meta.room_id;
          if (!rid) continue;
          var cached = getStoredPass(rid) || '';
          if (cached) {
            target = rid;
            pass = cached;
            break;
          }
        }
      }
      if (target) {
        var autoProvider = roomsById[target] ? roomsById[target].provider : null;
        if (autoProvider) {
          ensureProviderOption(autoProvider);
          if (providerEl) providerEl.value = autoProvider;
          applyProviderTheme(autoProvider);
        }
    joinRoom(target, pass, { allowProviderOverride: false }).catch(function(){ /* ignore auto-join failure */ });
      } else {
        showEmptyState('Select a room to join.');
      }
    });
    console.log('[ui-bound]');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.__sendMsg = send;
})();
