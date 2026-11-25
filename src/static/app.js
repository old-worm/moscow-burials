const PAGE_LIMIT = 50;
let currentOffset = 0;
let totalPlotsInDb = 0;
let doneBannerDismissed = false;
let isManualShutdown = false;
let isImportRunningNow = false;

const CLIENT_ID = (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function")
  ? crypto.randomUUID()
  : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

function showServerStoppedOverlay(manual = false) {
  const overlay = document.getElementById("serverStoppedOverlay");
  const titleEl = document.getElementById("serverStoppedTitle");
  const textEl = document.getElementById("serverStoppedText");
  if (!overlay) return;
  if (manual) {
    if (titleEl) titleEl.textContent = "Работа программы завершена";
    if (textEl) {
      textEl.textContent =
        "Работа программы завершена. Теперь вы можете закрыть эту вкладку браузера.";
    }
  } else if (!isManualShutdown) {
    if (titleEl) titleEl.textContent = "Связь с локальным сервером потеряна";
    if (textEl) {
      textEl.textContent =
        "Локальный сервер остановлен. Вы можете закрыть эту вкладку браузера или запустить приложение снова, чтобы продолжить работу.";
    }
  }
  overlay.classList.add("active");
}

function hideServerStoppedOverlay() {
  if (isManualShutdown) return;
  const overlay = document.getElementById("serverStoppedOverlay");
  if (overlay) {
    overlay.classList.remove("active");
  }
}

async function sendHeartbeat() {
  if (isManualShutdown) return;
  try {
    const res = await fetch(`/api/heartbeat?client_id=${encodeURIComponent(CLIENT_ID)}`, {
      method: "POST"
    });
    if (res.ok) {
      hideServerStoppedOverlay();
    }
  } catch (err) {
    showServerStoppedOverlay(false);
  }
}

let heartbeatTimer = setInterval(sendHeartbeat, 60000);
sendHeartbeat();

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !isManualShutdown) {
    sendHeartbeat();
  }
});

window.addEventListener("pagehide", () => {
  if (isManualShutdown) return;
  if (navigator.sendBeacon) {
    navigator.sendBeacon(`/api/client/close?client_id=${encodeURIComponent(CLIENT_ID)}`);
  }
});

let isReportingClientError = false;

function reportClientError(payload) {
  if (isReportingClientError || isManualShutdown) return;
  isReportingClientError = true;
  try {
    fetch("/api/client/error", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: String(payload.message || ""),
        source: String(payload.source || ""),
        lineno: payload.lineno || 0,
        colno: payload.colno || 0,
        stack: String(payload.stack || ""),
        url: window.location.href
      })
    }).catch(() => {}).finally(() => {
      isReportingClientError = false;
    });
  } catch (_) {
    isReportingClientError = false;
  }
}

window.addEventListener("error", (e) => {
  reportClientError({
    message: e.message || "Uncaught error",
    source: e.filename || "",
    lineno: e.lineno || 0,
    colno: e.colno || 0,
    stack: e.error && e.error.stack ? e.error.stack : ""
  });
});

window.addEventListener("unhandledrejection", (e) => {
  const reason = e.reason;
  reportClientError({
    message: reason && reason.message ? `UnhandledRejection: ${reason.message}` : `UnhandledRejection: ${String(reason)}`,
    source: "",
    lineno: 0,
    colno: 0,
    stack: reason && reason.stack ? reason.stack : ""
  });
});

function setView(mode) {
  const importPanel = document.getElementById("importPanel");
  const searchView = document.getElementById("searchView");
  const toggleBtn = document.getElementById("toggleImportBtn");
  const clearBtn = document.getElementById("clearDbBtn");
  const emptyBanner = document.getElementById("emptyDbBanner");

  if (mode === "import" || totalPlotsInDb === 0) {
    importPanel.classList.add("open");
    searchView.style.display = "none";
    if (totalPlotsInDb > 0) {
      emptyBanner.style.display = "none";
      toggleBtn.style.display = "inline-flex";
      toggleBtn.textContent = "Поиск";
      clearBtn.style.display = "inline-flex";
    } else {
      emptyBanner.style.display = "block";
      toggleBtn.style.display = "none";
      clearBtn.style.display = "none";
    }
  } else {
    emptyBanner.style.display = "none";
    doneBannerDismissed = true;
    document.getElementById("importProgressBox").style.display = "none";
    fetch("/api/import/dismiss", { method: "POST" }).catch(() => {});
    importPanel.classList.remove("open");
    searchView.style.display = "flex";
    toggleBtn.style.display = "inline-flex";
    toggleBtn.textContent = "Импорт";
  }
}

function getRadioValue(name) {
  const el = document.querySelector(`input[name="${name}"]:checked`);
  return el ? el.value : "prefix_default";
}

function updateModeToggleLabel(idx) {
  const btn = document.getElementById(`modeToggleBtn${idx}`);
  if (!btn) return;
  const val = getRadioValue(`mode${idx}`);
  if (val === "prefix_no_surname") {
    btn.textContent = "Режим: Без фамилии ▾";
    btn.classList.add("non-default");
  } else if (val === "substring") {
    btn.textContent = "Режим: В середине слов ▾";
    btn.classList.add("non-default");
  } else {
    btn.textContent = "Режим поиска ▾";
    btn.classList.remove("non-default");
  }
}

