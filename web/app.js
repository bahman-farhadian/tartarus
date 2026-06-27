(() => {
  'use strict';

  // --- Navigation ---
  const navButtons = document.querySelectorAll('nav button[data-view]');
  const views = document.querySelectorAll('.view');

  function switchView(view) {
    navButtons.forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    views.forEach((v) => v.classList.remove('active'));
    document.getElementById(`view-${view}`).classList.add('active');
    if (view === 'lists') {
      loadWordLists();
    }
  }

  navButtons.forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });

  // In-page links (e.g. on the About page) that jump to another view.
  document.querySelectorAll('[data-view-link]').forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.viewLink));
  });

  // --- API helper ---
  async function api(path, options) {
    const res = await fetch(path, options);
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  function showError(el, message) {
    el.innerHTML = message ? `<div class="error">${escapeHtml(message)}</div>` : '';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // --- Speech (backend TTS via macOS say) ---
  // Fire-and-forget: POSTs text to the server which calls 'say' and returns
  // when done. Voice selection is server-side so Siri/system voices work.
  function speak(text) {
    if (!document.getElementById('practice-audio').checked) return;
    fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, lang: sessionLang }),
    }).catch(() => {});
  }

  // --- Practice state ---
  let sessionId = null;
  let sessionLang = '';
  let currentQuestion = null;
  let drillActive = false;
  let answering = false;

  const setupCard = document.getElementById('practice-setup');
  const sessionCard = document.getElementById('practice-session');
  const summaryCard = document.getElementById('practice-summary');
  const practiceError = document.getElementById('practice-error');

  const sessionProgress = document.getElementById('session-progress');
  const sessionGauge = document.getElementById('session-gauge');
  const sessionType = document.getElementById('session-type');
  const wordDisplay = document.getElementById('word-display');
  const definitionLines = document.getElementById('definition-lines');
  const answerBlock = document.getElementById('answer-block');
  const answerInput = document.getElementById('answer-input');
  const drillBlock = document.getElementById('drill-block');
  const drillRep = document.getElementById('drill-rep');
  const drillStreak = document.getElementById('drill-streak');
  const drillDots = document.getElementById('drill-dots');
  const feedback = document.getElementById('feedback');

  const btnReplay = document.getElementById('btn-replay');
  const btnReveal = document.getElementById('btn-reveal');
  const btnFlag = document.getElementById('btn-flag');
  const btnMaster = document.getElementById('btn-master');
  const btnDrill = document.getElementById('btn-drill');
  const btnEnd = document.getElementById('btn-end');

  const TYPE_LABELS = {
    learning: 'Learning',
    audio: 'Audio',
    spelling: 'Learning',
    production: 'Production',
  };

  document.getElementById('start-session').addEventListener('click', startSession);
  // Only text inputs get Enter-to-submit; selects use their native behaviour.
  document.getElementById('practice-audio-lang').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); startSession(); }
  });
  document.getElementById('summary-restart').addEventListener('click', () => {
    summaryCard.style.display = 'none';
    setupCard.style.display = 'block';
    document.getElementById('start-session').focus();
  });
  document.getElementById('submit-answer').addEventListener('click', submitTextAnswer);
  answerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitTextAnswer(); }
    // Prevent Tab from escaping the input to action buttons; Backspace is
    // handled in the input so no need to guard it here.
    if (e.key === 'Tab') { e.preventDefault(); }
  });

  btnReplay.addEventListener('click', replayAudio);
  btnReveal.addEventListener('click', revealWord);

  function replayAudio() {
    if (currentQuestion) speak(currentQuestion.word);
  }

  function revealWord() {
    if (!currentQuestion) return;
    speak(currentQuestion.word);
    if (currentQuestion.type === 'audio' || currentQuestion.type === 'spelling') {
      wordDisplay.classList.remove('hidden-word');
      setTimeout(() => {
        if (currentQuestion && (currentQuestion.type === 'audio' || currentQuestion.type === 'spelling')) {
          wordDisplay.classList.add('hidden-word');
        }
      }, 1200);
    }
    // 'production' (drill): only replay audio, never reveal the word.
  }

  btnFlag.addEventListener('click', () => sendAnswer('!'));
  btnMaster.addEventListener('click', () => sendAnswer('@'));
  btnDrill.addEventListener('click', () => sendAnswer('$'));
  btnEnd.addEventListener('click', () => sendAnswer('!!'));

  // After a session ends, Enter goes back to setup.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && summaryCard.style.display !== 'none') {
      e.preventDefault();
      document.getElementById('summary-restart').click();
    }
  });

  // During an active session, prevent Backspace from triggering browser
  // back-navigation when no input element is focused (macOS produces a
  // system alert sound when the browser tries to go back with no history).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Backspace') return;
    if (!sessionId) return;
    const tag = document.activeElement?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    e.preventDefault();
  });

  async function startSession() {
    showError(practiceError, '');
    const userInput = document.getElementById('practice-user');
    const langInput = document.getElementById('practice-lang');
    const user = userInput.value.trim();
    const lang = langInput.value.trim();
    const audioLang = (document.getElementById('practice-audio-lang')?.value ?? '').trim() || undefined;
    const drillMode = document.getElementById('practice-drill-mode')?.checked ?? false;
    if (!user || !lang) {
      showError(practiceError, 'User and language are required.');
      (user ? langInput : userInput).focus();
      return;
    }
    try {
      const body = { user, lang };
      if (audioLang) body.audio_lang = audioLang;
      if (drillMode) body.drill_mode = true;
      const data = await api('/api/practice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      sessionId = data.session_id;
      sessionLang = data.lang || '';
      setupCard.style.display = 'none';
      summaryCard.style.display = 'none';
      sessionCard.style.display = 'block';
      renderQuestion(data.question, data.progress);
    } catch (err) {
      showError(practiceError, err.message);
    }
  }

  function renderQuestion(question, progress) {
    currentQuestion = question;
    drillActive = false;
    answering = false;
    feedback.textContent = '';
    feedback.className = 'feedback';
    drillBlock.style.display = 'none';

    // Drill mode, band 1/2: auto-enter drill UI immediately.
    if (question.drill_start) {
      const q = progress.questions ?? 0;
      const maxQ = progress.max_questions ?? '?';
      sessionProgress.textContent = `Drilled ${progress.drilled ?? 0}/${progress.total} · Q${q}/${maxQ}`;
      sessionGauge.textContent = `${question.gauge} (score: ${question.score.toFixed(1)})`;
      sessionGauge.className = `gauge band-${question.band}`;
      sessionType.textContent = 'Drill';
      wordDisplay.textContent = question.word;
      wordDisplay.className = `word-display ${question.gender}`;
      definitionLines.innerHTML = '';
      setActionButtons(true);
      showDrill(question.drill_start);
      return;
    }

    const q = progress.questions ?? 0;
    const maxQ = progress.max_questions ?? '?';
    sessionProgress.textContent = `Correct ${progress.correct ?? 0}/${progress.total} · Q${q}/${maxQ}`;
    sessionGauge.textContent = `${question.gauge} (score: ${question.score.toFixed(1)})`;
    sessionGauge.className = `gauge band-${question.band}`;
    sessionType.textContent = TYPE_LABELS[question.type] || question.type;

    wordDisplay.textContent = question.word;
    wordDisplay.className = `word-display ${question.gender}`;

    definitionLines.innerHTML = '';
    if (question.type === 'learning' && question.definition.length) {
      question.definition.forEach((line) => {
        const div = document.createElement('div');
        div.textContent = line;
        definitionLines.appendChild(div);
      });
    }

    setActionButtons(true);

    if (question.type === 'production') {
      // Band 3: show definition + play audio; user types the word.
      answerBlock.style.display = 'flex';
      wordDisplay.classList.add('hidden-word');
      if (question.definition && question.definition.length) {
        question.definition.forEach((line) => {
          const div = document.createElement('div');
          div.textContent = line;
          definitionLines.appendChild(div);
        });
      }
      speak(question.word);
      answerInput.value = '';
      answerInput.focus();
    } else if (question.type === 'audio') {
      answerBlock.style.display = 'flex';
      wordDisplay.classList.add('hidden-word');
      answerInput.value = '';
      speak(question.word);
      answerInput.focus();
    } else if (question.type === 'spelling') {
      answerBlock.style.display = 'flex';
      wordDisplay.classList.remove('hidden-word');
      answerInput.value = '';
      speak(question.word);
      answerInput.focus();
      setTimeout(() => {
        if (currentQuestion === question) {
          wordDisplay.classList.add('hidden-word');
        }
      }, 700);
    } else {
      // learning
      answerBlock.style.display = 'flex';
      wordDisplay.classList.remove('hidden-word');
      answerInput.value = '';
      speak(question.word);
      answerInput.focus();
    }
  }

  function setActionButtons(enabled) {
    [btnFlag, btnMaster, btnDrill].forEach((b) => { b.disabled = !enabled; });
  }

  function submitTextAnswer() {
    const value = answerInput.value;
    // '+' and '?' are always local commands — never submitted as answers.
    if (value.trim() === '+') { replayAudio(); answerInput.value = ''; return; }
    if (value.trim() === '?') { revealWord(); answerInput.value = ''; return; }
    sendAnswer(value);
  }

  async function sendAnswer(answer) {
    if (!sessionId || answering) return;
    answering = true;
    setActionButtons(false);
    try {
      const data = await api('/api/practice/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, answer }),
      });
      handleAnswerResult(data);
    } catch (err) {
      answering = false;
      setActionButtons(true);
      showError(practiceError, err.message);
    }
  }

  function handleAnswerResult(data) {
    if (data.result === 'drill_start' || data.result === 'drill_progress') {
      answering = false;
      showDrill(data.drill);
      return;
    }

    if (data.result === 'correct') {
      feedback.textContent = `Correct! '${data.word}'`;
      feedback.className = 'feedback correct';
    } else if (data.result === 'incorrect') {
      feedback.textContent = data.message;
      feedback.className = 'feedback incorrect';
    } else if (data.result === 'mastered' || data.result === 'flagged' || data.result === 'drilled') {
      feedback.textContent = data.message;
      feedback.className = 'feedback info';
    } else if (data.result === 'end') {
      feedback.textContent = 'Session ended.';
      feedback.className = 'feedback info';
    }

    // Play audio for the answered word concurrently (fire-and-forget) so
    // feedback is visible immediately and the 700ms is purely for reading time.
    if (data.result === 'correct' || data.result === 'incorrect') {
      speak(data.word);
    }

    if (data.done) {
      setTimeout(() => showSummary(data.session), 700);
      return;
    }

    setActionButtons(true);
    setTimeout(() => renderQuestion(data.question, data.progress), 700);
  }

  function showDrill(drill) {
    drillActive = true;
    drillBlock.style.display = 'block';
    answerBlock.style.display = 'flex';
    setActionButtons(false);

    wordDisplay.classList.remove('hidden-word');
    if (drill.definition && drill.definition.length) {
      definitionLines.innerHTML = '';
      drill.definition.forEach((line) => {
        const div = document.createElement('div');
        div.textContent = line;
        definitionLines.appendChild(div);
      });
    }

    drillRep.textContent = drill.repetition;
    drillStreak.textContent = drill.correct_in_a_row;
    drillDots.textContent = '●'.repeat(drill.correct_in_a_row) + '○'.repeat(drill.target - drill.correct_in_a_row);

    if (drill.correct === true) {
      feedback.textContent = 'Correct!';
      feedback.className = 'feedback correct';
    } else if (drill.correct === false) {
      feedback.textContent = 'Incorrect. Streak reset.';
      feedback.className = 'feedback incorrect';
    } else {
      feedback.textContent = '';
      feedback.className = 'feedback';
    }

    answerInput.value = '';
    answerInput.focus();
    speak(currentQuestion.word);
  }

  function showSummary(session) {
    sessionCard.style.display = 'none';
    summaryCard.style.display = 'block';
    sessionId = null;
    currentQuestion = null;

    const minutes = Math.floor(session.elapsed_seconds / 60);
    const seconds = session.elapsed_seconds % 60;
    let html = '<ul class="summary-list">';
    html += `<li>Words practiced: <strong>${session.practiced}</strong></li>`;
    html += `<li>Correct answers: <strong>${session.correct}</strong></li>`;
    html += `<li>Incorrect answers: <strong>${session.incorrect.length}</strong></li>`;
    html += `<li>Words drilled: <strong>${session.drilled}</strong></li>`;
    html += `<li>Session time: <strong>${minutes}m ${seconds}s</strong></li>`;
    html += '</ul>';
    if (session.incorrect.length) {
      html += '<h3>Words you got wrong</h3><ul class="summary-list">';
      session.incorrect.forEach((item) => {
        html += `<li>You wrote '<strong>${escapeHtml(item.attempt)}</strong>', correct was '<strong>${escapeHtml(item.word)}</strong>'</li>`;
      });
      html += '</ul>';
    }
    document.getElementById('summary-body').innerHTML = html;
    loadUserProgress(document.getElementById('practice-user').value);
  }

  // --- Report ---
  document.getElementById('load-report').addEventListener('click', loadReport);

  async function loadReport() {
    const reportError = document.getElementById('report-error');
    const resultsEl = document.getElementById('report-results');
    showError(reportError, '');
    resultsEl.innerHTML = '';
    const userInput = document.getElementById('report-user');
    const langInput = document.getElementById('report-lang');
    const user = userInput.value.trim();
    const lang = langInput.value.trim();
    if (!user) {
      showError(reportError, 'User is required.');
      userInput.focus();
      return;
    }
    try {
      const params = new URLSearchParams({ user });
      if (lang) params.set('lang', lang);

      if (!lang) {
        const summaryData = await api(`/api/report/summary?user=${encodeURIComponent(user)}`);
        if (summaryData.summary) {
          resultsEl.appendChild(renderUserSummaryCard(summaryData.summary));
        }
      }

      const data = await api(`/api/report?${params.toString()}`);
      if (!data.reports.length && !resultsEl.hasChildNodes()) {
        resultsEl.innerHTML = '<div class="card muted">No practice sessions found.</div>';
      } else {
        data.reports.forEach((report) => {
          resultsEl.appendChild(renderReportTable(report));
        });
      }
      if (lang) {
        await loadWordListStats(user, lang, resultsEl);
      }
    } catch (err) {
      showError(reportError, err.message);
    }
  }

  function renderUserSummaryCard(summary) {
    const card = document.createElement('div');
    card.className = 'card';
    const streak = summary.streak;
    let html = `<h3>User Overview: ${escapeHtml(summary.user)}</h3>`;
    html += `<p class="muted">Streak &rsaquo; Current: <strong>${streak.current}</strong> day${streak.current !== 1 ? 's' : ''} &nbsp;&middot;&nbsp; Best: <strong>${streak.best}</strong> day${streak.best !== 1 ? 's' : ''}</p>`;
    html += '<table><caption>Daily Summary (All Languages)</caption>';
    html += '<thead><tr><th>Date</th><th>Sessions</th><th>Languages</th><th>Time</th>'
      + '<th>Words</th><th>Correct</th><th>Wrong</th><th>Accuracy</th><th>Avg/Word</th></tr></thead><tbody>';
    summary.days.forEach((day) => {
      const m = Math.floor(day.seconds / 60), s = day.seconds % 60;
      html += `<tr><td>${day.date}</td><td>${day.sessions}</td><td>${day.languages}</td>`
        + `<td>${m}m ${s}s</td><td>${day.practiced}</td><td>${day.correct}</td><td>${day.incorrect}</td>`
        + `<td>${day.accuracy != null ? day.accuracy + '%' : 'N/A'}</td>`
        + `<td>${day.avg_time != null ? day.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    });
    const t = summary.total;
    const th = Math.floor(t.seconds / 3600), tm = Math.floor((t.seconds % 3600) / 60);
    html += `<tr class="total-row"><td><strong>Total</strong></td><td>${t.sessions}</td><td>${t.languages}</td>`
      + `<td>${th}h ${tm}m</td><td>${t.practiced}</td><td>${t.correct}</td><td>${t.incorrect}</td>`
      + `<td>${t.accuracy != null ? t.accuracy + '%' : 'N/A'}</td>`
      + `<td>${t.avg_time != null ? t.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    html += '</tbody></table>';
    card.innerHTML = html;
    return card;
  }

  async function loadWordListStats(user, lang, container) {
    try {
      const params = new URLSearchParams({ user, lang });
      const data = await api(`/api/wordlist/stats?${params.toString()}`);
      if (data.words.length) {
        container.appendChild(renderWordStatsTable(lang, data.words));
      }
    } catch (err) {
      // No word list for this user/language yet - nothing to show.
    }
  }

  function renderWordStatsTable(lang, words) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>Word list: ${escapeHtml(lang)}</caption>`;
    html += '<thead><tr><th>Word</th><th>Score</th><th>Gauge</th><th>Box</th><th>Next Review</th>'
      + '<th>Practiced</th><th>Correct</th><th>Wrong</th><th>Drilled</th><th>Flagged</th><th>Mastered</th></tr></thead><tbody>';
    words.forEach((w) => {
      const nextReview = w.next_review ?? 'now';
      html += `<tr${w.active ? '' : ' class="muted"'}><td>${escapeHtml(w.word)}</td>`
        + `<td>${w.score.toFixed(1)}</td><td class="gauge band-${w.band}">${w.gauge}</td>`
        + `<td>${w.leitner_box ?? 1}</td><td>${nextReview}</td>`
        + `<td>${w.times_practiced}</td><td>${w.times_correct}</td><td>${w.times_incorrect}</td>`
        + `<td>${w.times_drilled}</td><td>${w.times_flagged}</td><td>${w.times_mastered}</td></tr>`;
    });
    html += '</tbody></table>';
    card.innerHTML = html;
    return card;
  }

  function renderReportTable(report) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>${escapeHtml(report.language)}</caption>`;
    html += '<thead><tr><th>Date</th><th>Sessions</th><th>Time</th><th>Practiced</th>'
      + '<th>Correct</th><th>Wrong</th><th>Drilled</th><th>Avg/Word</th></tr></thead><tbody>';
    report.days.forEach((day) => {
      const minutes = Math.floor(day.seconds / 60);
      const seconds = day.seconds % 60;
      html += `<tr><td>${day.date}</td><td>${day.sessions}</td><td>${minutes}m ${seconds}s</td>`
        + `<td>${day.practiced}</td><td>${day.correct}</td><td>${day.incorrect}</td>`
        + `<td>${day.drilled}</td><td>${day.avg_time != null ? day.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    });
    const t = report.total;
    const tHours = Math.floor(t.seconds / 3600);
    const tMinutes = Math.floor((t.seconds % 3600) / 60);
    html += `<tr class="total-row"><td>Total</td><td>${t.sessions}</td><td>${tHours}h ${tMinutes}m</td>`
      + `<td>${t.practiced}</td><td>${t.correct}</td><td>${t.incorrect}</td>`
      + `<td>${t.drilled}</td><td>${t.avg_time != null ? t.avg_time.toFixed(1) + 's' : 'N/A'}</td></tr>`;
    html += '</tbody></table>';
    card.innerHTML = html;
    return card;
  }

  // --- Word lists + cascading dropdowns ---

  let allWordLists = [];

  const KNOWN_BASE_LANGS = new Set([
    'german', 'english', 'french', 'spanish', 'italian', 'dutch', 'portuguese',
    'russian', 'japanese', 'chinese', 'korean', 'turkish', 'polish', 'swedish',
    'norwegian', 'danish', 'arabic',
  ]);

  // Populate a lang <select> for the currently chosen user.
  function refreshLangSelect(userSelId, langSelId, { allLangsDefault = false } = {}) {
    const user = document.getElementById(userSelId).value;
    const langSel = document.getElementById(langSelId);
    const prev = langSel.value;
    langSel.innerHTML = allLangsDefault
      ? '<option value="">All languages</option>'
      : '<option value="">Select word list…</option>';
    allWordLists
      .filter((w) => w.user === user)
      .forEach((w) => {
        const opt = document.createElement('option');
        opt.value = w.lang;
        opt.textContent = w.lang;
        if (w.lang === prev) opt.selected = true;
        langSel.appendChild(opt);
      });
  }

  // Populate a user <select> and rebuild the corresponding lang <select>.
  function refreshUserSelect(userSelId, langSelId, opts = {}) {
    const userSel = document.getElementById(userSelId);
    const prev = userSel.value;
    const users = [...new Set(allWordLists.map((w) => w.user))].sort();
    userSel.innerHTML = '<option value="">Select user…</option>';
    users.forEach((u) => {
      const opt = document.createElement('option');
      opt.value = u;
      opt.textContent = u;
      if (u === prev) opt.selected = true;
      userSel.appendChild(opt);
    });
    refreshLangSelect(userSelId, langSelId, opts);
  }

  // Wire up user→lang cascade for a section (call once at init).
  function setupCascade(userSelId, langSelId, opts = {}) {
    document.getElementById(userSelId).addEventListener('change', () => {
      refreshLangSelect(userSelId, langSelId, opts);
    });
  }

  // --- Progress widget ---
  const progressEl = document.getElementById('practice-progress');

  async function loadUserProgress(user) {
    if (!user) { progressEl.style.display = 'none'; return; }
    try {
      const data = await api(`/api/user/progress?user=${encodeURIComponent(user)}`);
      if (!data.lists || !data.lists.length) { progressEl.style.display = 'none'; return; }
      progressEl.innerHTML = renderProgressWidget(data.lists);
      progressEl.style.display = 'block';
    } catch (_) {
      progressEl.style.display = 'none';
    }
  }

  function renderProgressWidget(lists) {
    let html = '<div class="card"><h2>Progress</h2><div class="progress-list">';
    lists.forEach((item) => {
      const pct = Math.min(item.progress, 100);
      html += `<div class="progress-row">
        <div class="progress-header">
          <span class="progress-lang">${escapeHtml(item.lang)}</span>
          <span class="progress-pct">${item.progress.toFixed(1)}%</span>
        </div>
        <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${pct}%"></div></div>
        <div class="progress-meta">
          <span>${item.learned} / ${item.total} learned</span>`;
      if (item.to_drill > 0) {
        html += `<span class="drill-badge">${item.to_drill} to drill</span>`;
      }
      html += '</div></div>';
    });
    html += '</div></div>';
    return html;
  }

  document.getElementById('practice-user').addEventListener('change', function () {
    loadUserProgress(this.value);
  });

  // Auto-fill practice audio-lang when the word list changes.
  document.getElementById('practice-lang').addEventListener('change', function () {
    const lang = this.value;
    const audioEl = document.getElementById('practice-audio-lang');
    if (!lang) { audioEl.value = ''; return; }
    const base = lang.split('_')[0].toLowerCase();
    audioEl.value = (lang.includes('_') && KNOWN_BASE_LANGS.has(base)) ? base : '';
  });

  setupCascade('practice-user', 'practice-lang');
  setupCascade('report-user',   'report-lang',  { allLangsDefault: true });
  setupCascade('editor-user',   'editor-lang');

  async function loadWordLists() {
    const listsBody = document.getElementById('lists-body');
    listsBody.textContent = 'Loading...';
    try {
      const data = await api('/api/wordlists');
      allWordLists = data.wordlists || [];

      // Refresh all cascading dropdowns across the app.
      refreshUserSelect('practice-user', 'practice-lang');
      refreshUserSelect('report-user',   'report-lang',  { allLangsDefault: true });
      refreshUserSelect('editor-user',   'editor-lang');

      // Render the Word Lists tab.
      if (!allWordLists.length) {
        listsBody.innerHTML = '<span class="muted">No word lists yet. Create one below.</span>';
        return;
      }
      let html = '<ul class="summary-list">';
      allWordLists.forEach((wl) => {
        html += `<li><button class="link-btn" data-user="${escapeHtml(wl.user)}" data-lang="${escapeHtml(wl.lang)}">`
          + `<strong>${escapeHtml(wl.user)}</strong> / ${escapeHtml(wl.lang)}</button> `
          + `&mdash; <code>data/word_lists/${escapeHtml(wl.user)}_${escapeHtml(wl.lang)}.json</code></li>`;
      });
      html += '</ul>';
      listsBody.innerHTML = html;
      listsBody.querySelectorAll('.link-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          // Pre-select user & lang in the editor dropdowns then load.
          const uSel = document.getElementById('editor-user');
          const lSel = document.getElementById('editor-lang');
          uSel.value = btn.dataset.user;
          refreshLangSelect('editor-user', 'editor-lang');
          lSel.value = btn.dataset.lang;
          loadEditor();
        });
      });
    } catch (err) {
      listsBody.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    }
  }

  // Load word lists immediately so dropdowns are populated on first page load.
  // After the dropdowns settle, load progress for whichever user is pre-selected.
  loadWordLists().then(() => {
    const user = document.getElementById('practice-user').value;
    if (user) loadUserProgress(user);
  });

  // --- Word list editor ---
  const editorUser = document.getElementById('editor-user');
  const editorLang = document.getElementById('editor-lang');
  const editorTableWrap = document.getElementById('editor-table-wrap');
  const editorBody = document.getElementById('editor-body');
  const editorMessage = document.getElementById('editor-message');

  document.getElementById('editor-load').addEventListener('click', loadEditor);
  document.getElementById('editor-add-row').addEventListener('click', () => addEditorRow({}));
  document.getElementById('editor-save').addEventListener('click', saveEditor);

  async function loadEditor() {
    showError(editorMessage, '');
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    if (!user || !lang) {
      showError(editorMessage, 'User and language are required.');
      (user ? editorLang : editorUser).focus();
      return;
    }
    try {
      const params = new URLSearchParams({ user, lang });
      const data = await api(`/api/wordlist?${params.toString()}`);
      editorBody.innerHTML = '';
      data.words.forEach(addEditorRow);
      editorTableWrap.style.display = 'block';
    } catch (err) {
      showError(editorMessage, err.message);
    }
  }

  function addEditorRow(item) {
    const tr = document.createElement('tr');
    const fields = ['word', 'def1', 'def2'];
    fields.forEach((field) => {
      const td = document.createElement('td');
      const input = document.createElement('input');
      input.type = 'text';
      input.className = `editor-${field}`;
      input.value = item[field] || '';
      input.autocomplete = 'off';
      input.autocorrect = 'off';
      input.autocapitalize = 'off';
      input.spellcheck = false;
      td.appendChild(input);
      tr.appendChild(td);
    });
    const td = document.createElement('td');
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'secondary';
    removeBtn.textContent = '×';
    removeBtn.title = 'Remove';
    removeBtn.addEventListener('click', () => tr.remove());
    td.appendChild(removeBtn);
    tr.appendChild(td);
    editorBody.appendChild(tr);
  }

  async function saveEditor() {
    showError(editorMessage, '');
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    const words = [...editorBody.querySelectorAll('tr')].map((tr) => ({
      word: tr.querySelector('.editor-word').value.trim(),
      def1: tr.querySelector('.editor-def1').value.trim(),
      def2: tr.querySelector('.editor-def2').value.trim(),
    })).filter((w) => w.word);
    try {
      const data = await api('/api/wordlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang, words }),
      });
      editorMessage.innerHTML = `<div class="success">Saved ${data.count} word(s) to ${escapeHtml(data.path)}</div>`;
    } catch (err) {
      showError(editorMessage, err.message);
    }
  }

  async function createWordList() {
    const initMessage = document.getElementById('init-message');
    showError(initMessage, '');
    const userInput = document.getElementById('init-user');
    const langInput = document.getElementById('init-lang');
    const user = userInput.value.trim();
    const lang = langInput.value.trim();
    if (!user || !lang) {
      showError(initMessage, 'User and language are required.');
      (user ? langInput : userInput).focus();
      return;
    }
    try {
      const data = await api('/api/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang }),
      });
      initMessage.innerHTML = `<div class="success">${data.created ? 'Created' : 'Already existed'}: ${escapeHtml(data.path)}</div>`;
      loadWordLists();
    } catch (err) {
      showError(initMessage, err.message);
    }
  }

  document.getElementById('init-create').addEventListener('click', createWordList);
  ['init-user', 'init-lang'].forEach((id) => {
    document.getElementById(id).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); createWordList(); }
    });
  });

})();
