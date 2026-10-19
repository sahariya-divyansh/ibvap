/**
 * IBVAP Frontend Application
 * Handles multi-camera grid, focus mode, stream lifecycle, and alert feed.
 * Vanilla JavaScript only. Zero innerHTML for server-supplied data.
 */
(function () {
  "use strict";

  const CAMERAS_POLL_MS = 2000;
  const STATS_POLL_MS = 2000;
  const EVENTS_POLL_MS = 2500;
  const OFFLINE_RETRY_MS = 5000;

  const ALARM_EVENT_TYPES = new Set([
    "VIRTUAL_FENCE_CROSS",
    "RESTRICTED_ZONE_ENTRY",
  ]);

  // Centralized application state
  const state = {
    cameras: [],
    focusedCameraId: null,
    selectedCameraId: null,
    isTabVisible: !document.hidden,
    isBackendReachable: true,
    lastError: null,
    renderedEvents: new Map(),
    renderedTiles: new Map(),
    initialPollComplete: false,
    audioContext: null,
    intervals: {
      cameras: null,
      stats: null,
      events: null,
      offlineRetry: null,
    },
  };

  // DOM element references
  const dom = {
    unreachableBanner: document.querySelector("#unreachable-banner"),
    unreachableText: document.querySelector("#unreachable-text"),
    mockBadge: document.querySelector("#mock-badge"),
    connectionStatus: document.querySelector("#connection-status"),
    alertCount: document.querySelector("#alert-count"),
    alarmBanner: document.querySelector("#alarm-banner"),
    personCount: document.querySelector("#person-count"),
    vehicleCount: document.querySelector("#vehicle-count"),
    animalCount: document.querySelector("#animal-count"),
    cameraGrid: document.querySelector("#camera-grid"),
    cameraCountBadge: document.querySelector("#camera-count-badge"),
    focusHint: document.querySelector("#focus-hint"),
    exitFocusBtn: document.querySelector("#exit-focus-btn"),
    alertFeed: document.querySelector("#alert-feed"),
    emptyState: document.querySelector("#empty-state"),
  };

  window.addEventListener("DOMContentLoaded", initApp);

  function initApp() {
    setupMockBadge();
    setupEventListeners();
    startPolling();
  }

  function setupMockBadge() {
    if (window.IBVAP_API && window.IBVAP_API.isMockMode()) {
      dom.mockBadge.hidden = false;
      const limit = window.IBVAP_API.getCamsLimit();
      if (limit !== null) {
        dom.mockBadge.textContent = `MOCK MODE (${limit} CAM${limit > 1 ? "S" : ""})`;
      } else {
        dom.mockBadge.textContent = "MOCK MODE";
      }
    }
  }

  function setupEventListeners() {
    document.addEventListener("visibilitychange", handleVisibilityChange);
    document.addEventListener("pointerdown", unlockAudio, { once: true });

    if (dom.exitFocusBtn) {
      dom.exitFocusBtn.addEventListener("click", exitFocusMode);
    }

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.focusedCameraId) {
        exitFocusMode();
      }
    });
  }

  function startPolling() {
    pollCameras();
    pollStats();
    pollEvents();

    state.intervals.cameras = setInterval(pollCameras, CAMERAS_POLL_MS);
    state.intervals.stats = setInterval(pollStats, STATS_POLL_MS);
    state.intervals.events = setInterval(pollEvents, EVENTS_POLL_MS);
    state.intervals.offlineRetry = setInterval(retryOfflineStreams, OFFLINE_RETRY_MS);
  }

  function stopPolling() {
    if (state.intervals.cameras) clearInterval(state.intervals.cameras);
    if (state.intervals.stats) clearInterval(state.intervals.stats);
    if (state.intervals.events) clearInterval(state.intervals.events);
    if (state.intervals.offlineRetry) clearInterval(state.intervals.offlineRetry);
    state.intervals.cameras = null;
    state.intervals.stats = null;
    state.intervals.events = null;
    state.intervals.offlineRetry = null;
  }

  function handleVisibilityChange() {
    state.isTabVisible = !document.hidden;

    if (document.hidden) {
      stopPolling();
      // Suspend all active stream images to free bandwidth and avoid stale connections
      state.renderedTiles.forEach((tile) => {
        if (tile.img && tile.img.src) {
          tile.img.dataset.cachedSrc = tile.img.src;
          tile.img.src = "";
        }
      });
    } else {
      // Resume streams for LIVE or DEGRADED cameras
      state.renderedTiles.forEach((tile, cameraId) => {
        const camData = state.cameras.find((c) => c.camera_id === cameraId);
        if (camData && camData.status !== "OFFLINE" && tile.img) {
          const restoredSrc = tile.img.dataset.cachedSrc || window.IBVAP_API.getVideoFeedUrl(cameraId, camData.location);
          tile.img.src = restoredSrc;
        }
      });
      startPolling();
    }
  }

  // --- Network Polling Functions ---

  async function pollCameras() {
    if (!state.isTabVisible) return;
    try {
      const cameras = await window.IBVAP_API.fetchCameras();
      setConnectionSuccess();
      state.cameras = cameras;
      renderCameraGrid(cameras);
    } catch (err) {
      setConnectionFailure(err);
    }
  }

  async function pollStats() {
    if (!state.isTabVisible) return;
    try {
      const stats = await window.IBVAP_API.fetchStats();
      dom.personCount.textContent = String(stats.person ?? 0);
      dom.vehicleCount.textContent = String(stats.vehicle ?? 0);
      dom.animalCount.textContent = String(stats.animal ?? 0);
    } catch (err) {
      // If cameras poll also failed, banner already shows error
      dom.personCount.textContent = "-";
      dom.vehicleCount.textContent = "-";
      dom.animalCount.textContent = "-";
    }
  }

  async function pollEvents() {
    if (!state.isTabVisible) return;
    try {
      const events = await window.IBVAP_API.fetchEvents();
      renderAlerts(events, state.initialPollComplete);
      state.initialPollComplete = true;
      updateSummary();
    } catch (err) {
      // Failure handled through setConnectionFailure if general network issue
    }
  }

  function setConnectionSuccess() {
    if (!state.isBackendReachable) {
      state.isBackendReachable = true;
      state.lastError = null;
    }
    dom.unreachableBanner.hidden = true;
    const modeLabel = window.IBVAP_API.isMockMode() ? "Mock Mode" : "Live";
    dom.connectionStatus.textContent = `${modeLabel} — synced ${new Date().toLocaleTimeString()}`;
  }

  function setConnectionFailure(err) {
    state.isBackendReachable = false;
    state.lastError = err;
    dom.unreachableBanner.hidden = false;

    if (window.IBVAP_API.isMockMode()) {
      dom.unreachableText.textContent = "Mock data loading failed (?fail=1)";
    } else {
      dom.unreachableText.textContent = "Backend unreachable — retrying";
    }
    dom.connectionStatus.textContent = `Error: ${err.message}`;
  }

  // --- Camera Grid & Tile Rendering ---

  function renderCameraGrid(cameras) {
    const cameraMap = new Map(cameras.map((c) => [c.camera_id, c]));
    dom.cameraGrid.dataset.count = String(cameras.length);
    dom.cameraCountBadge.textContent = `${cameras.length} Camera${cameras.length === 1 ? "" : "s"}`;

    // Remove tiles that are no longer in the camera list
    state.renderedTiles.forEach((tile, cameraId) => {
      if (!cameraMap.has(cameraId)) {
        tile.element.remove();
        state.renderedTiles.delete(cameraId);
      }
    });

    // Create or update tiles in-place (preserving DOM nodes and <img> streams)
    cameras.forEach((camera) => {
      let tile = state.renderedTiles.get(camera.camera_id);
      if (!tile) {
        tile = createCameraTile(camera);
        state.renderedTiles.set(camera.camera_id, tile);
        dom.cameraGrid.appendChild(tile.element);
      }
      updateCameraTile(tile, camera);
    });

    updateFocusModeLayout();
  }

  function createCameraTile(camera) {
    const article = document.createElement("article");
    article.className = "camera-tile";
    article.dataset.cameraId = camera.camera_id;
    article.tabIndex = 0;
    article.setAttribute("role", "button");
    article.setAttribute("aria-label", `Camera ${camera.camera_id} at ${camera.location}. Click to focus.`);

    // Header
    const header = document.createElement("div");
    header.className = "tile-header";

    const titleGroup = document.createElement("div");
    titleGroup.className = "tile-title-group";

    const idSpan = document.createElement("span");
    idSpan.className = "tile-camera-id";
    idSpan.textContent = camera.camera_id;

    const locSpan = document.createElement("span");
    locSpan.className = "tile-location";
    locSpan.textContent = camera.location;

    titleGroup.append(idSpan, locSpan);

    const statusBadge = document.createElement("span");
    statusBadge.className = "status-badge";

    header.append(titleGroup, statusBadge);

    // Video Container
    const videoContainer = document.createElement("div");
    videoContainer.className = "tile-video";

    const img = document.createElement("img");
    img.className = "camera-stream";
    img.alt = `Live stream for ${camera.camera_id}`;
    if (camera.status !== "OFFLINE" && state.isTabVisible) {
      img.src = window.IBVAP_API.getVideoFeedUrl(camera.camera_id, camera.location);
    }

    const lowVisBadge = document.createElement("span");
    lowVisBadge.className = "badge-low-vis";
    lowVisBadge.textContent = "LOW VISIBILITY";
    lowVisBadge.hidden = true;

    // Offline Overlay
    const offlineOverlay = document.createElement("div");
    offlineOverlay.className = "tile-offline-overlay";
    offlineOverlay.hidden = true;

    const offlineIcon = document.createElement("span");
    offlineIcon.className = "offline-icon";
    offlineIcon.textContent = "✕";

    const offlineMsg = document.createElement("span");
    offlineMsg.className = "offline-msg";
    offlineMsg.textContent = "No signal";

    const offlineSub = document.createElement("span");
    offlineSub.className = "offline-sub";
    offlineSub.textContent = "Auto-reconnecting every 5s";

    offlineOverlay.append(offlineIcon, offlineMsg, offlineSub);
    videoContainer.append(img, lowVisBadge, offlineOverlay);

    // Footer Metrics
    const footer = document.createElement("div");
    footer.className = "tile-footer";

    const metricsGroup = document.createElement("div");
    metricsGroup.className = "tile-metrics";

    const fpsSpan = document.createElement("span");
    fpsSpan.className = "metric-item";

    const latencySpan = document.createElement("span");
    latencySpan.className = "metric-item";

    metricsGroup.append(fpsSpan, latencySpan);

    const countsGroup = document.createElement("div");
    countsGroup.className = "tile-counts";

    footer.append(metricsGroup, countsGroup);
    article.append(header, videoContainer, footer);

    // Click and Keyboard handlers for Focus Mode
    article.addEventListener("click", () => toggleFocusMode(camera.camera_id));
    article.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleFocusMode(camera.camera_id);
      }
    });

    return {
      element: article,
      img,
      locSpan,
      statusBadge,
      lowVisBadge,
      offlineOverlay,
      offlineMsg,
      fpsSpan,
      latencySpan,
      countsGroup,
    };
  }

  function updateCameraTile(tile, camera) {
    // 1. Text updates
    tile.locSpan.textContent = camera.location;

    // 2. Status badge (Text + Color)
    const statusClass = `status-${camera.status.toLowerCase()}`;
    tile.statusBadge.className = `status-badge ${statusClass}`;
    tile.statusBadge.textContent = camera.status;

    // 3. Low visibility flag
    tile.lowVisBadge.hidden = !camera.low_visibility;

    // 4. Offline state handling
    const isOffline = camera.status === "OFFLINE";
    tile.offlineOverlay.hidden = !isOffline;

    if (isOffline) {
      const age = Math.round(camera.last_frame_age_s || 0);
      tile.offlineMsg.textContent = `No signal — last seen ${age}s ago`;
      if (tile.img.src) {
        tile.img.dataset.cachedSrc = tile.img.src;
        tile.img.src = "";
      }
    } else {
      // Reattach stream if it was empty and tab is visible
      if (!tile.img.src && state.isTabVisible) {
        tile.img.src = tile.img.dataset.cachedSrc || window.IBVAP_API.getVideoFeedUrl(camera.camera_id, camera.location);
      }
    }

    // 5. Metrics
    tile.fpsSpan.textContent = `${Number(camera.fps || 0).toFixed(1)} fps`;
    tile.latencySpan.textContent = `${Math.round(camera.latency_ms || 0)} ms`;

    // 6. Counts (Person, Vehicle, Animal)
    const p = camera.counts?.person ?? 0;
    const v = camera.counts?.vehicle ?? 0;
    const a = camera.counts?.animal ?? 0;
    tile.countsGroup.textContent = `P: ${p} · V: ${v} · A: ${a}`;
  }

  function retryOfflineStreams() {
    if (!state.isTabVisible || window.IBVAP_API.isMockMode()) return;
    state.cameras.forEach((camera) => {
      if (camera.status === "OFFLINE") {
        const tile = state.renderedTiles.get(camera.camera_id);
        if (tile && tile.img && !tile.img.src) {
          // Probe with cache-busting timestamp
          tile.img.src = `${window.IBVAP_API.getVideoFeedUrl(camera.camera_id)}?t=${Date.now()}`;
        }
      }
    });
  }

  // --- Focus Mode ---

  function toggleFocusMode(cameraId) {
    if (state.focusedCameraId === cameraId) {
      exitFocusMode();
    } else {
      enterFocusMode(cameraId);
    }
  }

  function enterFocusMode(cameraId) {
    state.focusedCameraId = cameraId;
    dom.focusHint.hidden = false;
    updateFocusModeLayout();
  }

  function exitFocusMode() {
    state.focusedCameraId = null;
    dom.focusHint.hidden = true;
    updateFocusModeLayout();
  }

  function updateFocusModeLayout() {
    if (state.focusedCameraId) {
      dom.cameraGrid.classList.add("has-focus");
      state.renderedTiles.forEach((tile, id) => {
        if (id === state.focusedCameraId) {
          tile.element.classList.add("is-focused");
          tile.element.setAttribute("aria-label", `Camera ${id} (Focused). Press Escape to exit.`);
        } else {
          tile.element.classList.remove("is-focused");
          tile.element.setAttribute("aria-label", `Camera ${id}. Click to switch focus.`);
        }
      });
    } else {
      dom.cameraGrid.classList.remove("has-focus");
      state.renderedTiles.forEach((tile, id) => {
        tile.element.classList.remove("is-focused");
        tile.element.setAttribute("aria-label", `Camera ${id}. Click to focus.`);
      });
    }
  }

  // --- Alert Feed & Events (Safe DOM Rendering) ---

  function renderAlerts(events, shouldAlarm) {
    for (const event of [...events].reverse()) {
      const existing = state.renderedEvents.get(event.event_id);
      if (existing) {
        syncAckState(existing, event);
        continue;
      }

      const element = createAlertElement(event);
      state.renderedEvents.set(event.event_id, element);
      dom.alertFeed.prepend(element);

      if (shouldAlarm && ALARM_EVENT_TYPES.has(event.event_type)) {
        triggerAlarm();
      }
    }
  }

  function createAlertElement(event) {
    const article = document.createElement("article");
    const sev = (event.severity || "MEDIUM").toLowerCase();
    article.className = `alert severity-${sev}`;
    article.dataset.eventId = event.event_id;
    article.dataset.ackStatus = event.ack_status || "pending";

    const body = document.createElement("div");
    body.className = "alert-body";

    // Heading
    const heading = document.createElement("div");
    heading.className = "alert-heading";

    const typeBadge = document.createElement("span");
    typeBadge.className = "event-type-badge";
    typeBadge.textContent = event.event_type;

    const ackBadge = document.createElement("span");
    ackBadge.className = "ack-badge";
    ackBadge.textContent = event.ack_status || "pending";

    heading.append(typeBadge, ackBadge);

    // Details List
    const details = document.createElement("dl");
    details.className = "details";
    addDetail(details, "Object", event.object_type || "N/A");
    addDetail(details, "Track", event.track_id !== null ? `#${event.track_id}` : "N/A");
    addDetail(details, "Confidence", Number(event.confidence || 0).toFixed(2));
    addDetail(details, "Camera", event.camera_id);
    addDetail(details, "Time", new Date(event.timestamp).toLocaleTimeString());

    // Actions
    const actions = document.createElement("div");
    actions.className = "actions";
    actions.append(
      createAckButton("Confirm", "confirmed", event.event_id),
      createAckButton("Reject", "rejected", event.event_id)
    );

    body.append(heading, details, actions);

    // Evidence
    let evidenceSrc = event.evidence_url || (event.evidence_path ? `/evidence/${encodeURIComponent(event.evidence_path.split(/[\\/]/).pop())}` : null);
    if (evidenceSrc && evidenceSrc.startsWith("/evidence/")) {
      evidenceSrc = `${window.IBVAP_API.getApiBase()}${evidenceSrc}`;
    }
    if (evidenceSrc) {
      const evidence = document.createElement("img");
      evidence.className = "evidence";
      evidence.src = evidenceSrc;
      evidence.alt = `Evidence for ${event.event_type}`;
      evidence.addEventListener("error", () => {
        evidence.classList.add("missing");
        evidence.alt = "Evidence image unavailable";
      });
      article.append(evidence, body);
    } else {
      article.classList.add("no-evidence");
      article.append(body);
    }

    if ((event.ack_status || "pending") !== "pending") {
      markHandled(article, event.ack_status);
    }

    return article;
  }

  function addDetail(details, label, value) {
    const wrapper = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value;
    wrapper.append(term, description);
    details.append(wrapper);
  }

  function createAckButton(label, status, eventId) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.action = status;
    button.textContent = label;
    button.addEventListener("click", () => acknowledge(eventId, status));
    return button;
  }

  async function acknowledge(eventId, status) {
    const element = state.renderedEvents.get(eventId);
    if (!element) return;

    setButtonsDisabled(element, true);
    try {
      const updated = await window.IBVAP_API.acknowledgeEvent(eventId, status);
      markHandled(element, updated.ack_status);
    } catch (error) {
      dom.connectionStatus.textContent = `Ack failed: ${error.message}`;
      setButtonsDisabled(element, false);
    }
  }

  function syncAckState(element, event) {
    const status = event.ack_status || "pending";
    if (status !== "pending" && element.dataset.ackStatus !== status) {
      markHandled(element, status);
    }
  }

  function markHandled(element, status) {
    element.dataset.ackStatus = status;
    element.classList.add("handled");
    element.querySelector(".ack-badge").textContent = status;

    const actions = element.querySelector(".actions");
    actions.replaceChildren(); // Safe DOM clear
    const label = document.createElement("span");
    label.className = "handled-label";
    label.textContent = status;
    actions.appendChild(label);

    updateSummary();
  }

  function setButtonsDisabled(element, disabled) {
    element.querySelectorAll("button").forEach((btn) => {
      btn.disabled = disabled;
    });
  }

  function updateSummary() {
    const activeCount = [...state.renderedEvents.values()].filter(
      (el) => el.dataset.ackStatus === "pending"
    ).length;
    dom.alertCount.textContent = String(activeCount);
    dom.emptyState.hidden = state.renderedEvents.size > 0;
  }

  // --- Audio Alarm ---

  function triggerAlarm() {
    playAlarmBeep();
    dom.alarmBanner.hidden = false;
    dom.alarmBanner.classList.remove("flash");
    void dom.alarmBanner.offsetWidth; // Trigger reflow
    dom.alarmBanner.classList.add("flash");
    window.setTimeout(() => {
      dom.alarmBanner.hidden = true;
    }, 1800);
  }

  function playAlarmBeep() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;

    state.audioContext = state.audioContext || new AudioCtx();
    if (state.audioContext.state === "suspended") {
      state.audioContext.resume();
    }
    const osc = state.audioContext.createOscillator();
    const gain = state.audioContext.createGain();
    osc.type = "square";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, state.audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.18, state.audioContext.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, state.audioContext.currentTime + 0.22);
    osc.connect(gain);
    gain.connect(state.audioContext.destination);
    osc.start();
    osc.stop(state.audioContext.currentTime + 0.24);
  }

  function unlockAudio() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    state.audioContext = state.audioContext || new AudioCtx();
    if (state.audioContext.state === "suspended") {
      state.audioContext.resume();
    }
  }
})();