function setRadioValue(name, val) {
  const el = document.querySelector(`input[name="${name}"][value="${val}"]`);
  if (el) el.checked = true;
  if (name === "mode1") updateModeToggleLabel(1);
  if (name === "mode2") updateModeToggleLabel(2);
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

let cemeteriesList = [];
let cemHighlightIdx = -1;

function formatCemeteryLabel(c) {
  const codePart = c.code ? ` [${c.code}]` : "";
  return `${c.name}${codePart}`;
}

function setCemeteryById(idVal) {
  const hiddenEl = document.getElementById("cemetery_id");
  const textEl = document.getElementById("cemeterySearchInput");
  const clearBtn = document.getElementById("cemeteryClearBtn");
  const strId = idVal ? String(idVal) : "";
  hiddenEl.value = strId;
  if (!strId) {
    textEl.value = "— Все кладбища —";
    clearBtn.style.display = "none";
    return;
  }
  const found = cemeteriesList.find(c => String(c.id) === strId);
  if (found) {
    textEl.value = formatCemeteryLabel(found);
    clearBtn.style.display = "inline-flex";
  } else {
    textEl.value = "— Все кладбища —";
    clearBtn.style.display = "none";
  }
}

function renderCemeteryDropdown(filterText = "") {
  const dd = document.getElementById("cemeteryDropdown");
  const currentId = document.getElementById("cemetery_id").value;
  const normFilter = filterText.trim().toLowerCase().replace(/ё/g, "е");
  const filtered = cemeteriesList.filter(c => {
    if (!normFilter) return true;
    const hay = `${c.name} ${c.code || ""}`.toLowerCase().replace(/ё/g, "е");
    return hay.includes(normFilter);
  });

  let html = "";
  if (!normFilter) {
    const allSel = !currentId ? " selected" : "";
    html += `<div class="cemetery-opt${allSel}" data-cem-id=""><span>— Все кладбища —</span></div>`;
  }
  if (filtered.length === 0) {
    html += `<div class="cemetery-opt-empty">Кладбище не найдено</div>`;
  } else {
    for (const c of filtered) {
      const sel = String(c.id) === currentId ? " selected" : "";
      const label = escapeHtml(formatCemeteryLabel(c));
      const cnt = c.plot_count.toLocaleString("ru-RU");
      html += `<div class="cemetery-opt${sel}" data-cem-id="${c.id}"><span>${label}</span><span class="cemetery-opt-count">${cnt}</span></div>`;
    }
  }
  dd.innerHTML = html;
  cemHighlightIdx = -1;
}

async function loadMeta() {
  const badge = document.getElementById("metaBadge");
  const prevVal = document.getElementById("cemetery_id").value;
  try {
    const res = await fetch("/api/meta");
    if (!res.ok) throw new Error("Ошибка загрузки /api/meta");
    const data = await res.json();
    totalPlotsInDb = data.total_plots || 0;
    cemeteriesList = data.cemeteries || [];
    badge.textContent = `В базе: ${data.total_plots.toLocaleString("ru-RU")} участков • ${data.total_persons.toLocaleString("ru-RU")} надписей • ${cemeteriesList.length} кладбищ`;
    setCemeteryById(prevVal);
    renderCemeteryDropdown("");
    if (totalPlotsInDb === 0) {
      setView("import");
      loadSources();
    } else if (document.getElementById("importPanel").classList.contains("open")) {
      setView("import");
    } else {
      setView("search");
    }
  } catch (err) {
    badge.textContent = "Ошибка чтения метаданных БД";
    if (err instanceof TypeError) {
      showServerStoppedOverlay(false);
    }
  }
}

function formatBytes(bytes) {
  if (!bytes) return "0 Б";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(1)} МБ`;
  return `${(bytes / 1024).toFixed(1)} КБ`;
}

let importPollTimer = null;

function formatDuration(totalSec) {
  const sec = Math.max(0, Math.floor(totalSec || 0));
  if (sec < 60) return `${sec} с`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = String(sec % 60).padStart(2, "0");
  if (h > 0) return `${h} ч ${String(m).padStart(2, "0")} мин`;
  return `${m} мин ${s} с`;
}

// Блок прогресса собирается из постоянных частей: шапка и детали перерисовываются
// при каждом опросе, а индикатор активности обновляется точечно — иначе анимация
// спиннера и полосы перезапускалась бы каждые 600 мс.
function getImportBoxParts(box) {
  if (!box._parts) {
    box.innerHTML = `
      <div class="import-box-head"></div>
      <div class="import-activity" hidden>
        <div class="import-activity-row">
          <span class="import-spinner" aria-hidden="true"></span>
          <span class="import-activity-text" role="status" aria-live="polite"></span>
          <span class="import-activity-timer"></span>
          <button type="button" class="btn btn-outline import-cancel-btn" hidden>Остановить импорт</button>
        </div>
        <div class="import-activity-track"><div class="import-activity-fill"></div></div>
        <div class="import-activity-hint" hidden></div>
      </div>
      <div class="import-box-body"></div>
    `;
    box._parts = {
      head: box.querySelector(".import-box-head"),
      body: box.querySelector(".import-box-body"),
      activity: box.querySelector(".import-activity"),
      text: box.querySelector(".import-activity-text"),
      timer: box.querySelector(".import-activity-timer"),
      track: box.querySelector(".import-activity-track"),
      fill: box.querySelector(".import-activity-fill"),
      hint: box.querySelector(".import-activity-hint"),
      cancelBtn: box.querySelector(".import-cancel-btn"),
    };
    box._parts.cancelBtn.addEventListener("click", cancelImport);
  }
  return box._parts;
}

const CANCELLABLE_IMPORT_PHASES = ["hashing", "reading", "indexing"];

async function cancelImport() {
  const ok = window.confirm(
    "Остановить импорт?\n\n"
    + "Все изменения, которые этот импорт успел внести в базу, будут отменены — "
    + "база останется такой же, как до начала импорта.\n\n"
    + "Сам файл останется в папке input/, его можно будет импортировать позже."
  );
  if (!ok) return;
  const btn = document.querySelector("#importProgressBox .import-cancel-btn");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/import/cancel", { method: "POST" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "Не удалось остановить импорт");
    }
  } catch (err) {
    if (err instanceof TypeError) {
      showServerStoppedOverlay(false);
      return;
    }
    alert(err.message);
  }
  await pollImportStatus();
}

function updateImportActivity(parts, st) {
  const { activity, text, timer, track, fill, hint, cancelBtn } = parts;
  if (!st.running || st.error) {
    activity.hidden = true;
    return;
  }
  activity.hidden = false;

  let label = "Импорт выполняется";
  let timerLabel = "";
  let pct = null;
  let hintText = "";
  if (st.phase === "uploading") {
    pct = st.bytes_total ? Math.min(100, Math.round((st.bytes_received / st.bytes_total) * 100)) : 0;
    label = `Файл передаётся в приложение — ${pct}%`;
  } else if (st.phase === "hashing") {
    label = "Проверка файла";
    timerLabel = formatDuration(st.phase_elapsed_sec);
  } else if (st.phase === "reading") {
    label = "Чтение файла и запись в базу";
    timerLabel = `прошло ${formatDuration(st.elapsed_sec)}`;
  } else if (st.phase === "indexing") {
    label = st.index_step && st.index_steps
      ? `Индексация, шаг ${st.index_step} из ${st.index_steps}: ${st.index_label || ""}`
      : "Подготовка индексации";
    timerLabel = `шаг идёт ${formatDuration(st.step_elapsed_sec ?? st.phase_elapsed_sec)}`;
    hintText = "Это самый долгий этап: на медленных компьютерах он может занять десятки минут. "
      + "Счётчики участков в это время не меняются — это нормально, пока крутится значок и идёт время, импорт работает. "
      + "Не закрывайте приложение.";
  }

  const canCancel = CANCELLABLE_IMPORT_PHASES.includes(st.phase);
  cancelBtn.hidden = !canCancel;
  if (st.cancel_requested) {
    label = "Импорт останавливается, изменения отменяются";
    hintText = "Обычно это занимает несколько секунд. Не закрывайте приложение.";
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Останавливаем…";
  } else {
    cancelBtn.disabled = false;
    cancelBtn.textContent = "Остановить импорт";
  }

  text.textContent = `${label}…`;
  timer.textContent = timerLabel;
  track.classList.toggle("indeterminate", pct === null);
  fill.style.width = pct === null ? "" : `${pct}%`;
  hint.textContent = hintText;
  hint.hidden = !hintText;
}

function renderImportStatus(st) {
  const box = document.getElementById("importProgressBox");
  isImportRunningNow = Boolean(st && st.running);
  if (!st || (!st.running && st.phase === "idle")) {
    box.style.display = "none";
    return;
  }
  if (st.running) {
    doneBannerDismissed = false;
  }
  if (!st.running && st.phase === "done" && doneBannerDismissed) {
    box.style.display = "none";
    return;
  }
  box.style.display = "block";

  if (st.error) {
    box.style.background = "#fef2f2";
    box.style.borderColor = "#fecaca";
    box.style.color = "#991b1b";
    const errParts = getImportBoxParts(box);
    updateImportActivity(errParts, st);
    errParts.body.innerHTML = "";
    errParts.head.innerHTML = `
      <div class="progress-top">
        <div class="progress-title-row">
          <span class="progress-phase-pill">Ошибка импорта</span>
          ${st.file_name ? `<b>${escapeHtml(st.file_name)}</b>` : ""}
        </div>
      </div>
      <div class="import-error-message">${escapeHtml(st.error)}</div>
      <div class="import-error-actions">
        <a href="/api/log.zip" download="moscow-burials-diagnostic.zip" class="btn btn-outline import-error-btn" title="Скачать диагностический архив с логом работы и сведениями о системе">Скачать лог (.zip)</a>
        <button type="button" class="btn btn-outline import-error-btn" id="openLogDirErrorBtn" title="Сохранить диагностический архив в папке logs/ и показать его в Finder / Проводнике">Показать лог в папке</button>
        <button type="button" class="btn btn-outline import-error-btn" id="dismissImportErrorBtn" title="Скрыть сообщение об ошибке">Скрыть ×</button>
      </div>
    `;
    const openLogBtn = document.getElementById("openLogDirErrorBtn");
    if (openLogBtn) {
      openLogBtn.addEventListener("click", async () => {
        try {
          const res = await fetch("/api/open-log-dir", { method: "POST" });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "Не удалось открыть папку с логом");
        } catch (err) {
          if (err instanceof TypeError) {
            showServerStoppedOverlay(false);
          }
        }
      });
    }
    const dismissBtn = document.getElementById("dismissImportErrorBtn");
    if (dismissBtn) {
      dismissBtn.addEventListener("click", () => {
        box.style.display = "none";
        fetch("/api/import/dismiss", { method: "POST" }).catch(() => {});
      });
    }
    return;
  }

  if (!st.running && st.phase === "cancelled") {
    box.style.background = "#f8fafc";
    box.style.borderColor = "#cbd5e1";
    box.style.color = "#334155";
    const cParts = getImportBoxParts(box);
    updateImportActivity(cParts, st);
    cParts.head.innerHTML = `
      <div class="progress-top">
        <div class="progress-title-row">
          <span class="progress-phase-pill">Импорт остановлен</span>
          <b>${escapeHtml(st.file_name || "")}</b>
        </div>
        <button type="button" class="btn btn-outline import-dismiss-btn" id="dismissImportCancelledBtn" title="Скрыть сообщение">Скрыть ×</button>
      </div>
    `;
    cParts.body.innerHTML = `<div class="import-cancelled-message">Изменения отменены — база данных осталась такой же, как до начала импорта. Файл остался в папке <code>input/</code>, его можно импортировать позже.</div>`;
    document.getElementById("dismissImportCancelledBtn").addEventListener("click", () => {
      box.style.display = "none";
      fetch("/api/import/dismiss", { method: "POST" }).catch(() => {});
    });
    return;
  }

  const isDone = st.phase === "done";
  box.style.background = isDone ? "#f0fdf4" : "#f0f9ff";
  box.style.borderColor = isDone ? "#bbf7d0" : "#bae6fd";
  box.style.color = isDone ? "#166534" : "#0c4a6e";

  const dateLabel = st.source_date ? escapeHtml(st.source_date) : "не указана";
  const dupHtml = st.was_duplicate
    ? `<span style="font-size: 12px; opacity: 0.85;">• Файл уже был в <code>input/</code> (дубликат не создавался)</span>`
    : "";

  let phaseLabel = "Импорт";
  let subNoteHtml = "";
  if (st.phase === "uploading") {
    phaseLabel = "Загрузка файла";
    subNoteHtml = `<div style="margin-top: 6px;">Передано в папку <code>input/</code>: ${formatBytes(st.bytes_received)} из ${formatBytes(st.bytes_total)}</div>`;
  } else if (st.phase === "hashing") {
    phaseLabel = "Проверка файла";
    subNoteHtml = `<div style="margin-top: 6px;">Вычисление контрольной суммы SHA-256 и проверка дубликатов</div>`;
  } else if (st.phase === "reading") {
    phaseLabel = "Потоковый импорт";
  } else if (st.phase === "indexing") {
    phaseLabel = "Индексация поиска";
  } else if (st.phase === "done") {
    phaseLabel = "Импорт завершён";
  }

  const actionBtnHtml = isDone
    ? `<button type="button" class="btn btn-success" id="doneGoSearchBtn" style="white-space: nowrap; flex-shrink: 0; height: 32px; padding: 0 14px; font-size: 13px;">К поиску →</button>`
    : "";

  let statsGridHtml = "";
  if (st.phase === "reading" || st.phase === "indexing" || st.phase === "done") {
    const seen = (st.plots_seen || 0).toLocaleString("ru-RU");
    const pSeen = (st.persons_seen || 0).toLocaleString("ru-RU");
    const ins = (st.plots_inserted || 0).toLocaleString("ru-RU");
    const upd = (st.plots_updated || 0).toLocaleString("ru-RU");
    const sec = formatDuration(st.elapsed_sec);
    statsGridHtml = `
      <div class="progress-stats">
        <div class="progress-stat-item">
          <div class="progress-stat-label">Участков в файле</div>
          <div class="progress-stat-value">${seen}</div>
        </div>
        <div class="progress-stat-item">
          <div class="progress-stat-label">Захоронений в файле</div>
          <div class="progress-stat-value">${pSeen}</div>
        </div>
        <div class="progress-stat-item">
          <div class="progress-stat-label">Новых участков</div>
          <div class="progress-stat-value">${ins}</div>
        </div>
        <div class="progress-stat-item">
          <div class="progress-stat-label">Обновлено участков</div>
          <div class="progress-stat-value">${upd}</div>
        </div>
        <div class="progress-stat-item">
          <div class="progress-stat-label">Время</div>
          <div class="progress-stat-value">${sec}</div>
        </div>
      </div>
    `;
  }

  const parts = getImportBoxParts(box);
  updateImportActivity(parts, st);
  parts.body.innerHTML = `${subNoteHtml}${statsGridHtml}`;
  parts.head.innerHTML = `
    <div class="progress-top">
      <div class="progress-title-row">
        <span class="progress-phase-pill">${phaseLabel}</span>
        <b>${escapeHtml(st.file_name || "")}</b>
        <span style="font-size: 12px; opacity: 0.85;">• Дата данных: <b>${dateLabel}</b></span>
        ${dupHtml}
      </div>
      ${actionBtnHtml}
    </div>
  `;

  const goBtn = document.getElementById("doneGoSearchBtn");
  if (goBtn) {
    goBtn.addEventListener("click", () => setView("search"));
  }
}

async function pollImportStatus() {
  try {
    const res = await fetch("/api/import/status");
    if (!res.ok) return;
    const st = await res.json();
    renderImportStatus(st);
    if (st.running) {
      if (!importPollTimer) {
        importPollTimer = setInterval(pollImportStatus, 600);
      }
    } else {
      if (importPollTimer) {
        clearInterval(importPollTimer);
        importPollTimer = null;
        await loadMeta();
        await loadSources();
      }
    }
  } catch (e) {
    // Игнорируем кратковременные сетевые ошибки при опросе
  }
}

async function loadSources() {
  const tbody = document.getElementById("sourcesTableBody");
  const pathHint = document.getElementById("inputDirPathHint");
  try {
    const res = await fetch("/api/sources");
    if (!res.ok) throw new Error("Не удалось получить список файлов");
    const data = await res.json();
    hideServerStoppedOverlay();
    if (pathHint && data.input_dir) {
      pathHint.innerHTML = `— <code>${escapeHtml(data.input_dir)}</code>`;
    }
    renderImportStatus(data.job);
    if (data.job && data.job.running && !importPollTimer) {
      importPollTimer = setInterval(pollImportStatus, 600);
    }

    const files = data.files || [];
    if (files.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" style="color: var(--muted-fg);">В папке <code>input/</code> пока нет файлов (.xlsx, .json, .csv, .xml). Выберите файл выше, чтобы добавить его.</td></tr>`;
      return;
    }

    tbody.innerHTML = "";
    for (const f of files) {
      const tr = document.createElement("tr");
      const statusBadge = f.imported
        ? `<span class="badge-ok">Импортирован</span>`
        : `<span class="badge-new">Не импортирован</span>`;
      const countsHtml = (f.imported && f.plots_seen !== null && f.plots_seen !== undefined)
        ? `<div><b>${f.plots_seen.toLocaleString("ru-RU")}</b> участков</div><div class="hint"><b>${(f.persons_seen || 0).toLocaleString("ru-RU")}</b> захоронений</div>`
        : `<span style="color: var(--muted-fg);">—</span>`;
      const dateVal = f.source_date || "";
      const dateHint = f.detected_date
        ? `<div class="hint">Определена из файла: ${escapeHtml(f.detected_date)}</div>`
        : `<div class="hint">Не указана (дополнение пустых полей)</div>`;
      const btnLabel = f.imported ? "Повторный импорт" : "Импортировать";
      const isRunning = data.job && data.job.running;

      tr.innerHTML = `
        <td><b>${escapeHtml(f.file_name)}</b></td>
        <td>${formatBytes(f.file_size)}</td>
        <td style="white-space: nowrap;">${countsHtml}</td>
        <td>
          <input type="date" class="src-date-input" data-file="${escapeHtml(f.file_name)}" value="${escapeHtml(dateVal)}" style="height: 30px; padding: 0 8px; border: 1px solid var(--input); border-radius: 5px; font-size: 12.5px;" />
          ${dateHint}
        </td>
        <td>${statusBadge}</td>
        <td style="text-align: right;">
          <button type="button" class="btn btn-outline btn-start-import" data-file="${escapeHtml(f.file_name)}" style="height: 30px; padding: 0 12px; font-size: 12.5px;" ${isRunning ? "disabled" : ""}>${btnLabel}</button>
        </td>
      `;
      tbody.appendChild(tr);
    }

    tbody.querySelectorAll(".btn-start-import").forEach(btn => {
      btn.addEventListener("click", async () => {
        const fileName = btn.getAttribute("data-file");
        const dateInput = tbody.querySelector(`.src-date-input[data-file="${CSS.escape(fileName)}"]`);
        const srcDate = dateInput ? dateInput.value.trim() : "";
        await startImportFromInput(fileName, srcDate);
      });
    });
  } catch (err) {
    if (err instanceof TypeError) {
      showServerStoppedOverlay(false);
      tbody.innerHTML = `<tr><td colspan="6" style="color: #991b1b;">Связь с локальным сервером потеряна.</td></tr>`;
    } else {
      tbody.innerHTML = `<tr><td colspan="6" style="color: #991b1b;">Ошибка: ${escapeHtml(err.message)}</td></tr>`;
    }
  }
}

