(() => {
  'use strict';

  // --- Navigation ---
  const navButtons = document.querySelectorAll('nav button[data-view]');
  const views = document.querySelectorAll('.view');
  navButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      navButtons.forEach((b) => b.classList.remove('active'));
      views.forEach((v) => v.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`view-${btn.dataset.view}`).classList.add('active');
      if (btn.dataset.view === 'lists') {
        loadWordLists();
      }
    });
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

  // --- Speech (Web Speech API) ---
  function speak(text, locale) {
    if (!('speechSynthesis' in window)) return;
    if (!document.getElementById('practice-audio').checked) return;
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(text);
    if (locale) utter.lang = locale;
    window.speechSynthesis.speak(utter);
  }

  // --- Practice state ---
  let sessionId = null;
  let langLocale = '';
  let currentQuestion = null;
  let drillActive = false;
  let selectedOption = null;

  const setupCard = document.getElementById('practice-setup');
  const sessionCard = document.getElementById('practice-session');
  const summaryCard = document.getElementById('practice-summary');
  const practiceError = document.getElementById('practice-error');

  const sessionProgress = document.getElementById('session-progress');
  const sessionGauge = document.getElementById('session-gauge');
  const sessionType = document.getElementById('session-type');
  const wordDisplay = document.getElementById('word-display');
  const definitionLines = document.getElementById('definition-lines');
  const optionsBlock = document.getElementById('options-block');
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
    meaning: 'Meaning',
    spelling: 'Learning',
  };

  document.getElementById('start-session').addEventListener('click', startSession);
  document.getElementById('summary-restart').addEventListener('click', () => {
    summaryCard.style.display = 'none';
    setupCard.style.display = 'block';
  });
  document.getElementById('submit-answer').addEventListener('click', submitTextAnswer);
  answerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitTextAnswer();
  });

  btnReplay.addEventListener('click', replayAudio);
  btnReveal.addEventListener('click', revealWord);

  function replayAudio() {
    if (currentQuestion) speak(currentQuestion.word, langLocale);
  }

  function revealWord() {
    if (!currentQuestion) return;
    speak(currentQuestion.word, langLocale);
    if (currentQuestion.type === 'audio' || currentQuestion.type === 'spelling') {
      wordDisplay.classList.remove('hidden-word');
      setTimeout(() => {
        if (currentQuestion && (currentQuestion.type === 'audio' || currentQuestion.type === 'spelling')) {
          wordDisplay.classList.add('hidden-word');
        }
      }, 1200);
    }
  }

  btnFlag.addEventListener('click', () => sendAnswer('!'));
  btnMaster.addEventListener('click', () => sendAnswer('@'));
  btnDrill.addEventListener('click', () => sendAnswer('$'));
  btnEnd.addEventListener('click', () => sendAnswer('!!'));

  async function startSession() {
    showError(practiceError, '');
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-lang').value.trim();
    const number = parseInt(document.getElementById('practice-number').value, 10) || 20;
    if (!user || !lang) {
      showError(practiceError, 'User and language are required.');
      return;
    }
    try {
      const data = await api('/api/practice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang, number }),
      });
      sessionId = data.session_id;
      langLocale = data.lang_locale || '';
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
    selectedOption = null;
    feedback.textContent = '';
    feedback.className = 'feedback';
    drillBlock.style.display = 'none';

    sessionProgress.textContent = `Word ${progress.current}/${progress.total}`;
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

    if (question.type === 'meaning') {
      optionsBlock.style.display = 'flex';
      answerBlock.style.display = 'none';
      optionsBlock.innerHTML = '';
      question.options.forEach((opt, i) => {
        const letter = String.fromCharCode(97 + i);
        const btn = document.createElement('button');
        btn.className = 'option-btn';
        btn.innerHTML = `<span class="option-letter">${letter})</span> ${escapeHtml(opt)}`;
        btn.addEventListener('click', () => {
          optionsBlock.querySelectorAll('.option-btn').forEach((b) => b.classList.remove('selected'));
          btn.classList.add('selected');
          selectedOption = letter;
          submitAnswer(letter);
        });
        optionsBlock.appendChild(btn);
      });
      wordDisplay.classList.remove('hidden-word');
      speak(question.word, langLocale);
    } else if (question.type === 'audio') {
      optionsBlock.style.display = 'none';
      answerBlock.style.display = 'flex';
      wordDisplay.classList.add('hidden-word');
      answerInput.value = '';
      answerInput.focus();
      speak(question.word, langLocale);
    } else if (question.type === 'spelling') {
      optionsBlock.style.display = 'none';
      answerBlock.style.display = 'flex';
      wordDisplay.classList.remove('hidden-word');
      answerInput.value = '';
      speak(question.word, langLocale);
      setTimeout(() => {
        if (currentQuestion === question) {
          wordDisplay.classList.add('hidden-word');
          answerInput.focus();
        }
      }, 700);
    } else {
      // learning
      optionsBlock.style.display = 'none';
      answerBlock.style.display = 'flex';
      wordDisplay.classList.remove('hidden-word');
      answerInput.value = '';
      answerInput.focus();
      speak(question.word, langLocale);
    }
  }

  function setActionButtons(enabled) {
    [btnFlag, btnMaster, btnDrill].forEach((b) => { b.disabled = !enabled; });
  }

  function submitTextAnswer() {
    const value = answerInput.value;
    // '?' and '+' are repeat/replay commands, handled locally - just like
    // the CLI, they never count as an answer attempt (except during drill,
    // where any input is checked against the word).
    if (!drillActive && (value.trim() === '?' || value.trim() === '+')) {
      if (value.trim() === '?') revealWord();
      else replayAudio();
      answerInput.value = '';
      return;
    }
    if (drillActive) {
      sendAnswer(value);
      return;
    }
    submitAnswer(value);
  }

  function submitAnswer(value) {
    sendAnswer(value);
  }

  async function sendAnswer(answer) {
    if (!sessionId) return;
    try {
      const data = await api('/api/practice/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, answer }),
      });
      handleAnswerResult(data);
    } catch (err) {
      showError(practiceError, err.message);
    }
  }

  function handleAnswerResult(data) {
    if (data.result === 'drill_start' || data.result === 'drill_progress') {
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

    if (data.done) {
      setTimeout(() => showSummary(data.session), 800);
      return;
    }

    setActionButtons(true);
    setTimeout(() => renderQuestion(data.question, data.progress), 700);
  }

  function showDrill(drill) {
    drillActive = true;
    drillBlock.style.display = 'block';
    optionsBlock.style.display = 'none';
    answerBlock.style.display = 'flex';
    setActionButtons(false);

    wordDisplay.classList.add('hidden-word');
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
    speak(currentQuestion.word, langLocale);
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
  }

  // --- Report ---
  document.getElementById('load-report').addEventListener('click', loadReport);

  async function loadReport() {
    const reportError = document.getElementById('report-error');
    const resultsEl = document.getElementById('report-results');
    showError(reportError, '');
    resultsEl.innerHTML = '';
    const user = document.getElementById('report-user').value.trim();
    const lang = document.getElementById('report-lang').value.trim();
    if (!user) {
      showError(reportError, 'User is required.');
      return;
    }
    try {
      const params = new URLSearchParams({ user });
      if (lang) params.set('lang', lang);
      const data = await api(`/api/report?${params.toString()}`);
      if (!data.reports.length) {
        resultsEl.innerHTML = '<div class="card muted">No practice sessions found.</div>';
        return;
      }
      data.reports.forEach((report) => {
        resultsEl.appendChild(renderReportTable(report));
      });
    } catch (err) {
      showError(reportError, err.message);
    }
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

  // --- Word lists ---
  async function loadWordLists() {
    const listsBody = document.getElementById('lists-body');
    listsBody.textContent = 'Loading...';
    try {
      const data = await api('/api/wordlists');
      if (!data.wordlists.length) {
        listsBody.innerHTML = '<span class="muted">No word lists yet. Create one below.</span>';
        return;
      }
      let html = '<ul class="summary-list">';
      data.wordlists.forEach((wl) => {
        html += `<li><strong>${escapeHtml(wl.user)}</strong> / ${escapeHtml(wl.lang)} `
          + `&mdash; <code>data/word_lists/${escapeHtml(wl.user)}_${escapeHtml(wl.lang)}.json</code></li>`;
      });
      html += '</ul>';
      listsBody.innerHTML = html;
    } catch (err) {
      listsBody.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    }
  }

  document.getElementById('init-create').addEventListener('click', async () => {
    const initMessage = document.getElementById('init-message');
    showError(initMessage, '');
    const user = document.getElementById('init-user').value.trim();
    const lang = document.getElementById('init-lang').value.trim();
    if (!user || !lang) {
      showError(initMessage, 'User and language are required.');
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
  });
})();
