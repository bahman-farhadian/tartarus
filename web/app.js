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
    } else if (view === 'today') {
      refreshTodayOverview();
    }
  }

  navButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      if (speechPending > 0) return;
      switchView(btn.dataset.view);
    });
  });

  // In-page links (e.g. on the About page) that jump to another view.
  document.querySelectorAll('[data-view-link]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (speechPending > 0) return;
      switchView(btn.dataset.viewLink);
    });
  });

  // --- API helper ---
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body != null && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    const res = await fetch(path, { ...options, cache: 'no-store', headers });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  // --- Client-side event/error reporting -> server log ---
  // Best-effort and fire-and-forget: logging must never itself break the page.
  function reportClientEvent(level, message, extra = {}) {
    try {
      const user = document.getElementById('practice-user')?.value
        || document.getElementById('editor-user')?.value || '';
      fetch('/api/client-log', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ level, message: String(message).slice(0, 2000), url: location.pathname, user, ...extra }),
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }

  window.addEventListener('error', (e) => {
    reportClientEvent('error', e.message || 'Uncaught error',
      { stack: e.error && e.error.stack ? String(e.error.stack).slice(0, 2000) : '' });
  });
  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason;
    reportClientEvent('error', (reason && reason.message) ? reason.message : String(reason),
      { stack: (reason && reason.stack) ? String(reason.stack).slice(0, 2000) : '' });
  });

  function showError(el, message) {
    if (!message) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="error">${escapeHtml(message).replace(/\n/g, '<br>')}</div>`;
    reportClientEvent('warn', message, { context: el.id || '' });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Some German B1/B2 noun lists alone run 4,000-5,000+ words, and a
  // language's total across all levels/parts-of-speech can pass 20,000 --
  // a raw count that size overflows a dropdown's closed-state text (browsers
  // clip native <select> labels with no ellipsis). Keep counts compact and
  // consistent everywhere a list-size shows up.
  function formatCount(n) {
    n = Number(n) || 0;
    if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
    return String(n);
  }

  // --- Speech (backend TTS via macOS say) ---
  let speechTail = Promise.resolve();
  let speechPending = 0;

  // Bundled content has pre-generated audio in a per-list database; personal/
  // custom lists don't, and fall back to live server-side say() as before.
  // Returns true if pre-generated audio was found and played.
  async function playPreGeneratedAudio(user, lang, text) {
    if (!user || !lang) return false;
    const params = new URLSearchParams({ user, lang, text });
    let response;
    try {
      response = await fetch(`/api/audio?${params.toString()}`);
    } catch (err) {
      return false;
    }
    if (!response.ok) return false;
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    try {
      await new Promise((resolve) => {
        const audio = new Audio(url);
        audio.addEventListener('ended', resolve, { once: true });
        audio.addEventListener('error', resolve, { once: true });
        audio.play().catch(resolve);
      });
    } finally {
      URL.revokeObjectURL(url);
    }
    return true;
  }

  function speak(text) {
    // TTS is queued so prompts/feedback never overlap. Stage policy decides
    // whether speech is automatic, manual-only, or disabled.
    const request = async () => {
      const played = await playPreGeneratedAudio(sessionUser, sessionListId, text);
      if (played) return;
      try {
        await fetch('/api/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, lang: sessionLang }),
        });
      } catch (err) { /* best-effort, matches the previous swallow-errors behavior */ }
    };
    speechPending += 1;
    // During speech only prompt typing may remain available. All buttons and
    // submit/navigation actions are locked until the queued speech finishes.
    answerSubmitReady = false;
    wordDisplay.classList.remove('can-submit');
    setActionButtons(false);
    const queued = speechTail.then(request, request);
    speechTail = queued.finally(() => {
      speechPending = Math.max(0, speechPending - 1);
      if (speechPending === 0) restoreInteractionAfterSpeech();
    });
    return speechTail;
  }


  function focusCurrentAnswer() {
    if (!currentQuestion) return;
    answerInput.focus();
  }

  function restoreInteractionAfterSpeech() {
    if (speechPending > 0 || !sessionId || !currentQuestion || answering) return;
    setAnswerInputEnabled(true);
    setActionButtons(true);
    focusCurrentAnswer();
  }

  // Audio must never be muted during practice, in any stage -- every
  // question plays its prompt automatically, and Replay always works.
  function automaticAudioAllowed(_type) {
    return true;
  }

  function replayAudioAllowed(_type) {
    return true;
  }

  function presentQuestionAudio(question, onReady) {
    // Prompt speech is the only speech interval where typing is permitted.
    // Submit/Enter, session controls, navigation and card changes stay locked.
    setAnswerInputEnabled(true, false);
    setActionButtons(false);
    focusCurrentAnswer();
    return speak(questionAudioText(question)).then(() => {
      if (currentQuestion === question && !answering) {
        onReady?.();
        restoreInteractionAfterSpeech();
      }
    });
  }

  function questionAudioText(question) {
    return question.audio_text || question.word_unmasked || question.word;
  }

  // --- Practice state ---
  let sessionId = null;
  let sessionLang = '';
  let sessionUser = '';
  let sessionListId = '';
  let currentQuestion = null;
  let drillActive = false;
  let answering = false;
  let answerSubmitReady = false;
  let answerTarget = '';
  let answerPrompt = '';

  const setupCard = document.getElementById('practice-setup');
  const supplementaryCard = document.getElementById('practice-supplementary');
  const practiceOverview = document.getElementById('practice-overview');
  const sessionCard = document.getElementById('practice-session');
  const summaryCard = document.getElementById('practice-summary');
  const practiceError = document.getElementById('practice-error');

  const sessionProgress = document.getElementById('session-progress');
  const sessionGauge = document.getElementById('session-gauge');
  const sessionType = document.getElementById('session-type');
  const wordDisplay = document.getElementById('word-display');
  const definitionLines = document.getElementById('definition-lines');
  const answerInput = document.getElementById('answer-input');
  const drillBlock = document.getElementById('drill-block');
  const drillRep = document.getElementById('drill-rep');
  const drillStreak = document.getElementById('drill-streak');
  const drillTargetLabel = document.getElementById('drill-target');
  const drillDots = document.getElementById('drill-dots');
  const answerTimerWrap = document.getElementById('answer-timer-wrap');
  const answerTimerBar = document.getElementById('answer-timer-bar');
  const answerTimerLabel = document.getElementById('answer-timer-label');
  const feedback = document.getElementById('feedback');
  const btnReplay = document.getElementById('btn-replay');
  const btnEnd = document.getElementById('btn-end');
  const sessionControlNote = document.querySelector('.session-control-note');

  const TYPE_LABELS = {
    learning: 'Learning',
    production: 'Reverse Translation',
    cued_recall: 'Fading Structure',
    effortful_retrieval: 'Heavy Masking',
    free_recall: 'Audio on Demand',
    reconsolidation: 'Reverse Translation',
    automaticity: 'Speed Production',
    spaced_maintenance: 'Spaced Maintenance',
    encoding_practice: 'Encoding Practice',
    retrieval_reading: 'Reading Retrieval',
    retrieval_listening: 'Listening Retrieval',
  };

  function isMaskableCharacter(ch) {
    return /[\p{L}\p{N}]/u.test(String(ch || ''));
  }

  function fullyMaskedTarget(target) {
    return Array.from(String(target || ''))
      .map((ch) => isMaskableCharacter(ch) ? '_' : ch)
      .join('');
  }

  function promptForQuestion(question) {
    const target = String(question?.word_unmasked || '');
    const supplied = String(question?.word || '');
    const hiddenMode = ['production', 'effortful_retrieval', 'free_recall', 'reconsolidation', 'automaticity', 'spaced_maintenance', 'retrieval_reading', 'retrieval_listening'].includes(question?.type);
    if (hiddenMode || !supplied) return fullyMaskedTarget(target);
    return Array.from(supplied).length === Array.from(target).length ? supplied : fullyMaskedTarget(target);
  }

  function setAnswerSurface(target, prompt) {
    answerTarget = String(target || '');
    answerPrompt = String(prompt || '');
    if (Array.from(answerPrompt).length !== Array.from(answerTarget).length) {
      answerPrompt = fullyMaskedTarget(answerTarget);
    }
    // Typing past the target is never valid input, so don't let it happen:
    // cap the field at exactly the target's length (paste is already
    // blocked separately below).
    if (answerInput) answerInput.maxLength = Array.from(answerTarget).length;
    renderAnswerSurface();
  }

  function renderDefinitionPanel(lines) {
    if (!definitionLines) return;
    const values = Array.isArray(lines)
      ? lines.map((line) => String(line ?? '')).filter((line) => line.length > 0)
      : [];
    definitionLines.replaceChildren();
    definitionLines.classList.toggle('has-content', values.length > 0);
    // Always render exactly two line slots (primary + context), regardless
    // of how many real lines this question has -- 0 for Listening
    // Retrieval, 1 for most recall types, 2 for Encoding. An empty slot
    // stays in the layout (reserved height, hidden via CSS) instead of
    // being omitted, so the panel's height never changes as a session
    // moves between question types, or as Reading/Listening Retrieval's
    // reveal adds a second line mid-question -- nothing below it shifts,
    // ever, the same "reserve space unconditionally" rule the answer
    // timer and drill box already follow.
    // A genuinely empty div's line box isn't reliable across browsers --
    // give the hidden slot real (invisible) text content instead, so its
    // height always matches a populated line's exactly.
    const primary = document.createElement('div');
    primary.className = 'definition-primary' + (values[0] ? '' : ' definition-empty');
    primary.textContent = values[0] || ' ';
    definitionLines.appendChild(primary);

    const context = document.createElement('div');
    context.className = 'definition-context' + (values[1] ? '' : ' definition-empty');
    context.textContent = values[1] || ' ';
    definitionLines.appendChild(context);

    // Unusually long custom material with more than two lines still
    // renders in full -- only the common 0/1/2-line cases are guaranteed
    // shift-free.
    values.slice(2).forEach((line) => {
      const div = document.createElement('div');
      div.className = 'definition-context';
      div.textContent = line;
      definitionLines.appendChild(div);
    });
  }

  function renderAnswerSurface() {
    if (!wordDisplay) return;
    const target = Array.from(answerTarget);
    const prompt = Array.from(answerPrompt);
    const typed = Array.from(answerInput?.value || '');
    const sequence = document.createElement('span');
    sequence.className = 'answer-sequence';

    // The learning surface deliberately uses one immutable character cell per
    // target character.  The cell geometry never depends on the glyph, mask,
    // glow, or typed value, so the learner's focal point cannot shift while
    // spelling.  A monospace stack makes the visual advance deterministic too.
    target.forEach((targetChar, index) => {
      const span = document.createElement('span');
      span.className = 'answer-char';

      const isSpace = /\s/u.test(targetChar);
      const maskable = isMaskableCharacter(targetChar);
      if (isSpace) span.classList.add('space');
      else if (!maskable) span.classList.add('punctuation');

      if (index < typed.length) {
        // Render exactly what the learner typed. Spaces remain real spaces and
        // punctuation remains punctuation; neither is converted to a fake gap.
        span.textContent = typed[index];
        span.classList.add('typed');
      } else if (!maskable) {
        // Preserve the target's text structure at every masking level. A comma,
        // period, apostrophe, hyphen, or whitespace character is always drawn
        // literally (dim until reached, bright once typed). Only letters/digits
        // may become underscore fill slots.
        span.textContent = targetChar;
        span.classList.add('prompt');
      } else {
        const ch = prompt[index] ?? '_';
        span.textContent = ch;
        span.classList.add(ch === '_' ? 'masked' : 'prompt');
      }

      if (!answering && typed.length === index) span.classList.add('caret-before');
      if (!answering && typed.length === target.length && index === target.length - 1) {
        span.classList.add('caret-after');
      }
      sequence.appendChild(span);
    });

    // Keep over-typing visible without letting it change or escape the target
    // grid.  The correction tail is a separate bounded row below the target,
    // so the target stays centered and immutable while long accidental input
    // remains visible and backspaceable before submission.
    const overflow = typed.slice(target.length);
    const children = [sequence];
    if (overflow.length) {
      const tail = document.createElement('span');
      tail.className = 'answer-extra-tail';
      overflow.forEach((character, index) => {
        const span = document.createElement('span');
        span.className = 'answer-char typed extra';
        span.textContent = character;
        if (!answering && index === overflow.length - 1) span.classList.add('caret-after');
        tail.appendChild(span);
      });
      children.push(tail);
    }

    wordDisplay.replaceChildren(...children);
    const maskableIndexes = target.map((ch, index) => isMaskableCharacter(ch) ? index : -1).filter((index) => index >= 0);
    wordDisplay.classList.toggle(
      'fully-masked',
      maskableIndexes.length > 0 && maskableIndexes.every((index) => prompt[index] === '_'),
    );
    wordDisplay.classList.toggle('long-target', target.length >= 48);
    wordDisplay.classList.toggle('very-long-target', target.length >= 90);
    wordDisplay.dataset.typedLength = String(typed.length);
  }

  // --- Consolidation Track status panel helpers ---
  const consolidationStageLabel = document.getElementById('consolidation-stage-label');
  const consolidationDayLabel = document.getElementById('consolidation-day-label');
  const consolidationSessionsLabel = document.getElementById('consolidation-sessions-label');
  const consolidationModeLabel = document.getElementById('consolidation-mode-label');

  async function fetchTrend(user, lang, metric) {
    if (!user || !lang) return [];
    const params = new URLSearchParams({ user, lang, metric });
    const data = await api(`/api/report/trend?${params.toString()}`);
    return Array.isArray(data.series) ? data.series : [];
  }

  async function fetchConsolidationStatus(user, lang) {
    if (!user || !lang) {
      if (practiceOverview) practiceOverview.style.display = 'none';
      const startButton = document.getElementById('start-session');
      if (startButton) startButton.disabled = false;
      return;
    }
    try {
      // Just the compact "what's next" status line here -- the full
      // roadmap/dashboard/word-stats detail lives in the merged live
      // report below (refreshPracticeReport), not duplicated here too.
      const data = await api(`/api/consolidation/progress?user=${encodeURIComponent(user)}&lang=${encodeURIComponent(lang)}`);
      const p = data.progress;
      if (!p) return;
      if (practiceOverview) practiceOverview.style.display = '';
      if (consolidationStageLabel) consolidationStageLabel.textContent = 'Per-word Consolidation Track';
      if (consolidationDayLabel) {
        consolidationDayLabel.textContent = `${p.encoding || 0} in Encoding · ${p.reinforcement_total || 0} in the 10-day track · ${p.long_term_review || 0} in long-term review`;
      }

      const maintenanceReady = data.roadmap ? Number(data.roadmap.maintenance_ready || 0) : 0;
      const nothingAvailable = Number(p.available_tasks || 0) === 0 && maintenanceReady === 0;
      const startButton = document.getElementById('start-session');
      if (startButton) startButton.disabled = nothingAvailable;

      if (consolidationSessionsLabel) {
        if (nothingAvailable) {
          consolidationSessionsLabel.textContent = p.complete
            ? 'The Consolidation Track is complete for this material; no Spaced Maintenance review is due today.'
            : 'Nothing left to practice here today — pick different material.';
        } else if (p.due_reinforcement) {
          consolidationSessionsLabel.textContent = `${p.due_reinforcement} reinforcement item${p.due_reinforcement === 1 ? '' : 's'} ready first`;
        } else if (maintenanceReady) {
          consolidationSessionsLabel.textContent = `${maintenanceReady} Spaced Maintenance review item${maintenanceReady === 1 ? '' : 's'} ready now`;
        } else {
          consolidationSessionsLabel.textContent = `${p.encoding || 0} item${p.encoding === 1 ? '' : 's'} left to master`;
        }
      }
      if (consolidationModeLabel) {
        consolidationModeLabel.textContent = 'Each mastered word follows its own 10-day clock; due review always comes first.';
      }
    } catch (err) {
      if (practiceOverview) practiceOverview.style.display = 'none';
      showError(practiceError, `Could not load Consolidation Track status: ${err.message}`);
      const startButton = document.getElementById('start-session');
      if (startButton) startButton.disabled = false;
    }
  }


  document.getElementById('start-session').addEventListener('click', () => startSession());
  const practiceTrackButtons = {
    'start-encoding-practice': 'encoding_practice',
    'start-retrieval-reading': 'retrieval_reading',
    'start-retrieval-listening': 'retrieval_listening',
  };
  Object.entries(practiceTrackButtons).forEach(([id, track]) => {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener('click', () => startSession(track));
  });
  async function restorePracticeSetup() {
    summaryCard.style.display = 'none';
    setupCard.style.display = 'block';
    if (supplementaryCard) supplementaryCard.style.display = '';

    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();

    // Rebuild the complete setup view after a session. The status and live
    // report are the same components shown before practice; returning from
    // a summary must not degrade the setup into a partial view.
    await Promise.all([
      fetchConsolidationStatus(user, lang),
      refreshPracticeReport(),
    ]);

    document.getElementById('start-session').focus();
  }

  document.getElementById('summary-restart').addEventListener('click', () => {
    restorePracticeSetup().catch((err) => showError(practiceError, err.message));
  });

  // Shift+Enter is a transport shortcut for Replay. It is handled as a
  // keyboard action, never as answer text, so the answer field remains a
  // strict dataset-string channel.
  document.addEventListener('keydown', (e) => {
    if (!sessionId || e.key !== 'Enter' || !e.shiftKey) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    replayAudio();
  }, true);

  answerInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitTextAnswer(); }
    // Keep the learner's visual focus on the inline answer surface.
    if (e.key === 'Tab') { e.preventDefault(); }
  });
  answerInput.addEventListener('input', renderAnswerSurface);
  window.addEventListener('resize', () => { if (answerTarget) requestAnimationFrame(renderAnswerSurface); });
  answerInput.addEventListener('focus', () => wordDisplay.classList.add('is-focused'));
  answerInput.addEventListener('blur', () => wordDisplay.classList.remove('is-focused'));
  answerInput.addEventListener('paste', (e) => e.preventDefault());
  wordDisplay.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    if (!answerInput.disabled) answerInput.focus();
  });

  function answerInteractionLocked() {
    return answering || speechPending > 0;
  }

  function isAnswerControl(target) {
    return target === answerInput;
  }

  for (const eventName of ['keydown', 'beforeinput', 'input']) {
    document.addEventListener(eventName, (event) => {
      if (!answering || !isAnswerControl(event.target)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.target instanceof HTMLInputElement) { event.target.value = ''; renderAnswerSurface(); }
    }, true);
  }

  document.addEventListener('keydown', (event) => {
    if (!sessionId || event.key !== 'Escape') return;
    event.preventDefault();
    if (answerInteractionLocked()) return;
    cancelSession();
  });

  function setAnswerInputEnabled(enabled, allowSubmit = enabled) {
    const allowTyping = enabled && !answering;
    answerInput.disabled = !allowTyping;
    answerInput.readOnly = !allowTyping;
    answerSubmitReady = Boolean(allowSubmit && allowTyping && speechPending === 0);
    wordDisplay.classList.toggle('is-disabled', !allowTyping);
    wordDisplay.classList.toggle('can-submit', answerSubmitReady);
  }

  btnReplay.addEventListener('click', replayAudio);
  btnEnd.addEventListener('click', cancelSession);

  function replayAudio() {
    if (!currentQuestion || speechPending > 0 || answering) return Promise.resolve();
    if (!replayAudioAllowed(currentQuestion.type)) {
      feedback.textContent = 'Audio is unavailable in this stage.';
      feedback.className = 'feedback info';
      return Promise.resolve();
    }
    return speak(questionAudioText(currentQuestion));
  }

  async function cancelSession() {
    if (!sessionId || answering || speechPending > 0) return;
    if (drillActive) {
      feedback.textContent = 'Complete the mandatory drill before ending the session.';
      feedback.className = 'feedback info';
      focusCurrentAnswer();
      return;
    }
    answering = true;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    try {
      const data = await api('/api/practice/cancel', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId }),
      });
      showSummary(data.session || {
        practiced: 0, correct: 0, incorrect: [], drilled: 0,
        elapsed_seconds: 0, ended_early: true,
      });
    } catch (err) {
      answering = false;
      restoreInteractionAfterSpeech();
      showError(practiceError, err.message);
    }
  }

  // After a session ends, Enter goes back to setup.
  // On the setup card, Enter starts a session (unless focus is on a select).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    if (!document.getElementById('view-practice').classList.contains('active')) return;
    if (speechPending > 0 || answering) { e.preventDefault(); return; }

    if (summaryCard.style.display !== 'none') {
      e.preventDefault();
      document.getElementById('summary-restart').click();
      return;
    }
    // If setup card is showing and active element is not a select/textarea, start session.
    if (setupCard.style.display !== 'none' && !sessionId) {
      const tag = document.activeElement?.tagName;
      if (tag !== 'SELECT' && tag !== 'TEXTAREA') {
        e.preventDefault();
        startSession();
      }
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

  async function startSession(track = null) {
    showError(practiceError, '');
    const userInput = document.getElementById('practice-user');
    const posInput = document.getElementById('practice-pos');
    const fileInput = document.getElementById('practice-file');
    const user = userInput.value.trim();
    const lang = fileInput.value.trim();

    if (!user || !lang) {
      showError(practiceError, 'Select a user and a part of speech before entering the Consolidation Track.');
      if (!user) userInput.focus();
      else if (!lang) posInput.focus();
      return;
    }

    try {
      // Consolidation Track: backend determines mode. Only send essential
      // fields, plus 'track' when one of the supplementary practice
      // buttons (not the main blind-start button) was used.
      const body = track ? { user, lang, track } : { user, lang };

      const data = await api('/api/practice/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      sessionId = data.session_id;
      sessionLang = data.audio_lang || data.lang || '';
      sessionUser = user;
      sessionListId = data.lang || '';
      setupCard.style.display = 'none';
      if (supplementaryCard) supplementaryCard.style.display = 'none';
      if (practiceOverview) practiceOverview.style.display = 'none';
      const reportResults = document.getElementById('practice-report-results');
      if (reportResults) reportResults.innerHTML = '';
      summaryCard.style.display = 'none';
      sessionCard.style.display = 'block';
      renderQuestion(data.question, data.progress);
    } catch (err) {
      showError(practiceError, err.message);
    }
  }



  function renderQuestion(question, progress) {
    if (window.consolidationTimer) {
      clearTimeout(window.consolidationTimer);
      window.consolidationTimer = null;
    }
    resetAnswerCountdown();
    currentQuestion = question;
    drillActive = false;
    answering = false;
    setAnswerInputEnabled(false);
    setActionButtons(false);
    feedback.textContent = '';
    feedback.className = 'feedback';
    drillBlock.classList.remove('is-active');
    wordDisplay.style.display = '';
    answerInput.style.display = '';
    answerInput.value = '';

    const q = progress.questions ?? 0;
    const maxQ = progress.max_questions ?? progress.total ?? '?';
    const gMeta = question.consolidation || {};
    // Spaced Maintenance and the supplementary practice tracks are not a
    // day of the 10-day Consolidation Track (day 0 already means Encoding
    // elsewhere in this app) -- don't show a day fraction for them.
    const dayLabel = (gMeta.mode === 'spaced_maintenance' || SUPPLEMENTARY_PRACTICE_TYPES.includes(gMeta.mode))
      ? null
      : (Number(gMeta.day) >= 11 ? 'Complete' : `Day ${gMeta.day ?? 0}/10`);
    const progressParts = [gMeta.stage_name || 'Practice'];
    if (dayLabel) progressParts.push(dayLabel);
    progressParts.push(`Q${Math.min(q + 1, maxQ)}/${maxQ}`);
    sessionProgress.textContent = progressParts.join(' · ');
    sessionGauge.textContent = `${question.gauge || '●●●'} (score: ${formatScore(question)})`;
    sessionGauge.className = 'gauge band-consolidation';
    sessionType.textContent = TYPE_LABELS[gMeta.mode] || TYPE_LABELS[question.type] || question.type;
    // The supplementary tracks never drill -- a wrong answer just retries
    // the same question -- so there's nothing that can ever block ending
    // the session; the mandatory-drill caption only applies to the graded
    // Consolidation Track. visibility, not display: reserve the space
    // unconditionally so nothing around it ever shifts as sessions move
    // between track types.
    if (sessionControlNote) {
      sessionControlNote.classList.toggle('is-hidden', SUPPLEMENTARY_PRACTICE_TYPES.includes(question.type));
    }

    wordDisplay.className = `word-display answer-entry ${question.gender || ''}`;
    setAnswerSurface(question.word_unmasked || '', promptForQuestion(question));
    renderDefinitionPanel(question.definition || []);

    if (question.drill_start) {
      showDrill(question.drill_start);
      return;
    }

    // Response time scales with how much there is to type: 0.75s/character
    // normally, half that for the harder silent-recall stages (Reconsolidation,
    // Automaticity) that already ask for more from memory.
    const msPerChar = { free_recall: 750, reconsolidation: 500, automaticity: 500 }[question.type];
    const timerMs = msPerChar
      ? Math.round(Array.from(question.word_unmasked || '').length * msPerChar)
      : undefined;
    const timerSeconds = timerMs / 1000;
    const timerLabel = Number.isInteger(timerSeconds) ? String(timerSeconds) : timerSeconds.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
    answerInput.setAttribute('aria-label', timerMs ? `Type the full answer; ${timerLabel} second timer; press Enter to submit` : 'Type the full answer and press Enter to submit');
    if (timerMs) {
      // Starts the moment the question is shown, not after the prompt
      // audio finishes -- the response clock runs independently of
      // speech, not after it.
      window.consolidationTimer = setTimeout(() => {
        if (currentQuestion === question && !answerInteractionLocked()) sendTimeout();
      }, timerMs);
      startAnswerCountdown(timerMs);
    }
    const ready = () => {
      restoreInteractionAfterSpeech();
    };
    // Reading Retrieval deliberately stays silent while the question is
    // shown -- it has a definition to read, and the prompt audio plays
    // only after the learner submits an answer (see handleAnswerResult),
    // right or wrong. Listening Retrieval has no text stimulus at all, so
    // it always needs its audio immediately -- it follows the normal
    // automaticAudioAllowed() path below like every other question type.
    if (RETRIEVAL_DEFERRED_AUDIO_TYPES.includes(question.type)) {
      ready();
    } else if (automaticAudioAllowed(question.type)) {
      presentQuestionAudio(question, ready);
    } else {
      ready();
    }
  }

  const SUPPLEMENTARY_PRACTICE_TYPES = ['encoding_practice', 'retrieval_reading', 'retrieval_listening'];
  const RETRIEVAL_DEFERRED_AUDIO_TYPES = ['retrieval_reading'];

  const TIMER_DIM_OPACITY = 0.32;

  function timerPercentFor(ms) {
    const remainingMs = Math.max(0, (window.consolidationTimerDeadline || 0) - Date.now());
    return ms ? Math.round((remainingMs / ms) * 100) : 0;
  }

  // A hard response timer (Free Recall/Reconsolidation/Automaticity) previously had zero visible
  // feedback -- only a screen-reader aria-label update. This bar makes the
  // countdown itself visible; it purely mirrors window.consolidationTimer's own
  // lifecycle and never drives the actual timeout logic.
  function startAnswerCountdown(ms) {
    if (!answerTimerWrap || !answerTimerBar) return;
    answerTimerWrap.classList.add('is-active');
    answerTimerBar.style.transition = 'none';
    answerTimerBar.style.width = '100%';
    answerTimerBar.style.opacity = '1';
    void answerTimerBar.offsetWidth; // force reflow so the animation below actually starts from 100%
    // Width shrinks to show elapsed-time-as-percentage; opacity fades to a
    // dim value on the same clock, matching this app's existing dim/bright
    // language (e.g. .masked/.prompt) instead of an alarm-style color swap.
    answerTimerBar.style.transition = `width ${ms}ms linear, opacity ${ms}ms linear`;
    answerTimerBar.style.width = '0%';
    answerTimerBar.style.opacity = String(TIMER_DIM_OPACITY);
    window.consolidationTimerDeadline = Date.now() + ms;
    window.consolidationTimerDuration = ms;
    if (answerTimerLabel) {
      // Percentage of time remaining, not a literal second count -- the
      // absolute duration isn't the point, how much of it is left is.
      const tick = () => { answerTimerLabel.textContent = `${timerPercentFor(ms)}%`; };
      tick();
      if (window.consolidationTimerTick) clearInterval(window.consolidationTimerTick);
      window.consolidationTimerTick = setInterval(tick, 100);
    }
  }

  // Submitting an answer stops the countdown from continuing to run, but
  // the bar stays exactly where it was and stays visible -- it does not
  // vanish. Only a genuinely new question (renderQuestion) clears and
  // re-arms it; nothing should appear to disappear mid-question.
  function freezeAnswerCountdown() {
    if (window.consolidationTimerTick) {
      clearInterval(window.consolidationTimerTick);
      window.consolidationTimerTick = null;
    }
    if (!answerTimerWrap || !answerTimerBar) return;
    if (!answerTimerWrap.classList.contains('is-active')) return;
    const ms = window.consolidationTimerDuration;
    const percent = timerPercentFor(ms);
    answerTimerBar.style.transition = 'none';
    answerTimerBar.style.width = `${percent}%`;
    answerTimerBar.style.opacity = String(TIMER_DIM_OPACITY + (1 - TIMER_DIM_OPACITY) * (percent / 100));
    if (answerTimerLabel) answerTimerLabel.textContent = `${percent}%`;
  }

  // A genuinely new question (or a drill, or the session ending) clears the
  // timer back to hidden/idle, ready for the next startAnswerCountdown().
  function resetAnswerCountdown() {
    if (window.consolidationTimerTick) {
      clearInterval(window.consolidationTimerTick);
      window.consolidationTimerTick = null;
    }
    if (!answerTimerWrap) return;
    answerTimerWrap.classList.remove('is-active');
    if (answerTimerLabel) answerTimerLabel.textContent = '';
  }

  function setActionButtons(enabled) {
    const interactive = enabled && speechPending === 0 && !answering;
    btnReplay.disabled = !interactive || !replayAudioAllowed(currentQuestion?.type);
    btnEnd.disabled = !interactive;
  }

  function formatScore(question) {
    return Number(question.score).toFixed(1);
  }

  function submitTextAnswer() {
    if (!answerSubmitReady || answerInteractionLocked()) return;
    // An answer shorter than the target can't be right and isn't a
    // meaningful attempt -- don't let Enter send it. maxLength on the input
    // already keeps it from ever running long, so this only ever blocks the
    // "too short" side.
    if (Array.from(answerInput.value).length !== Array.from(answerTarget).length) return;
    sendAnswer(answerInput.value);
  }


  function newAttemptId() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }

  async function sendTimeout() {
    if (!sessionId || answering || speechPending > 0) return;
    answering = true;
    if (window.consolidationTimer) { clearTimeout(window.consolidationTimer); window.consolidationTimer = null; }
    freezeAnswerCountdown();
    setAnswerInputEnabled(false);
    setActionButtons(false);
    try {
      const data = await api('/api/practice/timeout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          question_id: currentQuestion?.question_id,
          sequence: currentQuestion?.sequence,
          attempt_id: newAttemptId(),
        }),
      });
      handleAnswerResult(data);
    } catch (err) {
      answering = false;
      setAnswerInputEnabled(true);
      setActionButtons(true);
      showError(practiceError, err.message);
    }
  }

  async function sendAnswer(answer) {
    if (!sessionId || answering || speechPending > 0) return;
    answering = true;
    // Stop counting down the moment an answer goes in, correct or not --
    // but stay visible, frozen where it was, rather than vanishing. Only
    // a genuinely new question clears and re-arms it (renderQuestion).
    if (window.consolidationTimer) { clearTimeout(window.consolidationTimer); window.consolidationTimer = null; }
    freezeAnswerCountdown();
    setAnswerInputEnabled(false);
    setActionButtons(false);
    try {
      const data = await api('/api/practice/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId, answer,
          question_id: currentQuestion?.question_id, sequence: currentQuestion?.sequence,
          attempt_id: newAttemptId(),
        }),
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
    if (data.result === 'retry') {
      // All three supplementary tracks: wrong answer, no drill, same
      // question stays -- unlimited retries until it's actually typed
      // correctly, since none of them are mandatory practice.
      feedback.textContent = data.message || 'Not quite. Try again.';
      feedback.className = 'feedback incorrect';
      answerInput.value = '';
      // Reading/Listening Retrieval's first miss reveals the word (see
      // process_bucket_answer): blind guessing after a miss isn't
      // productive, so switch this question to the same fully-visible,
      // both-definitions presentation Encoding Practice already uses --
      // any further attempt is then a guaranteed-achievable copy.
      if (data.reveal && currentQuestion) {
        currentQuestion.word = data.reveal.word;
        currentQuestion.word_unmasked = data.reveal.word;
        currentQuestion.definition = data.reveal.definition;
        currentQuestion.text_hidden = false;
        // Bypass promptForQuestion() deliberately: it forces full masking
        // for question.type 'retrieval_reading'/'retrieval_listening'
        // regardless of question.word, which is exactly what a reveal
        // needs to override -- target and prompt are the same fully
        // visible word here, on purpose.
        setAnswerSurface(currentQuestion.word_unmasked, currentQuestion.word_unmasked);
        renderDefinitionPanel(currentQuestion.definition);
      } else {
        renderAnswerSurface();
      }
      const afterRetryFeedback = () => {
        answering = false;
        restoreInteractionAfterSpeech();
      };
      // Reading Retrieval stays silent on question-show (see renderQuestion)
      // and only speaks after each answer, right or wrong -- a retry is
      // still an answer. Encoding Practice/Listening Retrieval already
      // spoke once when the question rendered; no need to repeat it on
      // every retry attempt.
      if (RETRIEVAL_DEFERRED_AUDIO_TYPES.includes(currentQuestion?.type)) {
        // The question doesn't change on a retry, so -- same as
        // presentQuestionAudio() at question-render time -- typing is
        // allowed straight through the confirmation audio instead of
        // waiting for it to finish; only submission stays locked
        // (setAnswerInputEnabled's allowSubmit=false, and sendAnswer()'s
        // own speechPending guard) until speech completes.
        answering = false;
        setAnswerInputEnabled(true, false);
        focusCurrentAnswer();
        speak(questionAudioText(currentQuestion)).then(afterRetryFeedback);
      } else {
        afterRetryFeedback();
      }
      return;
    }

    if (data.result === 'drill_start' || data.result === 'drill_progress') {
      answering = false;
      showDrill(data.drill);
      return;
    }

    if (data.result === 'drilled' && data.drill) {
      showDrill(data.drill, false);
      answering = true;
      setAnswerInputEnabled(false);
      const afterDrillFeedback = () => setTimeout(() => {
        if (data.done) { showSummary(data.session); return; }
        answering = false;
        setActionButtons(true);
        renderQuestion(data.question, data.progress);
      }, 700);
      if (automaticAudioAllowed(currentQuestion?.type)) {
        speak(questionAudioText(currentQuestion)).then(afterDrillFeedback);
      } else {
        afterDrillFeedback();
      }
      return;
    }





    if (data.result === 'correct') {
      feedback.textContent = `Correct! '${data.word}'`;
      feedback.className = 'feedback correct';
    } else if (data.result === 'incorrect') {
      feedback.textContent = data.message;
      feedback.className = 'feedback incorrect';
    } else if (data.result === 'drilled') {
      feedback.textContent = data.message || 'Drill complete.';
      feedback.className = 'feedback info';
    } else if (data.result === 'end') {
      feedback.textContent = 'Session ended.';
      feedback.className = 'feedback info';
    }

    // Feedback is already shown above. Reconsolidation/Automaticity intentionally stay
    // silent; other modes can speak feedback before advancing.
    const audioOn = automaticAudioAllowed(currentQuestion?.type);
    const advance = () => {
      if (data.done) { showSummary(data.session); return; }
      setActionButtons(true);
      renderQuestion(data.question, data.progress);
    };

    if ((data.result === 'correct' || data.result === 'incorrect') && audioOn) {
      speak(questionAudioText(currentQuestion)).then(advance);
    } else {
      setTimeout(advance, 700);
    }
  }

  function showDrill(drill, playAudio = true) {
    if (window.consolidationTimer) {
      clearTimeout(window.consolidationTimer);
      window.consolidationTimer = null;
    }
    resetAnswerCountdown();
    drillActive = true;
    drillBlock.classList.add('is-active');
    setActionButtons(false);
    // Effortful Retrieval's own 2-production check-in preloads this same drill UI before
    // any mistake happens, so it keeps its stage label; every other path
    // into showDrill() is a real corrective drill triggered by a wrong
    // answer, in this question or a resumed one -- labeled consistently
    // regardless of which of those triggered it.
    const mode = currentQuestion?.consolidation?.mode;
    sessionType.textContent = mode === 'effortful_retrieval' ? TYPE_LABELS.effortful_retrieval : 'Mandatory Drill';

    const drillTarget = String(drill.word || currentQuestion?.word_unmasked || '');
    const drillDefinition = Array.isArray(drill.definition)
      ? drill.definition
      : (currentQuestion?.definition || []);
    const drillComplete = drill.correct === true && Number(drill.correct_in_a_row) >= Number(drill.target);
    answerInput.value = drillComplete ? drillTarget : '';
    setAnswerSurface(drillTarget, drill.show_word === false ? fullyMaskedTarget(drillTarget) : drillTarget);
    renderDefinitionPanel(drillDefinition);

    drillRep.textContent = drill.repetition;
    drillStreak.textContent = drill.correct_in_a_row;
    if (drillTargetLabel) drillTargetLabel.textContent = drill.target;
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

    setAnswerInputEnabled(!drillComplete);
    renderAnswerSurface();
    if (playAudio && automaticAudioAllowed(currentQuestion?.type)) {
      presentQuestionAudio(currentQuestion);
    } else {
      restoreInteractionAfterSpeech();
      focusCurrentAnswer();
    }
  }

  function showSummary(session) {
    answering = false;
    drillActive = false;
    if (window.consolidationTimer) {
      clearTimeout(window.consolidationTimer);
      window.consolidationTimer = null;
    }
    resetAnswerCountdown();
    setAnswerInputEnabled(false);
    answerTarget = ''; answerPrompt = ''; answerInput.value = ''; renderAnswerSurface();
    sessionCard.style.display = 'none';
    summaryCard.style.display = 'block';
    sessionId = null;
    currentQuestion = null;

    const minutes = Math.floor((session.elapsed_seconds || 0) / 60);
    const seconds = (session.elapsed_seconds || 0) % 60;
    let html = '<ul class="summary-list">';
    html += `<li>Words practiced: <strong>${session.practiced || 0}</strong></li>`;
    html += `<li>Correct answers: <strong>${session.correct || 0}</strong></li>`;
    html += `<li>Incorrect answers: <strong>${(session.incorrect || []).length}</strong></li>`;
    html += `<li>Words drilled: <strong>${session.drilled || 0}</strong></li>`;
    html += `<li>Session time: <strong>${minutes}m ${seconds}s</strong></li>`;
    html += '</ul>';
    if ((session.incorrect || []).length) {
      html += '<h3>Incorrect answers</h3><ul class="summary-list">';
      session.incorrect.forEach((item) => {
        html += `<li>You wrote '<strong>${escapeHtml(item.attempt)}</strong>'; target: '<strong>${escapeHtml(item.word)}</strong>'</li>`;
      });
      html += '</ul>';
    }
    document.getElementById('summary-body').innerHTML = html;
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    // Keep this screen to the two things worth seeing right after a session:
    // the summary above, and the compact day/stage status below. The full
    // report is for "Back to setup", not a second helping right after finishing.
    fetchConsolidationStatus(user, lang);
  }

  // --- Live progress report (merged into Practice setup) ---
  // Fires on every step of the same cascade that starts a session -- no
  // separate view, no "load" click. No file selected shows the full/total
  // report; a resolved file shows that file's focused report.
  //
  // The listener is attached to every field in the cascade (see below), and
  // populateSelect() auto-dispatches 'change' on a field that resolves to
  // exactly one option -- so a single pick can fire this several times in a
  // rapid burst as the cascade auto-resolves down to one file. Without
  // coordination, those overlapping async calls interleave and each append
  // their own copy of the same cards. reportRequestToken makes only the
  // most recently started call allowed to touch the DOM; every earlier one
  // notices it's been superseded and quietly abandons itself.
  let reportRequestToken = 0;

  async function refreshPracticeReport() {
    const myToken = ++reportRequestToken;
    const reportError = document.getElementById('practice-report-error');
    const resultsEl = document.getElementById('practice-report-results');
    const stale = () => myToken !== reportRequestToken;
    showError(reportError, '');
    resultsEl.innerHTML = '';
    const user = document.getElementById('practice-user').value.trim();
    const category = document.getElementById('practice-lang').value.trim();
    const level = document.getElementById('practice-level').value.trim();
    const pos = document.getElementById('practice-pos').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    if (!user) return; // nothing picked yet -- quietly show nothing, this isn't a button click
    if (!lang && (category || level || pos)) {
      showError(reportError, 'Choose a part of speech to see a focused report, or clear the filters for the full report.');
      return;
    }
    try {
      const params = new URLSearchParams({ user });
      if (lang) params.set('lang', lang);

      if (!lang) {
        const summaryData = await api(`/api/report/summary?user=${encodeURIComponent(user)}`);
        if (stale()) return;
        if (summaryData.summary) resultsEl.appendChild(renderUserSummaryCard(summaryData.summary));
      }

      const [data, masterySeries, box10Series] = await Promise.all([
        api(`/api/report?${params.toString()}`),
        lang ? fetchTrend(user, lang, 'mastered') : Promise.resolve([]),
        lang ? fetchTrend(user, lang, 'box10') : Promise.resolve([]),
      ]);
      if (stale()) return;

      if (data.roadmap) {
        data.roadmap.mastery_series = masterySeries;
        resultsEl.appendChild(renderRoadmapCard(data.roadmap));
      }

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
        if (stale()) return;
        const secHeader = document.createElement('div');
        secHeader.className = 'dash-section-header';
        secHeader.innerHTML = '<h2>Analytics</h2>';
        resultsEl.appendChild(secHeader);
        resultsEl.appendChild(renderDashCard1(dash.overview));
        const g1 = document.createElement('div');
        g1.className = 'dashboard-grid';
        if (dash.tracks) g1.appendChild(renderTrackProgressCard(dash.tracks, masterySeries, box10Series));
        g1.appendChild(renderPracticePaceCard(dash.velocity));
        resultsEl.appendChild(g1);
        if (dash.nemesis !== null) resultsEl.appendChild(renderMistakeHistoryCard(dash.nemesis));
        } catch (error) {
          if (!stale()) appendReportWarning(resultsEl, `Analytics unavailable: ${error.message}`);
        }
        if (stale()) return;
        await loadWordListStats(user, lang, resultsEl, stale);
      }
    } catch (err) {
      if (!stale()) showError(reportError, err.message);
    }
  }

  // --- Today tab: per-file, per-mode breakdown of what a user practiced today ---
  let todayRequestToken = 0;

  async function refreshTodayOverview() {
    const myToken = ++todayRequestToken;
    const stale = () => myToken !== todayRequestToken;
    const errorEl = document.getElementById('today-error');
    const dueEl = document.getElementById('today-due-results');
    const resultsEl = document.getElementById('today-results');
    showError(errorEl, '');
    dueEl.innerHTML = '';
    resultsEl.innerHTML = '';
    const user = document.getElementById('today-user').value.trim();
    if (!user) return;
    try {
      const [practiced, progress] = await Promise.all([
        api(`/api/report/today?user=${encodeURIComponent(user)}`),
        api(`/api/user/progress?user=${encodeURIComponent(user)}`),
      ]);
      if (stale()) return;
      dueEl.appendChild(renderDueTodayTable(user, progress.lists || []));
      resultsEl.appendChild(renderTodayTable(user, practiced.entries || []));
    } catch (err) {
      if (!stale()) showError(errorEl, err.message);
    }
  }

  // The forward-looking plan, not a log of what already happened -- but
  // only for files that actually have something to do. A file with
  // nothing due and nothing left in Encoding is fully caught up right now,
  // so it's excluded rather than padding the list with an all-zero row.
  function renderDueTodayTable(user, lists) {
    const withDue = lists.filter((item) => (
      item.consolidation.due_reinforcement + item.consolidation.due_maintenance + item.consolidation.encoding > 0
    ));
    if (!withDue.length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = 'Nothing due today -- every file is caught up.';
      return empty;
    }
    const sorted = [...withDue].sort((a, b) => {
      const dueA = a.consolidation.due_reinforcement + a.consolidation.due_maintenance + a.consolidation.encoding;
      const dueB = b.consolidation.due_reinforcement + b.consolidation.due_maintenance + b.consolidation.encoding;
      return dueB - dueA || a.lang.localeCompare(b.lang);
    });
    const wrap = document.createElement('div');
    let html = '<table><thead><tr><th>File</th><th>Reinforcement Due</th>'
      + '<th>Maintenance Due</th><th>Encoding Available</th><th></th></tr></thead><tbody>';
    sorted.forEach((item, index) => {
      const c = item.consolidation;
      html += `<tr><td>${escapeHtml(item.lang)}</td><td>${c.due_reinforcement}</td>`
        + `<td>${c.due_maintenance}</td><td>${c.encoding}</td>`
        + `<td><button type="button" class="secondary today-jump-btn" data-index="${index}">Practice &rarr;</button></td></tr>`;
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;
    wrap.querySelectorAll('.today-jump-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        jumpToPractice(user, sorted[Number(btn.dataset.index)].lang);
      });
    });
    return wrap;
  }

  function renderTodayTable(user, entries) {
    if (!entries.length) {
      const empty = document.createElement('p');
      empty.className = 'muted';
      empty.textContent = 'No practice recorded today for this user yet.';
      return empty;
    }
    const wrap = document.createElement('div');
    let html = '<table><thead><tr><th>File</th><th>Mode</th><th>Completed</th>'
      + '<th>Accuracy</th><th>Time</th><th></th></tr></thead><tbody>';
    entries.forEach((entry, index) => {
      const minutes = Math.floor(entry.seconds / 60);
      const seconds = entry.seconds % 60;
      const accuracy = entry.accuracy != null ? `${entry.accuracy}%` : 'N/A';
      html += `<tr><td>${escapeHtml(entry.language)}</td><td>${escapeHtml(entry.mode_name)}</td>`
        + `<td>${entry.practiced}</td><td>${accuracy}</td><td>${minutes}m ${seconds}s</td>`
        + `<td><button type="button" class="secondary today-jump-btn" data-index="${index}">Practice &rarr;</button></td></tr>`;
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;
    wrap.querySelectorAll('.today-jump-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        jumpToPractice(user, entries[Number(btn.dataset.index)].language);
      });
    });
    return wrap;
  }

  function jumpToPractice(user, language) {
    const meta = allWordLists.find((w) => w.user === user && w.lang === language);
    if (!meta) {
      switchView('practice');
      return;
    }
    const steps = [
      ['practice-user', user],
      ['practice-lang', meta.category],
      ['practice-level', meta.cefr_level],
      ['practice-pos', meta.pos],
      ['practice-file', meta.lang],
    ];
    steps.forEach(([id, value]) => {
      const sel = document.getElementById(id);
      sel.value = value;
      sel.dispatchEvent(new Event('change'));
    });
    switchView('practice');
  }

  function renderDailyChart(days) {
    if (!days || days.length === 0) return '';
    // Oldest-to-newest for left→right, cap at 60 days
    const chartDays = [...days].reverse().slice(-60);
    const series = chartDays.map((day) => ({ date: day.date, cumulative: day.practiced }));
    return `<div class="daily-chart-wrap">
      <div class="daily-chart-label muted">Words practiced per day (last ${chartDays.length} day${chartDays.length !== 1 ? 's' : ''})</div>
      ${renderTrendChart(series, { label: 'Words practiced per day' })}
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

  function appendReportWarning(container, message) {
    const warning = document.createElement('p');
    warning.className = 'muted report-warning';
    warning.textContent = message;
    container.appendChild(warning);
  }

  async function loadWordListStats(user, lang, container, stale = () => false) {
    const params = new URLSearchParams({ user, lang });
    try {
      const leitnerData = await api(`/api/wordlist/leitner?${params.toString()}`);
      if (stale()) return;
      if (leitnerData.leitner) container.appendChild(renderLeitnerCard(lang, leitnerData.leitner));
    } catch (error) {
      if (!stale()) appendReportWarning(container, `Leitner details unavailable: ${error.message}`);
    }
    try {
      const data = await api(`/api/wordlist/stats?${params.toString()}`);
      if (stale()) return;
      if (data.words.length) container.appendChild(renderWordStatsTable(lang, data.words, 'Full Word List'));
    } catch (error) {
      if (!stale()) appendReportWarning(container, `Word-list details unavailable: ${error.message}`);
      return;
    }
  }

  const CONSOLIDATION_STAGE_NAMES = {
    encoding: 'Encoding', cued_recall: 'Cued Recall', effortful_retrieval: 'Effortful Retrieval',
    free_recall: 'Free Recall', reconsolidation: 'Reconsolidation', automaticity: 'Automaticity',
  };

  function formatWordStage(state, day) {
    if (state === 'long_term_review') return 'Long-term review';
    const name = CONSOLIDATION_STAGE_NAMES[state] || state;
    return day ? `${name} · Day ${day}` : name;
  }

  function renderWordStatsTable(lang, words, caption) {
    const card = document.createElement('div');
    card.className = 'card';
    let html = `<table><caption>${escapeHtml(caption || `Word list: ${lang}`)}</caption>`;
    html += '<thead><tr><th>Word</th><th>Score</th><th>Gauge</th><th>Stage</th><th>Box</th><th>Maintenance</th>'
      + '<th>Practiced</th><th>Correct</th><th>Wrong</th><th>Drilled</th><th>Last activity</th></tr></thead><tbody>';
    words.forEach((w) => {
      const maintenance = w.leitner_box == null ? '—' : (w.maintenance_ready ? 'Ready' : (w.next_maintenance || '—'));
      html += `<tr><td>${escapeHtml(w.word)}</td>`
        + `<td>${w.score.toFixed(1)}</td><td class="gauge band-${w.gauge_band}">${w.gauge}</td>`
        + `<td>${escapeHtml(formatWordStage(w.consolidation_state, w.consolidation_day))}</td>`
        + `<td>${w.leitner_box ?? '—'}</td><td>${escapeHtml(maintenance)}</td>`
        + `<td>${w.times_practiced}</td><td>${w.times_correct}</td><td>${w.times_incorrect}</td>`
        + `<td>${w.times_drilled}</td><td>${formatDateTime(w.last_practiced)}</td></tr>`;
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

  function renderTrendChart(series, { compact = false, label = 'Cumulative progress over time' } = {}) {
    const points = (Array.isArray(series) ? series : [])
      .map((item) => ({ date: String(item.date || ''), value: Number(item.cumulative || 0) }))
      .filter((item) => item.date && Number.isFinite(item.value) && item.value >= 0);
    const width = compact ? 320 : 640;
    const height = compact ? 72 : 160;
    const left = compact ? 6 : 34;
    const right = compact ? 6 : 12;
    const top = compact ? 8 : 18;
    const bottom = compact ? 8 : 28;
    const base = height - bottom;
    const plotWidth = width - left - right;
    const plotHeight = base - top;
    const max = Math.max(1, ...points.map((item) => item.value));
    const coordinates = points.map((item, index) => {
      const x = points.length === 1 ? left + plotWidth / 2 : left + (plotWidth * index / (points.length - 1));
      const y = base - (plotHeight * item.value / max);
      return { ...item, x, y };
    });
    const line = coordinates.map((item, index) => `${index ? 'L' : 'M'}${item.x.toFixed(1)},${item.y.toFixed(1)}`).join(' ');
    const area = coordinates.length
      ? `M${coordinates[0].x.toFixed(1)},${base} ${coordinates.map((item) => `L${item.x.toFixed(1)},${item.y.toFixed(1)}`).join(' ')} L${coordinates[coordinates.length - 1].x.toFixed(1)},${base} Z`
      : '';
    const empty = coordinates.length === 0;
    const axisLabels = compact || empty ? '' : `
      <text class="trend-axis-label" x="${left}" y="${height - 7}">${escapeHtml(coordinates[0].date)}</text>
      <text class="trend-axis-label" x="${width - right}" y="${height - 7}" text-anchor="end">${escapeHtml(coordinates[coordinates.length - 1].date)}</text>`;
    const marks = coordinates.map((item) => `<circle class="trend-point" cx="${item.x.toFixed(1)}" cy="${item.y.toFixed(1)}" r="${compact ? 2 : 3}"><title>${escapeHtml(`${item.date}: ${item.value}`)}</title></circle>`).join('');
    return `<div class="trend-chart-wrap${compact ? ' trend-chart-compact' : ''}">
      <svg class="trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}">
        <title>${escapeHtml(label)}</title>
        <line class="trend-axis" x1="${left}" y1="${base}" x2="${width - right}" y2="${base}"/>
        ${area ? `<path class="trend-area" d="${area}"/><path class="trend-line" d="${line}"/>${marks}` : ''}
        ${axisLabels}
      </svg>
      ${empty ? '<span class="trend-empty">Milestone history starts with newly recorded progress.</span>' : ''}
    </div>`;
  }

  function renderRoadmapCard(roadmap) {
    const card = document.createElement('div');
    card.className = 'card roadmap-card';
    const consolidation = roadmap.consolidation || {};
    const stageCounts = new Map(
      (consolidation.reinforcement_stages || []).map((item) => [Number(item.stage), Number(item.count || 0)])
    );
    const stages = [
      { id: 0, name: 'Encoding', days: 'Day 0', count: Number(consolidation.encoding || 0) },
      { id: 1, name: 'Cued Recall', days: 'Days 1-2', count: stageCounts.get(1) || 0 },
      { id: 2, name: 'Effortful Retrieval', days: 'Days 3-4', count: stageCounts.get(2) || 0 },
      { id: 3, name: 'Free Recall', days: 'Days 5-6', count: stageCounts.get(3) || 0 },
      { id: 4, name: 'Reconsolidation', days: 'Days 7-8', count: stageCounts.get(4) || 0 },
      { id: 5, name: 'Automaticity', days: 'Days 9-10', count: stageCounts.get(5) || 0 },
    ];

    let consolidationHtml = `<div class="roadmap-section">
      <h3>The per-word 10-Day Consolidation Track</h3>
      <p class="muted">Each mastered word advances by its own mastery date; cohorts can occupy several stages at once.</p>
      <div class="roadmap-timeline">`;

    stages.forEach((stage) => {
      const statusClass = stage.count > 0
        ? 'active'
        : (stage.id === 0 && consolidation.mastered_total ? 'completed' : 'locked');
      const countLabel = `${stage.count} word${stage.count === 1 ? '' : 's'} · ${stage.days}`;
      consolidationHtml += `
        <div class="timeline-node ${statusClass}">
          <div class="node-circle">${stage.id}</div>
          <div class="node-info">
            <div class="node-name">${escapeHtml(stage.name)}</div>
            <div class="node-days">${escapeHtml(countLabel)}</div>
          </div>
        </div>
      `;
    });
    consolidationHtml += `</div>`;

    if (consolidation.total_tasks) {
      const stats = `<span class="stage-progress-stats">${consolidation.encoding || 0} Encoding · ${consolidation.reinforcement_total || 0} ten-day track · ${consolidation.long_term_review || 0} long-term</span>`;
      consolidationHtml += `
        <div class="roadmap-stage-progress-wrap">
          <div class="stage-progress-header">
            <span class="stage-progress-title">Mastery over time</span>
            ${stats}
          </div>
          ${renderTrendChart(roadmap.mastery_series, { label: 'Cumulative words mastered by day' })}
        </div>
      `;
    }
    consolidationHtml += `</div>`;

    const leitnerBoxes = [];
    for (let i = 1; i <= 10; i++) {
      leitnerBoxes.push({
        box: i,
        count: roadmap.leitner_distribution[i] || 0,
      });
    }
    const leitnerHtml = `<div class="roadmap-section leitner-section">
      <h3>Lifetime Spaced Maintenance</h3>
      <p class="muted">The maintenance distribution of score-9 items (Box 1 = 1 day, Box 10 = 10 days). ${roadmap.maintenance_ready || 0} ready now.</p>
      ${renderLeitnerRoadmap(leitnerBoxes)}
    </div>`;

    card.innerHTML = consolidationHtml + leitnerHtml;
    return card;
  }


  function renderLeitnerRoadmap(boxes, { showIntervals = false } = {}) {
    let html = '<div class="leitner-roadmap-scroll"><div class="leitner-roadmap-track">';
    for (const box of boxes) {
      const b = Number(box.box || 0);
      const total = Number(box.count ?? box.total ?? 0);
      const stateClass = total > 0 ? 'has-words' : 'empty';
      html += `
        <div class="leitner-roadmap-node ${stateClass}">
          <div class="leitner-roadmap-square" aria-label="Leitner Box ${b}"><span class="leitner-roadmap-number">${b}</span></div>
          <div class="leitner-roadmap-info">
            <div class="leitner-roadmap-name">Box ${b}</div>
            <div class="leitner-roadmap-count">${total} word${total === 1 ? '' : 's'}</div>
            ${showIntervals ? `<div class="leitner-roadmap-interval">${b} day${b === 1 ? '' : 's'}</div>` : ''}
          </div>
        </div>`;
    }
    html += '</div></div>';
    return html;
  }

  // --- Word lists + cascading dropdowns ---

  var allWordLists = [];

  // Editor cascade: user -> category -> level -> pos -> file
  function setupEditorCascade() {
    createCascade(
      ['editor-user', 'editor-category', 'editor-level', 'editor-pos', 'editor-lang'],
      (user, category, level, pos) => {
        if (!user) return [{value: '', label: 'Select word list…', disabled: true}];
        if (category === undefined) {
          return PRACTICE_CATEGORIES.map(([value, label]) => {
            const count = allWordLists.filter(w => w.user === user && w.category === value).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value, label: count ? `${label} (${formatCount(count)})` : `${label} (no files)`, disabled: false};
          });
        }
        if (level === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category);
          const levels = [...new Set(matches.map(w => w.cefr_level))].sort();
          return levels.map(val => {
            const count = matches.filter(w => w.cefr_level === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        if (pos === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category && w.cefr_level === level);
          const poses = [...new Set(matches.map(w => w.pos))].sort();
          return poses.map(val => {
            const count = matches.filter(w => w.pos === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a,b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${formatCount(w.word_count)}) ${w.lang}`}));
      }
    );
  }


  function renderLeitnerCard(lang, stats) {
    const card = document.createElement('div');
    card.className = 'card';
    const boxes = Object.entries(stats.distribution || {}).map(([box, count]) => ({ box: Number(box), count, interval_days: Number(box) }));
    const total = boxes.reduce((sum, item) => sum + Number(item.count || 0), 0);
    card.innerHTML = `<h3>Lifetime Spaced Maintenance &mdash; ${escapeHtml(lang)}</h3>`
      + `<p class="muted">${total} items in maintenance · ${stats.box10 || 0} at Box 10 · ${stats.ready || 0} ready now</p>`
      + renderLeitnerRoadmap(boxes, { showIntervals: true });
    return card;
  }

  const PRACTICE_CATEGORIES = [
    ['english_vocabulary', 'English vocabulary'],
    ['english_sentences', 'English sentences'],
    ['german_vocabulary', 'German vocabulary'],
    ['german_sentences', 'German sentences'],
  ];

  // Generic cascade: given a chain of select IDs and a filter function,
  // populate each select based on the previous one's value.
  function createCascade(selectIds, getOptions) {
    const selects = selectIds.map(id => document.getElementById(id));
    selects.forEach((sel, i) => {
      if (i === selects.length - 1) return;
      sel.addEventListener('change', () => {
        for (let child = i + 1; child < selects.length; child += 1) {
          const vals = selects.slice(0, child).map(s => s.value);
          populateSelect(selects[child], getOptions(...vals), '');
        }
      });
    });
  }

  function populateSelect(select, options, selectedValue = '') {
    const previousValue = select.value;
    select.innerHTML = '<option value="">' + (select.dataset.placeholder || 'Select…') + '</option>';
    options.forEach(opt => {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      o.disabled = opt.disabled;
      if (opt.value === selectedValue) o.selected = true;
      select.appendChild(o);
    });
    // When a step resolves to exactly one real choice, there is nothing left
    // for the user to decide -- select it automatically instead of forcing a
    // click through a dropdown with one option. Only dispatch 'change' when
    // this actually moves the value, so cascades settle without duplicate
    // downstream fetches.
    if (!selectedValue) {
      const enabled = options.filter(opt => !opt.disabled && opt.value !== '');
      if (enabled.length === 1) {
        select.value = enabled[0].value;
        if (select.value !== previousValue) select.dispatchEvent(new Event('change'));
      }
    }
  }

  // Practice cascade: user -> category -> level -> pos -> file
  function setupPracticeCascade() {
    createCascade(
      ['practice-user', 'practice-lang', 'practice-level', 'practice-pos', 'practice-file'],
      (user, category, level, pos) => {
        if (!user) return [{value: '', label: 'Select language…', disabled: true}];
        if (category === undefined) {
          return PRACTICE_CATEGORIES.map(([value, label]) => {
            const count = allWordLists.filter(w => w.user === user && w.category === value).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value, label: count ? `${label} (${formatCount(count)})` : `${label} (no files)`, disabled: false};
          });
        }
        if (level === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category);
          const levels = [...new Set(matches.map(w => w.cefr_level))].sort();
          return levels.map(val => {
            const count = matches.filter(w => w.cefr_level === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        if (pos === undefined) {
          const matches = allWordLists.filter(w => w.user === user && w.category === category && w.cefr_level === level);
          const poses = [...new Set(matches.map(w => w.pos))].sort();
          return poses.map(val => {
            const count = matches.filter(w => w.pos === val).reduce((sum, w) => sum + (w.word_count || 0), 0);
            return {value: val, label: `${val ? val.toUpperCase() : 'ALL'} (${formatCount(count)})`, disabled: false};
          });
        }
        return allWordLists
          .filter(w => w.user === user && w.category === category && w.cefr_level === level && w.pos === pos)
          .sort((a,b) => a.lang.localeCompare(b.lang))
          .map(w => ({value: w.lang, label: `(${formatCount(w.word_count)}) ${w.lang}`}));
      }
    );
  }

  setupEditorCascade();
  setupPracticeCascade();
  document.getElementById('practice-file').addEventListener('change', () => {
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    fetchConsolidationStatus(user, lang);
  });
  document.getElementById('practice-user').addEventListener('change', () => {
    const user = document.getElementById('practice-user').value.trim();
    const lang = document.getElementById('practice-file').value.trim();
    fetchConsolidationStatus(user, lang);
  });
  // The live report responds to every step of the same cascade, not just
  // the leaf -- narrowing or widening the filters must update it too.
  ['practice-user', 'practice-lang', 'practice-level', 'practice-pos', 'practice-file'].forEach((id) => {
    document.getElementById(id).addEventListener('change', refreshPracticeReport);
  });
  document.getElementById('today-user').addEventListener('change', refreshTodayOverview);


  async function loadWordLists() {
    let apiUsers = [];
    try {
      const data = await api('/api/wordlists');
      allWordLists = data.wordlists || [];
      apiUsers = data.users || [];
    } catch (err) {
      console.error('Failed to load word lists:', err);
      allWordLists = [];
    }

    // Always refresh dropdowns, even if API failed (will use cached/empty data).
    // Populate user dropdowns
    const users = apiUsers.length ? apiUsers : [...new Set(allWordLists.map(w => w.user))].sort();
    ['practice-user', 'editor-user', 'today-user'].forEach(id => {
      const sel = document.getElementById(id);
      if (sel) {
        let prev = sel.value;
        if (!prev && users.length === 1) prev = users[0];
        sel.innerHTML = '<option value="">Select user…</option>' + users.map(u => `<option value="${u}"${u === prev ? ' selected' : ''}>${u}</option>`).join('');
        if (prev) sel.value = prev;
      }
    });
    // Populate all dependent selects after the word-list data is available.
    document.getElementById('practice-user')?.dispatchEvent(new Event('change'));
    document.getElementById('editor-user')?.dispatchEvent(new Event('change'));
    document.getElementById('today-user')?.dispatchEvent(new Event('change'));
  }

  // Load word lists immediately so dropdowns are populated on first page load.
  loadWordLists();

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

  // --- Dashboard card renderers (used inside refreshPracticeReport) ---

  // Generic card factory: creates a card with optional header and body
  function createCard(className, title, bodyHtml, extraClass = '') {
    const card = document.createElement('div');
    card.className = `card ${className} ${extraClass}`.trim();
    if (title) {
      const h3 = document.createElement('h3');
      h3.textContent = title;
      card.appendChild(h3);
    }
    if (typeof bodyHtml === 'string') {
      card.insertAdjacentHTML('beforeend', bodyHtml);
    } else if (bodyHtml instanceof Node) {
      card.appendChild(bodyHtml);
    }
    return card;
  }

  // Stat tile helper
  function statTile(num, label, extraClass = '', unit = '') {
    return `<div class="stat-tile ${extraClass}"><span class="stat-num">${num}${unit ? `<span class="stat-unit">${unit}</span>` : ''}</span><span class="stat-label">${label}</span></div>`;
  }

  // Factual dashboard cards only — no debt badges, predictions, or psychological labels.
  function renderDashCard1(overview) {
    const h = Math.floor((overview.total_seconds || 0) / 3600);
    const m = Math.floor(((overview.total_seconds || 0) % 3600) / 60);
    const accuracy = overview.overall_accuracy;
    const r = 38, circ = +(2 * Math.PI * r).toFixed(1);
    const filled = accuracy != null ? +(circ * accuracy / 100).toFixed(1) : 0;
    const arcColor = accuracy == null ? 'var(--surface1)' : accuracy >= 85 ? 'var(--green)' : accuracy >= 70 ? 'var(--yellow)' : 'var(--red)';
    const ringLabel = accuracy != null ? `${accuracy}%` : 'N/A';
    return createCard('dash-card-full dash-card-overview', 'Current Status', `
      <div class="stat-tiles">
        ${statTile(`${overview.streak.current}<span class="stat-unit">day${overview.streak.current !== 1 ? 's' : ''}</span>`, 'Current Streak')}
        ${statTile(`${h}h ${m}m`, 'Total Practice Time')}
        <div class="stat-tile stat-ring-tile"><svg width="90" height="90" viewBox="0 0 90 90" class="accuracy-ring">
          <circle cx="45" cy="45" r="${r}" fill="none" stroke="var(--surface1)" stroke-width="9"/>
          <circle cx="45" cy="45" r="${r}" fill="none" stroke="${arcColor}" stroke-width="9" stroke-dasharray="${filled} ${circ-filled}" stroke-linecap="round" transform="rotate(-90 45 45)"/>
          <text x="45" y="45" text-anchor="middle" dominant-baseline="middle" fill="${arcColor}" font-size="14" font-weight="700">${ringLabel}</text></svg>
          <span class="stat-label">Overall Accuracy</span></div>
      </div>`);
  }

  function renderTrackProgressCard(tracks, masterySeries = [], box10Series = []) {
    const total = tracks.total || 0;
    const tPct = total ? Math.round(1000 * tracks.consolidation_score9 / total) / 10 : 0;
    const lPct = total ? Math.round(1000 * tracks.leitner_box10 / total) / 10 : 0;
    return createCard('dash-card-tracks', 'Learning Tracks', `
      <div class="track-metric"><strong>Mastered (score 9)</strong><span>${tPct.toFixed(1)}%</span></div>
      ${renderTrendChart(masterySeries, { label: 'Cumulative Tartarus mastery by day' })}
      <div class="track-metric"><strong>Leitner Box 10</strong><span>${lPct.toFixed(1)}%</span></div>
      ${renderTrendChart(box10Series, { label: 'Cumulative Leitner Box 10 milestones by day' })}
      <p class="muted">Consolidation Track: <strong>${tracks.consolidation_track_complete ? 'complete' : 'in progress'}</strong> · Learning path: <strong>${tracks.learning_complete ? 'complete' : 'in progress'}</strong></p>`);
  }

  function renderMistakeHistoryCard(words) {
    if (!words.length) return createCard('dash-card-nemesis', 'Most Mistaken Words', '<p class="muted">No incorrect-answer history for this list.</p>');
    const rows = words.map((w) => `<tr><td>${escapeHtml(w.word)}</td><td>${w.times_incorrect}</td><td>${w.times_correct}</td><td>${w.score.toFixed(1)}</td></tr>`).join('');
    return createCard('dash-card-nemesis', 'Most Mistaken Words', `
      <p class="muted">Historical incorrect-answer counts only. These rows do not create pending drills or session priority.</p>
      <table class="nemesis-table"><thead><tr><th>Word</th><th>Wrong</th><th>Right</th><th>Score</th></tr></thead><tbody>${rows}</tbody></table>`);
  }

  function renderPracticePaceCard(velocity) {
    const spw = velocity.avg_seconds_per_word != null ? `${velocity.avg_seconds_per_word}s` : 'N/A';
    return createCard('dash-card-velocity', 'Practice Pace', `
      <div class="velocity-tiles">
        <div class="vel-tile"><span class="vel-num">${spw}</span><span class="vel-label muted">average per practiced item</span></div>
        <div class="vel-tile"><span class="vel-num">${velocity.sessions || 0}</span><span class="vel-label muted">recorded sessions</span></div>
      </div>`);
  }

  // --- Word list editor ---
  const editorUser = document.getElementById('editor-user');
  const editorLang = document.getElementById('editor-lang');
  const editorTableWrap = document.getElementById('editor-table-wrap');
  const editorBody = document.getElementById('editor-body');
  const editorMessage = document.getElementById('editor-message');
  const editorRestart = document.getElementById('editor-restart');

  async function loadEditor() {
    showError(editorMessage, '');
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    if (!user || !lang) {
      showError(editorMessage, 'Select a user, language, level, and part of speech before loading.');
      return;
    }
    try {
      const params = new URLSearchParams({ user, lang });
      const data = await api(`/api/wordlist?${params.toString()}`);
      editorBody.innerHTML = '';
      (data.items || []).forEach(addEditorRow);
      editorTableWrap.style.display = 'block';
      if (editorRestart) editorRestart.style.display = '';
    } catch (err) {
      editorTableWrap.style.display = 'none';
      if (editorRestart) editorRestart.style.display = 'none';
      showError(editorMessage, err.message);
    }
  }

  async function restartEditorProgress() {
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    if (!user || !lang) return;
    if (!confirm('Reset all progress for this word list? This cannot be undone.')) return;
    showError(editorMessage, '');
    try {
      await api('/api/wordlist/restart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang }),
      });
      editorMessage.innerHTML = '<div class="success">Progress reset. The next session starts from the beginning.</div>';
    } catch (err) {
      showError(editorMessage, err.message);
    }
  }

  async function playEditorWord(text) {
    text = (text || '').trim();
    if (!text) return;
    const user = editorUser.value.trim();
    const lang = editorLang.value.trim();
    const played = await playPreGeneratedAudio(user, lang, text);
    if (played) return;
    fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, lang }),
    }).catch(() => {});
  }

  function addEditorRow(item) {
    const tr = document.createElement('tr');
    tr.dataset.id = item.id || '';
    tr._record = item.record || {};
    const definition = Array.isArray(item.definition) ? item.definition : [];
    const values = {
      word: item.word || '',
      def1: definition[0] || '',
      def2: definition[1] || '',
    };
    const playTd = document.createElement('td');
    const playBtn = document.createElement('button');
    playBtn.type = 'button';
    playBtn.className = 'secondary editor-play';
    playBtn.textContent = '▶';
    playBtn.title = 'Play pronunciation';
    playBtn.addEventListener('click', () => playEditorWord(tr.querySelector('.editor-word').value));
    playTd.appendChild(playBtn);
    tr.appendChild(playTd);
    ['word', 'def1', 'def2'].forEach((field) => {
      const td = document.createElement('td');
      const input = document.createElement('input');
      input.type = 'text';
      input.className = `editor-${field}`;
      input.value = values[field];
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
    const items = [...editorBody.querySelectorAll('tr')].map((tr) => {
      const record = structuredClone(tr._record || {});
      const original = Array.isArray(record.definition) ? [...record.definition]
        : (record.definition ? String(record.definition).split('\n') : []);
      original[0] = tr.querySelector('.editor-def1').value;
      original[1] = tr.querySelector('.editor-def2').value;
      return {
        id: tr.dataset.id,
        word: tr.querySelector('.editor-word').value,
        definition: original,
        record,
      };
    }).filter((item) => item.word.trim());
    try {
      const data = await api('/api/wordlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, lang, items }),
      });
      editorMessage.innerHTML = `<div class="success">Saved ${data.count} word(s) to ${escapeHtml(data.path)}</div>`;
    } catch (err) {
      showError(editorMessage, err.message);
    }
  }

  document.getElementById('editor-load').addEventListener('click', loadEditor);
  if (editorRestart) editorRestart.addEventListener('click', restartEditorProgress);
  document.getElementById('editor-add-row').addEventListener('click', () => {
    addEditorRow({word: '', definition: ['', ''], record: {}});
  });
  document.getElementById('editor-save').addEventListener('click', saveEditor);

  async function createWordList() {
    const initMessage = document.getElementById('init-message');
    showError(initMessage, '');
    const userInput = document.getElementById('init-user');
    const langInput = document.getElementById('init-lang');
    const typeInput = document.getElementById('init-type');
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
        body: JSON.stringify({ user, lang, type: typeInput.value }),
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


  // --- Import / Export / Custom Lists ---
  const btnCreateUser = document.getElementById('create-user');
  const createUserContainer = document.getElementById('create-user-container');
  const newUsernameInput = document.getElementById('new-username');
  const btnSubmitCreateUser = document.getElementById('submit-create-user');
  const btnCancelCreateUser = document.getElementById('cancel-create-user');

  if (btnCreateUser && createUserContainer) {
    btnCreateUser.addEventListener('click', () => {
      createUserContainer.style.display = 'flex';
      newUsernameInput.focus();
    });

    btnCancelCreateUser.addEventListener('click', () => {
      createUserContainer.style.display = 'none';
      newUsernameInput.value = '';
    });

    const submitUser = async () => {
      const newUser = newUsernameInput.value.trim();
      if (!newUser) {
        alert("Username cannot be empty");
        return;
      }
      try {
        await api('/api/user/create', {
          method: 'POST',
          body: JSON.stringify({ user: newUser })
        });
        alert(`User '${newUser}' created successfully!`);
        await loadWordLists(); // refresh user lists
        document.getElementById('practice-user').value = newUser;
        createUserContainer.style.display = 'none';
        newUsernameInput.value = '';
      } catch (err) {
        alert('Failed to create user: ' + err.message);
      }
    };

    btnSubmitCreateUser.addEventListener('click', submitUser);
    newUsernameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitUser();
      if (e.key === 'Escape') btnCancelCreateUser.click();
    });
  }
  const btnExportProgress = document.getElementById('export-progress');
  if (btnExportProgress) {
    btnExportProgress.addEventListener('click', async () => {
      const user = document.getElementById('practice-user').value;
      if (!user) { alert('Please select a user first'); return; }
      try {
        const data = await api(`/api/export?user=${encodeURIComponent(user)}`);
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `tartarus_export_${user}.json`;
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert('Export failed: ' + err.message);
      }
    });
  }

  const btnImportProgress = document.getElementById('import-progress');
  const fileImportProgress = document.getElementById('import-file');
  if (btnImportProgress && fileImportProgress) {
    btnImportProgress.addEventListener('click', () => fileImportProgress.click());
    fileImportProgress.addEventListener('change', async (e) => {
      const reportError = document.getElementById('practice-report-error');
      const user = document.getElementById('practice-user').value;
      showError(reportError, '');
      if (!user) { showError(reportError, 'Select a user before importing.'); return; }
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async (ev) => {
        try {
          const payload = { user, data: JSON.parse(ev.target.result) };
          await api('/api/import', { method: 'POST', body: JSON.stringify(payload) });
          await refreshPracticeReport();
          reportError.innerHTML = '<div class="success">Import successful.</div>';
        } catch (err) {
          showError(reportError, `Import failed: ${err.message}`);
        } finally {
          fileImportProgress.value = '';
        }
      };
      reader.onerror = () => {
        showError(reportError, 'Import failed: could not read the selected file.');
        fileImportProgress.value = '';
      };
      reader.readAsText(file);
    });
  }

  const btnShiftDates = document.getElementById('shift-dates');
  if (btnShiftDates) {
    btnShiftDates.addEventListener('click', async () => {
      const reportError = document.getElementById('practice-report-error');
      const user = document.getElementById('practice-user').value;
      showError(reportError, '');
      if (!user) { showError(reportError, 'Select a user before shifting dates.'); return; }
      if (!confirm(
        `Bring practice records up to today for '${user}'? `
        + 'If there is a gap -- a whole day missed, or work left unfinished since yesterday -- every '
        + 'practice-record date moves forward together so the most recent one lands on today. '
        + 'If the records are already current (practiced today), this does nothing.'
      )) return;
      // The backend is race-safe on its own (a second overlapping call
      // re-checks under its write lock and no-ops if the gap's already
      // closed), but disabling the button too avoids firing a pointless
      // second request from an impatient double-click in the first place.
      btnShiftDates.disabled = true;
      try {
        const result = await api('/api/user/shift-dates', { method: 'POST', body: JSON.stringify({ user }) });
        await refreshPracticeReport();
        // The backend reports exactly what it decided and why, so the
        // message reflects the real outcome instead of restating the
        // request. 'no_room' is deliberately worded as a refusal, not a
        // success: nothing moved and the learner should know why.
        const days = result.shift_days === 1 ? '1 day' : `${result.shift_days} days`;
        const outcome = {
          missed_day: `<div class="success">Practice dates moved forward ${days} to cover a missed day -- everything is now current as of today.</div>`,
          unfinished_learning: `<div class="success">Practice dates moved forward ${days} to cover work left unfinished -- everything is now current as of today.</div>`,
          current: '<div class="success">No gap to fill -- practice dates are already current.</div>',
          never_practiced: '<div class="success">Nothing to shift -- this user has not practiced yet.</div>',
          no_room: '<div class="success">Nothing to shift -- a record is already dated today, so there is no room to move without dating something in the future.</div>',
        }[result.reason];
        reportError.innerHTML = outcome
          || (result.shifted
            ? `<div class="success">Practice dates moved forward ${days}.</div>`
            : '<div class="success">No gap to fill -- practice dates are already current.</div>');
      } catch (err) {
        showError(reportError, `Shift failed: ${err.message}`);
      } finally {
        btnShiftDates.disabled = false;
      }
    });
  }

  const btnImportCustom = document.getElementById('import-custom-list');
  const fileImportCustom = document.getElementById('import-custom-file');
  if (btnImportCustom && fileImportCustom) {
    btnImportCustom.addEventListener('click', () => fileImportCustom.click());
    fileImportCustom.addEventListener('change', async (e) => {
      const user = document.getElementById('init-user').value;
      if (!user) { alert('Please enter a username in the field above'); return; }
      const file = e.target.files[0];
      if (!file) return;
      const listName = file.name.replace(/\.json$/, '');
      const reader = new FileReader();
      reader.onload = async (ev) => {
        try {
          const items = JSON.parse(ev.target.result);
          const payload = { user, list_name: listName, items };
          await api('/api/wordlist/custom', { method: 'POST', body: JSON.stringify(payload) });
          alert('Custom list imported successfully!');
          loadWordLists();
        } catch (err) {
          alert('Import failed: ' + err.message);
        }
      };
      reader.readAsText(file);
    });
  }

})();