async function startImportFromInput(fileName, sourceDate) {
  try {
    const res = await fetch("/api/import/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_name: fileName, source_date: sourceDate || null })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Ошибка запуска импорта");
    renderImportStatus(data);
    if (!importPollTimer) {
      importPollTimer = setInterval(pollImportStatus, 600);
    }
    await loadSources();
  } catch (err) {
    if (err instanceof TypeError) {
      showServerStoppedOverlay(false);
    } else {
      renderImportStatus({
        running: false,
        phase: "error",
        file_name: fileName,
        error: err.message
      });
    }
  }
}

const DATE_INPUT_IDS = ["birth1_exact", "death1_exact", "birth2_exact", "death2_exact"];

function parseSingleBoundClient(rawBound) {
  const s = rawBound.trim();
  if (/^\d{4}$/.test(s)) {
    const y = parseInt(s, 10);
    if (y < 1000 || y > 2999) return { error: `Некорректный год: ${s}` };
    return { year: y, month: null, day: null };
  }
  const mMy = /^(\d{1,2})\.(\d{4})$/.exec(s);
  if (mMy) {
    const m = parseInt(mMy[1], 10);
    const y = parseInt(mMy[2], 10);
    if (y < 1000 || y > 2999) return { error: `Некорректный год в дате: ${s}` };
    if (m < 1 || m > 12) return { error: `Некорректный месяц в дате ${s} (допустимо 01–12)` };
    return { year: y, month: m, day: null };
  }
  const mDmy = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(s);
  if (mDmy) {
    const d = parseInt(mDmy[1], 10);
    const m = parseInt(mDmy[2], 10);
    const y = parseInt(mDmy[3], 10);
    if (y < 1000 || y > 2999) return { error: `Некорректный год в дате: ${s}` };
    if (m < 1 || m > 12) return { error: `Некорректный месяц в дате ${s} (допустимо 01–12)` };
    const dt = new Date(y, m - 1, d);
    if (dt.getFullYear() !== y || dt.getMonth() !== m - 1 || dt.getDate() !== d) {
      return { error: `Несуществующая календарная дата: ${s}` };
    }
    return { year: y, month: m, day: d };
  }
  return {
    error: "Формат: ГГГГ, ММ.ГГГГ, ДД.ММ.ГГГГ или диапазон через дефис (1900-12.12.1967)"
  };
}

