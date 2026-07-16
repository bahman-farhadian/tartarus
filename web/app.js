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
    if (!message) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="error">${escapeHtml(message).replace(/\n/g, '<br>')}</div>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // --- Speech (backend TTS via macOS say) ---
  // Returns a Promise that resolves when the server's 'say' finishes.
  // Callers that need to wait (answer flow) chain .then(); callers that
  // don't (question display, drill, replay) just call it without awaiting.
  function speak(text) {
    if (!document.getElementById('practice-audio').checked) return Promise.resolve();
    const wpmInput = document.getElementById('practice-wpm');
    let wpm = 128;
    if (wpmInput) {
      const parsed = parseInt(wpmInput.value, 10);
      if (!Number.isNaN(parsed) && parsed >= 30 && parsed <= 400) wpm = parsed;
    }
    return fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, lang: sessionLang, wpm }),
    }).then(() => {}).catch(() => {});
  }

  // --- Practice state ---
  let sessionId = null;
  let sessionLang = '';
  let sessionWpm = 128;
  let sessionFastMode = false;
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
  const submitAnswerButton = document.getElementById('submit-answer');
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
    known_review: 'Known review',
  };

  document.getElementById('start-session').addEventListener('click', startSession);
  const drillAllInput = document.getElementById('practice-drill-all');
  const drillModeInput = document.getElementById('practice-drill-mode');
  const knownDrillModeInput = document.getElementById('practice-known-drill-mode');
  const instantDrillInput = document.getElementById('practice-instant-drill');
  const fastModeInput = document.getElementById('practice-fast-mode');
  function isSentenceListName(lang) {
    return String(lang || '').toLowerCase().includes('sentences');
  }
  function syncSentenceDrillOptions() {
    const sentenceList = isSentenceListName(document.getElementById('practice-lang')?.value);
    [drillAllInput, drillModeInput, knownDrillModeInput, instantDrillInput].forEach((input) => {
      if (!input) return;
      input.disabled = sentenceList;
      input.closest('.check-option')?.classList.toggle('disabled', sentenceList);
      if (sentenceList) input.checked = false;
    });
  }

  function selectDrillMode(selected) {
    [drillAllInput, drillModeInput, knownDrillModeInput, instantDrillInput].forEach((input) => {
      input.checked = input === selected;
    });
    fastModeInput.checked = false;
  }

  drillModeInput.addEventListener('change', () => {
    if (drillModeInput.checked) selectDrillMode(drillModeInput);
  });
  knownDrillModeInput.addEventListener('change', () => {
    if (knownDrillModeInput.checked) selectDrillMode(knownDrillModeInput);
  });
  drillAllInput.addEventListener('change', () => {
    if (drillAllInput.checked) selectDrillMode(drillAllInput);
  });
  instantDrillInput.addEventListener('change', () => {
    if (instantDrillInput.checked) selectDrillMode(instantDrillInput);
  });
  fastModeInput.addEventListener('change', () => {
    if (fastModeInput.checked) {
      drillAllInput.checked = false;
      drillModeInput.checked = false;
      knownDrillModeInput.checked = false;
      instantDrillInput.checked = false;
    }
  });
  // Only text inputs get Enter-to-submit; selects use their native behaviour.
  document.getElementById('practice-audio-lang').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); startSession(); }
  });
  document.getElementById('summary-restart').addEventListener('click', () => {
    summaryCard.style.display = 'none';
    setupCard.style.display = 'block';
    document.getElementById('start-session').focus();
  });
  submitAnswerButton.addEventListener('click', submitTextAnswer);
  answerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitTextAnswer(); }
    // Prevent Tab from escaping the input to action buttons; Backspace is
    // handled in the input so no need to guard it here.
    if (e.key === 'Tab') { e.preventDefault(); }
  });
  answerInput.addEventListener('paste', (e) => e.preventDefault());

  function setAnswerInputEnabled(enabled) {
    answerInput.disabled = !enabled;
    submitAnswerButton.disabled = !enabled;
  }

  btnReplay.addEventListener('click', replayAudio);
  btnReveal.addEventListener('click', revealWord);

  function replayAudio() {
    if (currentQuestion) speak(currentQuestion.word_unmasked || currentQuestion.word);
  }

  function revealWord() {
    if (!currentQuestion) return;
    speak(currentQuestion.word_unmasked || currentQuestion.word);
    if (currentQuestion.type === 'audio' || currentQuestion.type === 'spelling') {
      wordDisplay.classList.remove('hidden-word');
      setTimeout(() => {
        if (currentQuestion && (currentQuestion.type === 'audio' || currentQuestion.type === 'spelling')) {
          wordDisplay.classList.add('hidden-word');
        }
      }, 1200);
    } else if (currentQuestion.sentence_mode) {
      // In sentence mode, reveal the full unmasked sentence briefly
      if (currentQuestion.word_unmasked) {
        const originalText = wordDisplay.textContent;
        wordDisplay.textContent = currentQuestion.word_unmasked;
        setTimeout(() => {
          if (currentQuestion && currentQuestion.sentence_mode) {
            wordDisplay.textContent = originalText;
          }
        }, 1500);
      }
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
    syncSentenceDrillOptions();
    const audioLang = (document.getElementById('practice-audio-lang')?.value ?? '').trim() || undefined;
    const drillMode = drillModeInput?.checked ?? false;
    const knownDrillMode = knownDrillModeInput?.checked ?? false;
    const drillAll = drillAllInput?.checked ?? false;
    const instantDrill = instantDrillInput?.checked ?? false;
    const fastMode = fastModeInput?.checked ?? false;
    const wpmInput = document.getElementById('practice-wpm');
    let wpm = 128;
    if (wpmInput) {
      const parsed = parseInt(wpmInput.value, 10);
      if (!Number.isNaN(parsed) && parsed >= 30 && parsed <= 400) wpm = parsed;
    }
    if (!user || !lang) {
      showError(practiceError, 'User and language are required.');
      (user ? langInput : userInput).focus();
      return;
    }
    try {
      const body = { user, lang, wpm };
      if (audioLang) body.audio_lang = audioLang;
      if (drillAll) body.drill_all = true;
      if (drillMode) body.drill_mode = true;
      if (knownDrillMode) body.known_drill_mode = true;
      if (instantDrill) body.instant_drill = true;
      if (fastMode) body.fast_mode = true;
      const data = await api('/api/practice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      sessionId = data.session_id;
      sessionLang = data.lang || '';
      sessionWpm = wpm;
      sessionFastMode = !!data.fast_mode;
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
    setAnswerInputEnabled(true);
    feedback.textContent = '';
    feedback.className = 'feedback';
    drillBlock.style.display = 'none';

    // Full drill mode: auto-enter drill UI immediately.
    if (question.drill_start) {
      const q = progress.questions ?? 0;
      const maxQ = progress.max_questions ?? '?';
      sessionProgress.textContent = `Drilled ${progress.drilled ?? 0}/${progress.total} · Q${q}/${maxQ}`;
      sessionGauge.textContent = `${question.gauge} (score: ${formatScore(question)})`;
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
    sessionGauge.textContent = `${question.gauge} (score: ${formatScore(question)})`;
    sessionGauge.className = `gauge band-${question.band}`;
    sessionType.textContent = TYPE_LABELS[question.type] || question.type;

    if (question.fast_mode) {
      sessionProgress.textContent = `Fast mode · ${progress.questions ?? 0}/${progress.max_questions ?? progress.total} · Correct ${progress.correct ?? 0}`;
      sessionGauge.textContent = 'Mastered';
      sessionGauge.className = 'gauge';
      sessionType.textContent = 'Fast mode';
      wordDisplay.textContent = question.word_unmasked || question.word;
      wordDisplay.className = `word-display ${question.gender}`;
      definitionLines.innerHTML = '';
      if (question.definition && question.definition.length) {
        question.definition.forEach((line) => {
          const div = document.createElement('div');
          div.textContent = line;
          definitionLines.appendChild(div);
        });
      }
      answerBlock.style.display = 'flex';
      answerInput.value = '';
      setActionButtons(true);
      speak(question.word_unmasked || question.word);
      answerInput.focus();
      return;
    }

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

    if (question.type === 'production' || question.type === 'known_review') {
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
      speak(question.word_unmasked || question.word);
      answerInput.value = '';
      answerInput.focus();
    } else if (question.type === 'audio') {
      answerBlock.style.display = 'flex';
      wordDisplay.classList.add('hidden-word');
      answerInput.value = '';
      speak(question.word_unmasked || question.word);
      answerInput.focus();
    } else if (question.type === 'spelling') {
      answerBlock.style.display = 'flex';
      wordDisplay.classList.remove('hidden-word');
      answerInput.value = '';
      speak(question.word_unmasked || question.word);
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
      speak(question.word_unmasked || question.word);
      answerInput.focus();
    }
  }

  function setActionButtons(enabled) {
    btnFlag.disabled = !enabled || sessionFastMode;
    btnMaster.disabled = !enabled || sessionFastMode;
    // Drill is disabled for sentence practice (sentences are too long to drill).
    btnDrill.disabled = !enabled || sessionFastMode || (currentQuestion && currentQuestion.sentence_mode);
    btnReveal.disabled = !enabled || sessionFastMode;
  }

  function formatScore(question) {
    return question && question.sentence_mode
      ? String(Math.round(question.score))
      : Number(question.score).toFixed(1);
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
    setAnswerInputEnabled(false);
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
      setAnswerInputEnabled(true);
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

    if (data.fast_retry) {
      answering = false;
      setAnswerInputEnabled(true);
      setActionButtons(true);
      feedback.textContent = data.message || 'Incorrect. Try again.';
      feedback.className = 'feedback incorrect';
      answerInput.value = '';
      answerInput.focus();
      speak(currentQuestion.word_unmasked || currentQuestion.word);
      return;
    }

    if (data.result === 'sentence_retry') {
      answering = false;
      setAnswerInputEnabled(true);
      setActionButtons(true);
      feedback.textContent = data.message || 'Incorrect. Try one more time.';
      feedback.className = 'feedback incorrect';
      answerInput.value = '';
      answerInput.focus();
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

    // Feedback is already shown above. Now advance:
    // - audio on: speak the word (server blocks until say finishes), then advance
    // - audio off: wait 700ms so the user can read the feedback, then advance
    const audioOn = document.getElementById('practice-audio').checked;
    const advance = () => {
      if (data.done) { showSummary(data.session); return; }
      setActionButtons(true);
      renderQuestion(data.question, data.progress);
    };

    if ((data.result === 'correct' || data.result === 'incorrect') && audioOn) {
      speak(data.word).then(advance);
    } else {
      setTimeout(advance, 700);
    }
  }

  function showDrill(drill) {
    drillActive = true;
    setAnswerInputEnabled(true);
    drillBlock.style.display = 'block';
    answerBlock.style.display = 'flex';
    setActionButtons(false);

    wordDisplay.classList.toggle('hidden-word', drill.show_word === false);
    definitionLines.innerHTML = '';
    if (drill.definition && drill.definition.length) {
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
    speak(currentQuestion.word_unmasked || currentQuestion.word);
  }

  function showSummary(session) {
    setAnswerInputEnabled(false);
    sessionCard.style.display = 'none';
    summaryCard.style.display = 'block';
    sessionId = null;
    currentQuestion = null;

    if (session.fast_mode) {
      sessionFastMode = false;
      const accuracy = session.accuracy == null ? 'N/A' : `${session.accuracy}%`;
      const average = session.avg_seconds_per_item == null ? 'N/A' : `${session.avg_seconds_per_item}s`;
      const minutes = Math.floor(session.elapsed_seconds / 60);
      const seconds = session.elapsed_seconds % 60;
      let html = '<ul class="summary-list">';
      html += `<li>Items reviewed: <strong>${session.practiced}</strong></li>`;
      html += `<li>Correct answers: <strong>${session.correct}</strong></li>`;
      html += `<li>Incorrect answers: <strong>${session.incorrect.length}</strong></li>`;
      html += `<li>Accuracy: <strong>${accuracy}</strong></li>`;
      html += `<li>Total time: <strong>${minutes}m ${seconds}s</strong></li>`;
      html += `<li>Average time per item: <strong>${average}</strong></li>`;
      html += '</ul>';
      document.getElementById('summary-body').innerHTML = html;
      loadUserProgress(document.getElementById('practice-user').value);
      return;
    }

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
        // Progress overview: per-list bars with due-today counts
        try {
          const progressData = await api(`/api/user/progress?user=${encodeURIComponent(user)}`);
          if (progressData.lists && progressData.lists.length) {
            resultsEl.appendChild(renderProgressOverview(progressData.lists));
          }
        } catch (_) {}
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
        // Dashboard analytics cards (before the word-by-word stats table)
        try {
          const dParams = new URLSearchParams({ user, lang });
          const dash = await api(`/api/dashboard?${dParams}`);
          const secHeader = document.createElement('div');
          secHeader.className = 'dash-section-header';
          secHeader.innerHTML = '<h2>Analytics</h2>';
          resultsEl.appendChild(secHeader);
          resultsEl.appendChild(renderDashCard1(dash.overview));
          const g1 = document.createElement('div');
          g1.className = 'dashboard-grid';
          g1.appendChild(renderDashCard4(dash.velocity, user, lang));
          if (dash.mastery) g1.appendChild(renderDashCard2(dash.mastery));
          resultsEl.appendChild(g1);
          if (dash.nemesis !== null && dash.prediction !== null) {
            const g2 = document.createElement('div');
            g2.className = 'dashboard-grid';
            g2.appendChild(renderDashCard3(dash.nemesis, user, lang));
            g2.appendChild(renderDashCard5(dash.prediction, lang));
            resultsEl.appendChild(g2);
          }
        } catch (_) {}
        await loadWordListStats(user, lang, resultsEl);
      }
    } catch (err) {
      showError(reportError, err.message);
    }
  }

  function renderDailyChart(days) {
    if (!days || days.length === 0) return '';
    // Oldest-to-newest for left→right bars, cap at 60 days
    const chartDays = [...days].reverse().slice(-60);
    const maxVal = Math.max(...chartDays.map((d) => d.practiced), 1);
    const bars = chartDays.map((day) => {
      const pct = day.practiced > 0 ? Math.max(4, Math.round(100 * day.practiced / maxVal)) : 0;
      return `<div class="day-bar${pct === 0 ? ' day-bar-empty' : ''}" style="height:${pct}%" title="${day.date}: ${day.practiced} words"></div>`;
    }).join('');
    return `<div class="daily-chart-wrap">
      <div class="daily-chart-label muted">Words practiced per day (last ${chartDays.length} day${chartDays.length !== 1 ? 's' : ''})</div>
      <div class="daily-chart">${bars}</div>
    </div>`;
  }

  function renderUserSummaryCard(summary) {
    const card = document.createElement('div');
    card.className = 'card';
    const streak = summary.streak;
    let html = `<h3>User Overview: ${escapeHtml(summary.user)}</h3>`;
    html += `<p class="muted">Streak &rsaquo; Current: <strong>${streak.current}</strong> day${streak.current !== 1 ? 's' : ''} &nbsp;&middot;&nbsp; Best: <strong>${streak.best}</strong> day${streak.best !== 1 ? 's' : ''}</p>`;
    html += renderDailyChart(summary.days);
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
    const params = new URLSearchParams({ user, lang });
    // Leitner stats card first
    try {
      const leitnerData = await api(`/api/wordlist/leitner?${params.toString()}`);
      if (leitnerData.leitner) {
        container.appendChild(renderLeitnerCard(lang, leitnerData.leitner));
      }
    } catch (_) {}
    // Full word list table
    try {
      const data = await api(`/api/wordlist/stats?${params.toString()}`);
      if (data.words.length) {
        container.appendChild(renderWordStatsTable(lang, data.words, 'Full Word List'));
      }
    } catch (_) {}
    // Due today table (separate)
    try {
      params.set('due_today', 'true');
      const data = await api(`/api/wordlist/stats?${params.toString()}`);
      if (data.words.length) {
        container.appendChild(renderWordStatsTable(lang, data.words, `Due Today (${data.words.length})`));
      }
    } catch (_) {}
  }

  function renderWordStatsTable(lang, words, caption) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>${escapeHtml(caption || `Word list: ${lang}`)}</caption>`;
    html += '<thead><tr><th>Word</th><th>Score</th><th>Gauge</th><th>Box</th><th>Next Review</th><th>Known Review</th>'
      + '<th>Practiced</th><th>Correct</th><th>Wrong</th><th>Drilled</th><th>Flagged</th><th>Mastered</th></tr></thead><tbody>';
    words.forEach((w) => {
      const nextReview = w.next_review ?? 'now';
      const knownReview = formatDateTime(w.last_known_review_at);
      html += `<tr${w.active ? '' : ' class="muted"'}><td>${escapeHtml(w.word)}</td>`
        + `<td>${w.score.toFixed(1)}</td><td class="gauge band-${w.band}">${w.gauge}</td>`
        + `<td>${w.leitner_box ?? 1}</td><td>${nextReview}</td><td>${knownReview}</td>`
        + `<td>${w.times_practiced}</td><td>${w.times_correct}</td><td>${w.times_incorrect}</td>`
        + `<td>${w.times_drilled}</td><td>${w.times_flagged}</td><td>${w.times_mastered}</td></tr>`;
    });
    html += '</tbody></table>';
    card.innerHTML = html;
    return card;
  }

  function formatDateTime(value) {
    if (!value) return 'never';
    return String(value).replace('T', ' ').split('.')[0];
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
    if (!userSel) return;
    const prev = userSel.value;
    const users = [...new Set((allWordLists || []).map((w) => w.user))].sort();
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
      if (item.due_today > 0) {
        html += `<span class="due-today-badge">${item.due_today} due today</span>`;
      }
      if (item.to_drill > 0) {
        html += `<span class="drill-badge">${item.to_drill} to drill</span>`;
      }
      html += '</div></div>';
    });
    html += '</div></div>';
    return html;
  }

  // Progress overview card used in the Report view (no specific lang selected).
  function renderProgressOverview(lists) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = '<h3>Word List Progress</h3><div class="progress-list">';
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
      if (item.due_today > 0) {
        html += `<span class="due-today-badge">${item.due_today} due today</span>`;
      }
      if (item.to_drill > 0) {
        html += `<span class="drill-badge">${item.to_drill} to drill</span>`;
      }
      html += '</div></div>';
    });
    html += '</div>';
    card.innerHTML = html;
    return card;
  }

  function renderLeitnerCard(lang, stats) {
    const card = document.createElement('div');
    card.className = 'card';
    const INTERVAL_LABEL = ['', 'Daily', 'Every 2 days', 'Every 4 days', 'Every 9 days', 'Every 14 days'];
    let html = `<h3>Leitner Flashcard Status &mdash; ${escapeHtml(lang)}</h3>`;

    // Top-level summary: four stat tiles
    html += '<div class="leitner-summary">';
    html += `<div class="leitner-stat-item"><span class="leitner-stat-num">${stats.total}</span><span class="muted">total</span></div>`;
    html += `<div class="leitner-stat-item lsi-learned"><span class="leitner-stat-num">${stats.learned}</span><span class="muted">learned</span></div>`;
    html += `<div class="leitner-stat-item lsi-new"><span class="leitner-stat-num">${stats.never_practiced}</span><span class="muted">new</span></div>`;
    html += `<div class="leitner-stat-item lsi-due"><span class="leitner-stat-num">${stats.due_today}</span><span class="muted">due today</span></div>`;
    html += '</div>';

    // Per-box breakdown
    html += '<div class="leitner-boxes">';
    for (let b = 1; b <= 5; b++) {
      const box = stats.boxes.find((x) => x.box === b) || { box: b, total: 0, learned: 0, due: 0 };
      const fillPct = stats.total > 0 ? Math.min(100, Math.round(100 * box.total / stats.total)) : 0;
      html += `<div class="leitner-box-row">
        <div class="leitner-box-meta">
          <span>Box ${b}</span>
          <span class="muted" style="font-size:0.78rem">${INTERVAL_LABEL[b]}</span>
        </div>
        <div class="leitner-bar-wrap"><div class="leitner-bar-fill" style="width:${fillPct}%"></div></div>
        <div class="leitner-box-counts">
          <span class="muted">${box.total} word${box.total !== 1 ? 's' : ''}</span>
          ${box.due > 0 ? `<span class="due-today-badge">${box.due} due</span>` : ''}
        </div>
      </div>`;
    }
    html += '</div>';
    card.innerHTML = html;
    return card;
  }

  document.getElementById('practice-user').addEventListener('change', function () {
    loadUserProgress(this.value);
  });

  // Auto-fill practice audio-lang when the word list changes.
  document.getElementById('practice-lang').addEventListener('change', function () {
    const lang = this.value;
    const audioEl = document.getElementById('practice-audio-lang');
    syncSentenceDrillOptions();
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
    } catch (err) {
      console.error('Failed to load word lists:', err);
      listsBody.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
      allWordLists = [];
    }

    // Always refresh dropdowns, even if API failed (will use cached/empty data).
    refreshUserSelect('practice-user', 'practice-lang');
    refreshUserSelect('report-user', 'report-lang', { allLangsDefault: true });
    refreshUserSelect('editor-user', 'editor-lang');

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
        const uSel = document.getElementById('editor-user');
        const lSel = document.getElementById('editor-lang');
        uSel.value = btn.dataset.user;
        refreshLangSelect('editor-user', 'editor-lang');
        lSel.value = btn.dataset.lang;
        loadEditor();
      });
    });
  }

  // Load word lists immediately so dropdowns are populated on first page load.
  // After the dropdowns settle, load progress for whichever user is pre-selected.
  loadWordLists().then(() => {
    const user = document.getElementById('practice-user').value;
    if (user) loadUserProgress(user);
  });

  // Fallback: ensure dropdowns are populated even if initial load failed
  async function ensureDropdownsPopulated(retries = 3) {
    const userSel = document.getElementById('practice-user');
    if (!userSel || userSel.options.length > 1) return;
    for (let i = 0; i < retries; i++) {
      try {
        await loadWordLists();
        if (userSel.options.length > 1) break;
      } catch (_) {}
      await new Promise(r => setTimeout(r, 200 * (i + 1)));
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensureDropdownsPopulated);
  } else {
    ensureDropdownsPopulated();
  }

  // --- Dashboard card renderers (used inside loadReport) ---

  // Card 1 — Current Status (scoped to selected list)
  function renderDashCard1(overview) {
    const card = document.createElement('div');
    card.className = 'card dash-card-full dash-card-overview';
    const h = Math.floor(overview.total_seconds / 3600);
    const m = Math.floor((overview.total_seconds % 3600) / 60);
    const accuracy = overview.overall_accuracy;
    const r = 38, circ = +(2 * Math.PI * r).toFixed(1);
    const filled = accuracy != null ? +(circ * accuracy / 100).toFixed(1) : 0;
    const arcColor = accuracy == null ? 'var(--surface1)'
      : accuracy >= 85 ? 'var(--green)' : accuracy >= 70 ? 'var(--yellow)' : 'var(--red)';
    const ringLabel = accuracy != null ? `${accuracy}%` : 'N/A';
    card.innerHTML = `
      <h3>Current Status</h3>
      <div class="stat-tiles">
        <div class="stat-tile">
          <span class="stat-num stat-due">${overview.due_today}</span>
          <span class="stat-label">Due Today</span>
        </div>
        <div class="stat-tile">
          <span class="stat-num">${overview.streak.current}<span class="stat-unit">day${overview.streak.current !== 1 ? 's' : ''}</span></span>
          <span class="stat-label">Current Streak</span>
        </div>
        <div class="stat-tile">
          <span class="stat-num">${h}h ${m}m</span>
          <span class="stat-label">Total Practice Time</span>
        </div>
        <div class="stat-tile stat-ring-tile">
          <svg width="90" height="90" viewBox="0 0 90 90" class="accuracy-ring">
            <circle cx="45" cy="45" r="${r}" fill="none" stroke="var(--surface1)" stroke-width="9"/>
            <circle cx="45" cy="45" r="${r}" fill="none" stroke="${arcColor}" stroke-width="9"
              stroke-dasharray="${filled} ${circ - filled}" stroke-linecap="round"
              transform="rotate(-90 45 45)"/>
            <text x="45" y="45" text-anchor="middle" dominant-baseline="middle"
              fill="${arcColor}" font-size="14" font-weight="700">${ringLabel}</text>
          </svg>
          <span class="stat-label">Overall Accuracy</span>
        </div>
      </div>`;
    return card;
  }

  // Card 2 — Mastery Funnel (per list)
  function renderDashCard2(mastery) {
    const card = document.createElement('div');
    card.className = 'card dash-card-mastery';
    const { learning, familiar, mastered, total } = mastery;
    const lPct = total ? Math.round(100 * learning / total) : 0;
    const fPct = total ? Math.round(100 * familiar / total) : 0;
    const mPct = 100 - lPct - fPct;
    const masteredPct = total ? Math.round(100 * mastered / total) : 0;
    card.innerHTML = `
      <h3>Mastery Funnel</h3>
      <div class="stacked-bar">
        ${lPct > 0 ? `<div class="stacked-seg seg-learning" style="width:${lPct}%" title="Learning: ${learning}"></div>` : ''}
        ${fPct > 0 ? `<div class="stacked-seg seg-familiar" style="width:${fPct}%" title="Familiar: ${familiar}"></div>` : ''}
        ${mPct > 0 ? `<div class="stacked-seg seg-mastered" style="width:${mPct}%" title="Mastered: ${mastered}"></div>` : ''}
      </div>
      <div class="stacked-legend">
        <span><span class="legend-dot dot-learning"></span>Learning: <strong>${learning}</strong></span>
        <span><span class="legend-dot dot-familiar"></span>Familiar: <strong>${familiar}</strong></span>
        <span><span class="legend-dot dot-mastered"></span>Mastered: <strong>${mastered}</strong></span>
      </div>
      <p class="muted insight-text">${mastered > 0
        ? `You&rsquo;ve pushed <strong>${masteredPct}%</strong> of your vocabulary into long-term memory.`
        : 'Keep practicing — mastered words will appear here.'
      }</p>`;
    return card;
  }

  // Card 3 — Nemesis Words (per list)
  function renderDashCard3(nemesis, user, lang) {
    const card = document.createElement('div');
    card.className = 'card dash-card-nemesis';
    if (!nemesis.length) {
      card.innerHTML = '<h3>Hardest Words</h3><p class="muted">No words with incorrect answers yet — great work!</p>';
      return card;
    }
    let rows = nemesis.map((w) =>
      `<tr><td>${escapeHtml(w.word)}</td><td>${w.times_incorrect}</td><td>${w.times_correct}</td><td>${w.score.toFixed(1)}</td></tr>`
    ).join('');
    card.innerHTML = `
      <h3>Hardest Words</h3>
      <table class="nemesis-table">
        <thead><tr><th>Word</th><th>Wrong</th><th>Right</th><th>Score</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <button type="button" class="secondary" id="btn-drill-nemesis" style="margin-top:0.75rem;">
        Drill these words
      </button>`;
    card.querySelector('#btn-drill-nemesis').addEventListener('click', () => {
      document.getElementById('practice-user').value = user;
      refreshLangSelect('practice-user', 'practice-lang');
      document.getElementById('practice-lang').value = lang;
      selectDrillMode(document.getElementById('practice-drill-mode'));
      switchView('practice');
    });
    return card;
  }

  // Card 4 — Velocity & Efficiency (scoped to selected list)
  function renderDashCard4(velocity, user, lang) {
    const card = document.createElement('div');
    card.className = 'card dash-card-velocity';
    const { avg_seconds_per_word, avg_words_per_day_7d, avg_minutes_per_day_7d, benchmark, enough_data } = velocity;
    const benchmarkColors = {
      'Hyper-Learner': 'var(--green)',
      'On Track': 'var(--green)',
      'Building Momentum': 'var(--yellow)',
      'Getting Started': 'var(--yellow)',
    };
    const badgeColor = benchmark ? (benchmarkColors[benchmark] || 'var(--subtext0)') : 'var(--subtext0)';
    const spwText = avg_seconds_per_word != null ? `${avg_seconds_per_word}s` : 'N/A';
    card.innerHTML = `
      <h3>Velocity &amp; Efficiency</h3>
      <div class="velocity-tiles">
        <div class="vel-tile">
          <span class="vel-num">${spwText}</span>
          <span class="vel-label muted">avg. per word</span>
        </div>
        <div class="vel-tile">
          <span class="vel-num">${avg_words_per_day_7d}</span>
          <span class="vel-label muted">words / day (7d avg)</span>
        </div>
        <div class="vel-tile">
          <span class="vel-num">${avg_minutes_per_day_7d}m</span>
          <span class="vel-label muted">practice / day (7d avg)</span>
        </div>
      </div>
      ${benchmark ? `<div class="benchmark-badge" style="color:${badgeColor}; border-color:${badgeColor};">${benchmark}</div>` : ''}
      ${!enough_data ? '<p class="muted" style="margin-top:0.75rem;font-size:0.85rem;">Practice a few more sessions to unlock full velocity stats.</p>' : ''}`;
    return card;
  }

  // Card 5 — Completion Forecast (per list)
  function renderDashCard5(prediction, lang) {
    const card = document.createElement('div');
    card.className = 'card dash-card-forecast';
    if (!prediction.enough_data) {
      const need = prediction.sessions_needed ?? 3;
      card.innerHTML = `
        <h3>Completion Forecast</h3>
        <p class="muted">We&rsquo;re still analyzing your learning speed. Practice for ${need} more session${need !== 1 ? 's' : ''} to unlock your forecast!</p>`;
      return card;
    }
    card.innerHTML = `
      <h3>Completion Forecast &mdash; ${escapeHtml(lang)}</h3>
      <div class="prediction-rows">
        <div class="pred-row">
          <div class="pred-label">Active practice needed</div>
          <div class="pred-value">${prediction.grind_hours}h to score all words 9.0</div>
        </div>
        <div class="pred-row">
          <div class="pred-label">Long-term memory (Box 5)</div>
          <div class="pred-value pred-date">${prediction.box5_date}</div>
        </div>
      </div>
      <p class="muted insight-text">At your current pace, every word in <strong>${escapeHtml(lang)}</strong> will be locked into long-term memory by <strong>${prediction.box5_date}</strong>. Keep it up!</p>`;
    return card;
  }

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