function validateDateExprClient(rawExpr) {
  if (!rawExpr) return null;
  const cleaned = rawExpr.replace(/[–—]|\.\./g, "-").trim();
  if (!cleaned) return null;
  if (!cleaned.includes("-")) {
    const b = parseSingleBoundClient(cleaned);
    return b.error || null;
  }
  const parts = cleaned.split("-").map(p => p.trim());
  if (parts.length > 2) {
    return "Разделитель дней/месяцев — точка, диапазона — один дефис (например: 1900-12.12.1967)";
  }
  const [leftStr, rightStr] = parts;
  if (!leftStr && !rightStr) {
    return "Укажите хотя бы одну границу диапазона дат";
  }
  let lo = null;
  let hi = null;
  if (leftStr) {
    lo = parseSingleBoundClient(leftStr);
    if (lo.error) return lo.error;
  }
  if (rightStr) {
    hi = parseSingleBoundClient(rightStr);
    if (hi.error) return hi.error;
  }
  if (lo && hi) {
    const loTuple = lo.year * 10000 + (lo.month || 1) * 100 + (lo.day || 1);
    const hiTuple = hi.year * 10000 + (hi.month || 12) * 100 + (hi.day || 31);
    if (loTuple > hiTuple) {
      return `Начало диапазона (${leftStr}) позже конца (${rightStr})`;
    }
  }
  return null;
}

function validateSingleDateInput(id) {
  const input = document.getElementById(id);
  const errEl = document.getElementById(`${id}_err`);
  if (!input || !errEl) return true;
  const errMsg = validateDateExprClient(input.value);
  if (errMsg) {
    input.classList.add("input-invalid");
    errEl.textContent = errMsg;
    errEl.classList.add("visible");
    return false;
  }
  input.classList.remove("input-invalid");
  errEl.textContent = "";
  errEl.classList.remove("visible");
  return true;
}

function validateAllDateInputs() {
  let firstInvalid = null;
  for (const id of DATE_INPUT_IDS) {
    if (!validateSingleDateInput(id) && !firstInvalid) {
      firstInvalid = document.getElementById(id);
    }
  }
  if (firstInvalid) firstInvalid.focus();
  return !firstInvalid;
}

function clearDateErrors() {
  for (const id of DATE_INPUT_IDS) {
    const input = document.getElementById(id);
    const errEl = document.getElementById(`${id}_err`);
    if (input) input.classList.remove("input-invalid");
    if (errEl) {
      errEl.textContent = "";
      errEl.classList.remove("visible");
    }
  }
}

function collectParams(offsetVal) {
  const params = new URLSearchParams();
  const fields = [
    "fio1", "birth1_exact", "death1_exact",
    "fio2", "birth2_exact", "death2_exact",
    "cemetery_id", "place_number", "global_id"
  ];
  for (const id of fields) {
    const v = document.getElementById(id).value.trim();
    if (v !== "") params.set(id, v);
  }
  const m1 = getRadioValue("mode1");
  if (m1 !== "prefix_default") params.set("mode1", m1);
  const m2 = getRadioValue("mode2");
  if (m2 !== "prefix_default") params.set("mode2", m2);
  params.set("limit", String(PAGE_LIMIT));
  if (offsetVal > 0) params.set("offset", String(offsetVal));
  return params;
}

function restoreFormFromUrl() {
  const qs = new URLSearchParams(window.location.search);
  let hasFilter = false;
  const fields = [
    "fio1", "birth1_exact", "death1_exact",
    "fio2", "birth2_exact", "death2_exact",
    "cemetery_id", "place_number", "global_id"
  ];
  for (const id of fields) {
    if (qs.has(id)) {
      document.getElementById(id).value = qs.get(id);
      if (id === "cemetery_id") {
        setCemeteryById(qs.get(id));
      }
      hasFilter = true;
    }
  }
  [["birth1", "birth1_exact"], ["death1", "death1_exact"], ["birth2", "birth2_exact"], ["death2", "death2_exact"]].forEach(([prefix, targetId]) => {
    const el = document.getElementById(targetId);
    if (el && !el.value && (qs.has(`${prefix}_from`) || qs.has(`${prefix}_to`))) {
      el.value = `${qs.get(`${prefix}_from`) || ""}-${qs.get(`${prefix}_to`) || ""}`;
      hasFilter = true;
    }
  });
  if (qs.has("mode1")) setRadioValue("mode1", qs.get("mode1"));
  if (qs.has("mode2")) setRadioValue("mode2", qs.get("mode2"));
  const initOffset = parseInt(qs.get("offset") || "0", 10) || 0;
  return { hasFilter, initOffset };
}

async function runSearch(offsetVal = 0) {
  currentOffset = offsetVal;
  const errBanner = document.getElementById("errorBanner");
  errBanner.style.display = "none";

  const params = collectParams(offsetVal);
  const urlParams = new URLSearchParams(params);
  urlParams.delete("limit");
  const newQuery = urlParams.toString();
  const newUrl = newQuery ? `${window.location.pathname}?${newQuery}` : window.location.pathname;
  window.history.replaceState(null, "", newUrl);

  const searchBtn = document.getElementById("searchBtn");
  searchBtn.disabled = true;
  searchBtn.textContent = "Поиск...";

  try {
    const res = await fetch(`/api/search?${params.toString()}`);
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || "Ошибка поискового запроса");
    }
    hideServerStoppedOverlay();
    renderResults(data);
  } catch (err) {
    if (err instanceof TypeError) {
      showServerStoppedOverlay(false);
      errBanner.textContent = "Связь с локальным сервером потеряна. Запустите приложение снова.";
    } else {
      errBanner.textContent = err.message;
    }
    errBanner.style.display = "block";
  } finally {
    searchBtn.disabled = false;
    searchBtn.textContent = "Найти";
  }
}

function splitPersonDisplay(p) {
  if (typeof p.fio_display === "string" || typeof p.dates_display === "string") {
    return {
      fio: p.fio_display || "",
      dates: p.dates_display || ""
    };
  }
  return {
    fio: (p.raw_line || "").trim(),
    dates: ""
  };
}

const COPY_ICON_SVG = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>`;
const CHECK_ICON_SVG = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;

async function copyTextToClipboard(text, btn) {
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    if (btn) {
      if (btn._copyTimer) clearTimeout(btn._copyTimer);
      if (!btn.dataset.origHtml) {
        btn.dataset.origHtml = btn.innerHTML;
      }
      btn.classList.add("copied");
      if (btn.classList.contains("copy-block-btn")) {
        btn.innerHTML = `${CHECK_ICON_SVG}<span>Скопировано</span>`;
      } else {
        btn.innerHTML = CHECK_ICON_SVG;
      }
      btn._copyTimer = setTimeout(() => {
        btn.classList.remove("copied");
        if (btn.dataset.origHtml) {
          btn.innerHTML = btn.dataset.origHtml;
        }
      }, 1300);
    }
  } catch (err) {
    console.error("Не удалось скопировать в буфер:", err);
  }
}

function renderResults(data) {
  const tbody = document.getElementById("resultsBody");
  const statusBar = document.getElementById("statusBar");
  const statusText = document.getElementById("statusText");
  const prevBtn = document.getElementById("prevBtn");
  const nextBtn = document.getElementById("nextBtn");
  const exportBtn = document.getElementById("exportCsvBtn");
  const statusBarBottom = document.getElementById("statusBarBottom");
  const statusTextBottom = document.getElementById("statusTextBottom");
  const prevBtnBottom = document.getElementById("prevBtnBottom");
  const nextBtnBottom = document.getElementById("nextBtnBottom");

  tbody.innerHTML = "";
  statusBar.style.display = "flex";

  const count = data.results.length;
  if (count === 0) {
    statusText.textContent = `Ничего не найдено (${data.elapsed_ms.toFixed(2)} мс)`;
    prevBtn.disabled = currentOffset === 0;
    nextBtn.disabled = true;
    exportBtn.disabled = true;
    statusBarBottom.style.display = "none";
    tbody.innerHTML = `<tr><td colspan="3" class="empty-state not-found">Участков по заданным условиям не найдено.</td></tr>`;
    return;
  }

  exportBtn.disabled = false;
  const fromIdx = currentOffset + 1;
  const toIdx = currentOffset + count;
  const moreSign = data.total_plots >= 10000 && data.has_more ? "+" : "";
  const summaryStr = `Показаны участки ${fromIdx}–${toIdx} из ${data.total_plots.toLocaleString("ru-RU")}${moreSign} (${data.elapsed_ms.toFixed(2)} мс)`;
  statusText.textContent = summaryStr;
  statusTextBottom.textContent = summaryStr;
  prevBtn.disabled = currentOffset === 0;
  nextBtn.disabled = !data.has_more;
  prevBtnBottom.disabled = currentOffset === 0;
  nextBtnBottom.disabled = !data.has_more;
  statusBarBottom.style.display = "flex";

  for (const plot of data.results) {
    const tr = document.createElement("tr");

    const secHtml = plot.plot_section
      ? `<div>Участок / колумбарий: <button type="button" class="plot-section-link" data-filter-cemetery="${plot.cemetery_id}" data-filter-section="${escapeHtml(plot.plot_section)}" title="Показать все захоронения на участке ${escapeHtml(plot.plot_section)} кладбища «${escapeHtml(plot.cemetery_name)}»">${escapeHtml(plot.plot_section)}</button></div>`
      : "";
    const placeHtml = plot.place_number ? `<div>Шифр: <code>${escapeHtml(plot.place_number)}</code></div>` : "";
    const areaHtml = plot.area !== null ? `<div>Площадь: ${escapeHtml(plot.area)} м²</div>` : "";
    const coordHtml = (plot.lat !== null && plot.lon !== null)
      ? `<div>Координаты: ${plot.lat.toFixed(6)}, ${plot.lon.toFixed(6)}</div>`
      : "";

    const plotCopyLines = [
      plot.cemetery_name,
      `Тип: ${plot.grave_type_label}`,
      plot.plot_section ? `Участок / колумбарий: ${plot.plot_section}` : null,
      plot.place_number ? `Шифр: ${plot.place_number}` : null,
      plot.area !== null ? `Площадь: ${plot.area} м²` : null,
      (plot.lat !== null && plot.lon !== null) ? `Координаты: ${plot.lat.toFixed(6)}, ${plot.lon.toFixed(6)}` : null,
      `global_id: ${plot.global_id}`
    ].filter(Boolean).join("\n");

    let personsHtml = "";
    if (plot.persons && plot.persons.length > 0) {
      const allPersonLines = [];
      const items = plot.persons.map(p => {
        let cls = "inscription-item";
        if (p.matched_p1) {
          cls += " match-p1";
        } else if (p.matched_p2) {
          cls += " match-p2";
        }
        const { fio: fioDisplay, dates: datesDisplay } = splitPersonDisplay(p);
        const fioText = fioDisplay || "ФИО не указано";
        const personCopyLine = datesDisplay ? `${fioText} ${datesDisplay}` : fioText;
        allPersonLines.push(personCopyLine);

        const fioCls = "person-fio" + (fioDisplay ? "" : " empty-fio") + (datesDisplay ? "" : " full-width");
        const fioHtml = `<span class="${fioCls}">${escapeHtml(fioText)}</span>`;
        const datesHtml = datesDisplay
          ? ` <span class="person-dates">${escapeHtml(datesDisplay)}</span>`
          : "";
        const copyBtnHtml = `<button type="button" class="copy-icon-btn copy-person-btn" data-copy-text="${escapeHtml(personCopyLine)}" title="Скопировать ФИО и даты" aria-label="Скопировать ФИО и даты">${COPY_ICON_SVG}</button>`;
        return `<li class="${cls}">${fioHtml}${datesHtml}${copyBtnHtml}</li>`;
      }).join("");
      const allPersonsCopyText = allPersonLines.join("\n");
      const blockFooterHtml = `
        <div class="persons-footer">
          <button type="button" class="copy-block-btn" data-copy-text="${escapeHtml(allPersonsCopyText)}" title="Скопировать все имена и даты на участке">
            ${COPY_ICON_SVG}<span>Копировать весь список</span>
          </button>
        </div>
      `;
      personsHtml = `<ul class="inscriptions-list">${items}</ul>${blockFooterHtml}`;
    } else {
      personsHtml = `<span style="color:var(--muted-fg);font-style:italic;">Надмогильное сооружение / надписи отсутствуют</span>`;
    }

    const epoiskMapHtml = plot.epoisk_map_url
      ? `<a class="ext-link" style="grid-column: 1 / -1;" href="${escapeHtml(plot.epoisk_map_url)}" target="_blank" rel="noopener noreferrer" title="Интерактивная схема кладбища на epoisk.ru с выделением участка">Схема кладбища (epoisk)</a>`
      : "";
    const yaLinkHtml = plot.ya_url
      ? `<a class="ext-link" href="${escapeHtml(plot.ya_url)}" target="_blank" rel="noopener noreferrer">Яндекс.Карты</a>`
      : "";
    const googleLinkHtml = plot.google_url
      ? `<a class="ext-link" href="${escapeHtml(plot.google_url)}" target="_blank" rel="noopener noreferrer">Google Карты</a>`
      : "";
    const osmLinkHtml = plot.osm_url
      ? `<a class="ext-link" href="${escapeHtml(plot.osm_url)}" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>`
      : "";
    const nakarteLinkHtml = plot.nakarte_url
      ? `<a class="ext-link" href="${escapeHtml(plot.nakarte_url)}" target="_blank" rel="noopener noreferrer" title="nakarte.me (слой Bing Satellite)">nakarte.me</a>`
      : "";

    tr.innerHTML = `
      <td class="plot-cell">
        <div class="cemetery-header">
          <button type="button" class="cemetery-link-btn" data-filter-cemetery="${plot.cemetery_id}" title="Выбрать кладбище «${escapeHtml(plot.cemetery_name)}» в фильтре поиска">${escapeHtml(plot.cemetery_name)}</button>
          <button type="button" class="copy-icon-btn copy-plot-btn" data-copy-text="${escapeHtml(plotCopyLines)}" title="Скопировать сведения о захоронении" aria-label="Скопировать сведения о захоронении">${COPY_ICON_SVG}</button>
        </div>
        <div class="plot-meta">
          <div>Тип: ${escapeHtml(plot.grave_type_label)}</div>
          ${secHtml}
          ${placeHtml}
          ${areaHtml}
          ${coordHtml}
          <div>global_id: <code>${plot.global_id}</code></div>
        </div>
      </td>
      <td class="persons-cell">${personsHtml}</td>
      <td>
        <div class="links-col">
          ${epoiskMapHtml}
          ${yaLinkHtml}
          ${googleLinkHtml}
          ${osmLinkHtml}
          ${nakarteLinkHtml}
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  }
}

document.getElementById("resultsBody").addEventListener("click", (e) => {
  const copyBtn = e.target.closest("[data-copy-text]");
  if (copyBtn) {
    copyTextToClipboard(copyBtn.getAttribute("data-copy-text"), copyBtn);
    return;
  }
  const secBtn = e.target.closest("[data-filter-section]");
  if (secBtn) {
    const cemId = secBtn.getAttribute("data-filter-cemetery");
    const secVal = secBtn.getAttribute("data-filter-section");
    document.getElementById("searchForm").reset();
    clearDateErrors();
    updateModeToggleLabel(1);
    updateModeToggleLabel(2);
    setCemeteryById(cemId);
    document.getElementById("place_number").value = secVal;
    runSearch(0).then(() => {
      document.getElementById("statusBar").scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    return;
  }
  const cemBtn = e.target.closest("[data-filter-cemetery]");
  if (cemBtn) {
    const cemId = cemBtn.getAttribute("data-filter-cemetery");
    setCemeteryById(cemId);
    document.getElementById("global_id").value = "";
    runSearch(0).then(() => {
      document.getElementById("statusBar").scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }
});

DATE_INPUT_IDS.forEach(id => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("blur", () => validateSingleDateInput(id));
  el.addEventListener("input", () => {
    if (el.classList.contains("input-invalid")) {
      validateSingleDateInput(id);
    }
  });
});

document.getElementById("searchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!validateAllDateInputs()) return;
  runSearch(0);
});

document.querySelectorAll(".icon-help-btn[data-help-target]").forEach(btn => {
  btn.addEventListener("click", () => {
    const targetId = btn.getAttribute("data-help-target");
    const box = document.getElementById(targetId);
    if (!box) return;
    const isOpen = box.classList.toggle("open");
    document.querySelectorAll(`.icon-help-btn[data-help-target="${targetId}"]`).forEach(b => {
      b.classList.toggle("active", isOpen);
    });
  });
});

[1, 2].forEach(idx => {
  const modeBtn = document.getElementById(`modeToggleBtn${idx}`);
  const modesBox = document.getElementById(`modesBox${idx}`);
  if (modeBtn && modesBox) {
    modeBtn.addEventListener("click", () => {
      const isOpen = modesBox.classList.toggle("open");
      modeBtn.classList.toggle("active", isOpen);
    });
  }
  document.querySelectorAll(`input[name="mode${idx}"]`).forEach(radio => {
    radio.addEventListener("change", () => updateModeToggleLabel(idx));
  });
  const resetPBtn = document.getElementById(`resetP${idx}Btn`);
  if (resetPBtn) {
    resetPBtn.addEventListener("click", () => {
      document.getElementById(`fio${idx}`).value = "";
      document.getElementById(`birth${idx}_exact`).value = "";
      document.getElementById(`death${idx}_exact`).value = "";
      setRadioValue(`mode${idx}`, "prefix_default");
      validateSingleDateInput(`birth${idx}_exact`);
      validateSingleDateInput(`death${idx}_exact`);
    });
  }
});

(function initCemeteryCombobox() {
  const combo = document.getElementById("cemeteryCombo");
  const input = document.getElementById("cemeterySearchInput");
  const clearBtn = document.getElementById("cemeteryClearBtn");
  const dd = document.getElementById("cemeteryDropdown");

  function openDropdown(filterStr) {
    renderCemeteryDropdown(filterStr);
    dd.classList.add("open");
  }

  function closeDropdown() {
    dd.classList.remove("open");
    setCemeteryById(document.getElementById("cemetery_id").value);
  }

  input.addEventListener("focus", () => {
    if (!document.getElementById("cemetery_id").value) {
      input.value = "";
    } else {
      input.select();
    }
    openDropdown("");
  });

  input.addEventListener("click", () => {
    if (!dd.classList.contains("open")) {
      openDropdown("");
    }
  });

  input.addEventListener("input", () => {
    openDropdown(input.value);
  });

  input.addEventListener("keydown", (e) => {
    const opts = Array.from(dd.querySelectorAll(".cemetery-opt"));
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!dd.classList.contains("open")) openDropdown("");
      if (opts.length > 0) {
        cemHighlightIdx = Math.min(cemHighlightIdx + 1, opts.length - 1);
        opts.forEach((o, i) => o.classList.toggle("highlighted", i === cemHighlightIdx));
        opts[cemHighlightIdx].scrollIntoView({ block: "nearest" });
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (opts.length > 0) {
        cemHighlightIdx = Math.max(cemHighlightIdx - 1, 0);
        opts.forEach((o, i) => o.classList.toggle("highlighted", i === cemHighlightIdx));
        opts[cemHighlightIdx].scrollIntoView({ block: "nearest" });
      }
    } else if (e.key === "Enter" && dd.classList.contains("open")) {
      if (cemHighlightIdx >= 0 && cemHighlightIdx < opts.length) {
        e.preventDefault();
        const chosenId = opts[cemHighlightIdx].getAttribute("data-cem-id");
        setCemeteryById(chosenId);
        dd.classList.remove("open");
      } else if (opts.length === 1 && opts[0].hasAttribute("data-cem-id")) {
        e.preventDefault();
        setCemeteryById(opts[0].getAttribute("data-cem-id"));
        dd.classList.remove("open");
      }
    } else if (e.key === "Escape") {
      closeDropdown();
      input.blur();
    }
  });

  dd.addEventListener("mousedown", (e) => {
    const opt = e.target.closest(".cemetery-opt");
    if (!opt) return;
    e.preventDefault();
    setCemeteryById(opt.getAttribute("data-cem-id"));
    dd.classList.remove("open");
  });

  clearBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    setCemeteryById("");
    dd.classList.remove("open");
  });

  document.addEventListener("mousedown", (e) => {
    if (!combo.contains(e.target)) {
      if (dd.classList.contains("open")) {
        const rawVal = input.value.trim();
        if (!rawVal) {
          setCemeteryById("");
        }
        closeDropdown();
      }
    }
  });
})();

document.getElementById("resetPlotBtn").addEventListener("click", () => {
  setCemeteryById("");
  document.getElementById("place_number").value = "";
  document.getElementById("global_id").value = "";
});

document.getElementById("resetBtn").addEventListener("click", () => {
  document.getElementById("searchForm").reset();
  setCemeteryById("");
  clearDateErrors();
  updateModeToggleLabel(1);
  updateModeToggleLabel(2);
  document.querySelectorAll(".collapsible-hint.open, .modes-group.open").forEach(el => {
    el.classList.remove("open");
  });
  document.querySelectorAll(".icon-help-btn.active, .mode-toggle-btn.active").forEach(el => {
    el.classList.remove("active");
  });
  window.history.replaceState(null, "", window.location.pathname);
  document.getElementById("statusBar").style.display = "none";
  document.getElementById("statusBarBottom").style.display = "none";
  document.getElementById("errorBanner").style.display = "none";
  document.getElementById("resultsBody").innerHTML = `
    <tr>
      <td colspan="3" class="empty-state">
        Задайте параметры поиска (ФИО, даты, кладбище или <code>global_id</code>) и нажмите <b>Найти</b>.
      </td>
    </tr>`;
});

document.getElementById("exportCsvBtn").addEventListener("click", () => {
  if (!validateAllDateInputs()) return;
  const params = collectParams(0);
  params.set("limit", "1000");
  params.delete("offset");
  window.location.href = `/api/export.csv?${params.toString()}`;
});

document.getElementById("prevBtn").addEventListener("click", () => {
  if (currentOffset >= PAGE_LIMIT) {
    runSearch(currentOffset - PAGE_LIMIT);
  }
});

document.getElementById("nextBtn").addEventListener("click", () => {
  runSearch(currentOffset + PAGE_LIMIT);
});

document.getElementById("prevBtnBottom").addEventListener("click", () => {
  if (currentOffset >= PAGE_LIMIT) {
    runSearch(currentOffset - PAGE_LIMIT).then(() => {
      document.getElementById("statusBar").scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }
});

document.getElementById("nextBtnBottom").addEventListener("click", () => {
  runSearch(currentOffset + PAGE_LIMIT).then(() => {
    document.getElementById("statusBar").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
});

(function initTopExamplesBar() {
  const topBar = document.getElementById("topExamplesBar");
  const hideBtn = document.getElementById("hideExamplesBtn");
  try {
    if (localStorage.getItem("hideTopExamples") === "1" && topBar) {
      topBar.style.display = "none";
    }
  } catch (_) {}
  if (hideBtn && topBar) {
    hideBtn.addEventListener("click", () => {
      topBar.style.display = "none";
      try {
        localStorage.setItem("hideTopExamples", "1");
      } catch (_) {}
    });
  }
})();

document.querySelectorAll(".example-chip").forEach(btn => {
  btn.addEventListener("click", () => {
    document.getElementById("searchForm").reset();
    setCemeteryById("");
    clearDateErrors();
    updateModeToggleLabel(1);
    updateModeToggleLabel(2);
    const ex = btn.getAttribute("data-example");
    if (ex === "endings") {
      document.getElementById("fio1").value = "Красновский";
    } else if (ex === "quoted") {
      document.getElementById("fio1").value = '"Красновский"';
    } else if (ex === "patronymic-only") {
      document.getElementById("fio1").value = "Красновский Александрович";
    } else if (ex === "initials-dot") {
      document.getElementById("fio1").value = "Красновская Е. Г.";
    } else if (ex === "tilde-dollar") {
      document.getElementById("fio1").value = "Иван~$";
      setRadioValue("mode1", "prefix_no_surname");
    } else if (ex === "tilde-mid") {
      document.getElementById("fio1").value = "Куз~миченков";
    } else if (ex === "co-burial") {
      document.getElementById("fio1").value = "Введенский Михаил";
      document.getElementById("fio2").value = "Введенский Евгений";
    }
    const fromGuide = Boolean(btn.closest(".guide-examples"));
    runSearch(0).then(() => {
      if (fromGuide) {
        document.getElementById("statusBar").scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });
  });
});

document.getElementById("toggleImportBtn").addEventListener("click", () => {
  const panel = document.getElementById("importPanel");
  if (panel.classList.contains("open")) {
    setView("search");
  } else {
    setView("import");
    loadSources();
  }
});

document.getElementById("openInputDirBtn").addEventListener("click", async () => {
  try {
    const res = await fetch("/api/open-input-dir", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Не удалось открыть папку input/");
  } catch (err) {
    if (err instanceof TypeError) {
      showServerStoppedOverlay(false);
    } else {
      alert(err.message);
    }
  }
});

document.querySelectorAll(".btn-open-log-dir").forEach(btn => {
  btn.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/open-log-dir", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Не удалось открыть папку с логом");
    } catch (err) {
      if (err instanceof TypeError) {
        showServerStoppedOverlay(false);
      } else {
        alert(err.message);
      }
    }
  });
});

document.getElementById("reconnectBtn").addEventListener("click", () => {
  window.location.reload();
});

document.getElementById("shutdownBtn").addEventListener("click", async () => {
  if (isImportRunningNow) {
    const ok = window.confirm(
      "Сейчас выполняется импорт данных. Прервать импорт и завершить работу программы?"
    );
    if (!ok) return;
  }
  isManualShutdown = true;
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  if (importPollTimer) {
    clearInterval(importPollTimer);
    importPollTimer = null;
  }
  try {
    await fetch("/api/shutdown", { method: "POST" });
  } catch (_) {}
  showServerStoppedOverlay(true);
  try {
    window.close();
  } catch (_) {}
});

document.getElementById("clearDbBtn").addEventListener("click", async () => {
  const ok = window.confirm(
    "Очистить базу данных? Все импортированные участки и надписи будут удалены из базы (исходные файлы в папке input/ сохранятся)."
  );
  if (!ok) return;
  const btn = document.getElementById("clearDbBtn");
  btn.disabled = true;
  try {
    const res = await fetch("/api/db/clear", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Ошибка очистки базы данных");
    document.getElementById("importProgressBox").style.display = "none";
    document.getElementById("statusBar").style.display = "none";
    document.getElementById("resultsBody").innerHTML = `
      <tr>
        <td colspan="3" class="empty-state">
          Задайте параметры поиска (ФИО, даты, кладбище или <code>global_id</code>) и нажмите <b>Найти</b>.
        </td>
      </tr>`;
    await loadMeta();
    await loadSources();
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("uploadFileInput").addEventListener("change", (e) => {
  const label = document.getElementById("uploadFileNameLabel");
  if (e.target.files && e.target.files.length > 0) {
    label.textContent = e.target.files[0].name;
    label.style.color = "var(--fg)";
  } else {
    label.textContent = "Файл не выбран";
    label.style.color = "var(--muted-fg)";
  }
});

async function uploadFileAndImport(file) {
  setView("import");
  if (file.size === 0) {
    renderImportStatus({
      running: false,
      phase: "error",
      file_name: file.name,
      error: `Файл «${file.name}» пуст (0 байт). Скачайте полный файл выгрузки (.xlsx, .csv, .json или .xml).`
    });
    return;
  }
  const ext = (file.name.includes(".") ? file.name.split(".").pop() : "").toLowerCase();
  if (["zip", "rar", "7z", "gz", "tgz", "tar", "bz2", "xz"].includes(ext)) {
    renderImportStatus({
      running: false,
      phase: "error",
      file_name: file.name,
      error: `Файл «${file.name}» является архивом (.${ext}). Пожалуйста, сначала распакуйте архив на компьютере и выберите находящийся внутри файл (.xlsx, .csv, .json или .xml).`
    });
    return;
  }
  if (ext === "xls") {
    renderImportStatus({
      running: false,
      phase: "error",
      file_name: file.name,
      error: `Файл «${file.name}» имеет устаревший бинарный формат Excel 97–2003 (.xls). Пересохраните его в Excel в формате «Книга Excel (.xlsx)» или «CSV (разделители — запятые)».`
    });
    return;
  }
  if (ext === "pdf") {
    renderImportStatus({
      running: false,
      phase: "error",
      file_name: file.name,
      error: `Файл «${file.name}» является документом PDF, а не таблицей данных. Загрузите выгрузку в формате .xlsx, .csv, .json или .xml.`
    });
    return;
  }
  if (["html", "htm"].includes(ext)) {
    renderImportStatus({
      running: false,
      phase: "error",
      file_name: file.name,
      error: `Файл «${file.name}» является сохранённой веб-страницей (HTML), а не таблицей данных. Скачайте сам файл выгрузки (.xlsx, .csv, .json или .xml).`
    });
    return;
  }
  if (!["xlsx", "json", "csv", "xml"].includes(ext)) {
    renderImportStatus({
      running: false,
      phase: "error",
      file_name: file.name,
      error: `Формат файла «${file.name}» не поддерживается. Поддерживаются только файлы выгрузок .xlsx, .json, .csv или .xml.`
    });
    return;
  }
  const fileInput = document.getElementById("uploadFileInput");
  const fileLabel = document.getElementById("uploadFileNameLabel");
  const dateInput = document.getElementById("uploadSourceDate");
  const srcDate = dateInput.value.trim();
  const btn = document.getElementById("uploadAndImportBtn");
  fileLabel.textContent = file.name;
  fileLabel.style.color = "var(--fg)";
  btn.disabled = true;
  btn.textContent = "Загрузка...";

  renderImportStatus({
    running: true,
    phase: "uploading",
    file_name: file.name,
    bytes_received: 0,
    bytes_total: file.size
  });
  if (!importPollTimer) {
    importPollTimer = setInterval(pollImportStatus, 600);
  }

  try {
    const qs = new URLSearchParams({ filename: file.name });
    if (srcDate) qs.set("source_date", srcDate);
    const res = await fetch(`/api/import/upload?${qs.toString()}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Ошибка загрузки файла");
    fileInput.value = "";
    fileLabel.textContent = "Файл не выбран";
    fileLabel.style.color = "var(--muted-fg)";
    renderImportStatus(data);
    await loadSources();
  } catch (err) {
    if (importPollTimer) {
      clearInterval(importPollTimer);
      importPollTimer = null;
    }
    if (err instanceof TypeError) {
      showServerStoppedOverlay(false);
    } else {
      await loadSources();
      renderImportStatus({
        running: false,
        phase: "error",
        file_name: file.name,
        error: err.message
      });
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Загрузить и импортировать";
  }
}

document.getElementById("uploadAndImportBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("uploadFileInput");
  if (!fileInput.files || fileInput.files.length === 0) {
    renderImportStatus({
      running: false,
      phase: "error",
      file_name: "",
      error: "Пожалуйста, выберите файл на компьютере (.xlsx, .json, .csv или .xml) или перетащите его в окно браузера."
    });
    return;
  }
  await uploadFileAndImport(fileInput.files[0]);
});

let dragCounter = 0;
const dropOverlay = document.getElementById("dropOverlay");
const uploadDropZone = document.getElementById("uploadDropZone");

function hasDraggedFiles(e) {
  return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
}

window.addEventListener("dragenter", (e) => {
  if (!hasDraggedFiles(e)) return;
  e.preventDefault();
  dragCounter++;
  dropOverlay.classList.add("active");
  uploadDropZone.classList.add("drag-over");
});

window.addEventListener("dragover", (e) => {
  if (!hasDraggedFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
});

window.addEventListener("dragleave", (e) => {
  if (!hasDraggedFiles(e)) return;
  e.preventDefault();
  dragCounter = Math.max(0, dragCounter - 1);
  if (dragCounter === 0) {
    dropOverlay.classList.remove("active");
    uploadDropZone.classList.remove("drag-over");
  }
});

window.addEventListener("drop", async (e) => {
  if (!hasDraggedFiles(e)) return;
  e.preventDefault();
  dragCounter = 0;
  dropOverlay.classList.remove("active");
  uploadDropZone.classList.remove("drag-over");
  const files = e.dataTransfer.files;
  if (files && files.length > 0) {
    await uploadFileAndImport(files[0]);
  }
});

loadMeta().then(() => {
  const { hasFilter, initOffset } = restoreFormFromUrl();
  if (hasFilter && totalPlotsInDb > 0) {
    runSearch(initOffset);
  }
});
