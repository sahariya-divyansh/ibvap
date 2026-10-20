/**
 * IBVAP Frontend Application
 * Handles multi-camera grid, focus mode, stream lifecycle, and alert feed with dismiss/reviewed toggling.
 * Vanilla JavaScript only. Zero innerHTML for server-supplied data.
 */
(function () {
  "use strict";

  const CAMERAS_POLL_MS = 2000;
  const STATS_POLL_MS = 2000;
  const EVENTS_POLL_MS = 2500;
  const OFFLINE_RETRY_MS = 5000;
  const STORAGE_KEY_SHOW_REVIEWED = "ibvap_show_reviewed";

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
    renderedTiles: new Map(), // cameraId -> tile object
    initialPollComplete: false,
    audioContext: null,
    soundEnabled: true,
    playedAlarmEventIds: new Set(),
    activeModalEvent: null,
    filters: {
      camera: "",
      type: "",
      severities: new Set(["HIGH", "MEDIUM", "LOW"]),
      status: "",
      timeRange: "all",
      search: "",
      searchDebounceTimer: null,
    },
    intervals: {
      cameras: null,
      stats: null,
      events: null,
      offlineRetry: null,
    },
  };

  try {
    state.soundEnabled = localStorage.getItem("ibvap_sound_enabled") !== "false";
  } catch (_) {
    state.soundEnabled = true;
  }

  // Alert tracking & reviewed state (from Divyansh's hide-reviewed feature)
  let latestEvents = [];
  const dismissedIds = new Set();
  const renderedPending = new Map();
  const renderedReviewed = new Map();
  let cachedReviewedCount = 0;
  let showReviewed = false;

  try {
    showReviewed = localStorage.getItem(STORAGE_KEY_SHOW_REVIEWED) === "true";
  } catch (_) {
    showReviewed = false;
  }

  // DOM element references
  const dom = {
    unreachableBanner: document.querySelector("#unreachable-banner"),
    unreachableText: document.querySelector("#unreachable-text"),
    mockBadge: document.querySelector("#mock-badge"),
    connectionStatus: document.querySelector("#connection-status"),
    alertCount: document.querySelector("#alert-count"),
    alarmBanner: document.querySelector("#alarm-banner"),
    reviewedToggle: document.querySelector("#reviewed-toggle"),
    personCount: document.querySelector("#person-count"),
    vehicleCount: document.querySelector("#vehicle-count"),
    animalCount: document.querySelector("#animal-count"),
    cameraGrid: document.querySelector("#camera-grid"),
    cameraCountBadge: document.querySelector("#camera-count-badge"),
    focusHint: document.querySelector("#focus-hint"),
    exitFocusBtn: document.querySelector("#exit-focus-btn"),
    alertFeed: document.querySelector("#alert-feed"),
    emptyState: document.querySelector("#empty-state"),
    reviewedFeed: document.querySelector("#reviewed-feed"),

    // Sound toggle
    soundAlarmToggle: document.querySelector("#sound-alarm-toggle"),
    soundToggleIcon: document.querySelector("#sound-toggle-icon"),
    soundToggleText: document.querySelector("#sound-toggle-text"),

    // Filter controls
    filterCameraSelect: document.querySelector("#filter-camera-select"),
    filterTypeSelect: document.querySelector("#filter-type-select"),
    filterAckSelect: document.querySelector("#filter-ack-select"),
    filterTimerangeSelect: document.querySelector("#filter-timerange-select"),
    filterSearchInput: document.querySelector("#filter-search-input"),
    filterSevHigh: document.querySelector("#filter-sev-high"),
    filterSevMedium: document.querySelector("#filter-sev-medium"),
    filterSevLow: document.querySelector("#filter-sev-low"),
    filterCount: document.querySelector("#filter-count"),
    clearFiltersBtn: document.querySelector("#clear-filters-btn"),
    exportCsvBtn: document.querySelector("#export-csv-btn"),
    printReportBtn: document.querySelector("#print-report-btn"),

    // Analytics canvases
    chartHourlyCanvas: document.querySelector("#chart-hourly-canvas"),
    chartSeverityCanvas: document.querySelector("#chart-severity-canvas"),
    chartStatusCanvas: document.querySelector("#chart-status-canvas"),
    hourlyChartSummary: document.querySelector("#hourly-chart-summary"),

    // Evidence modal
    evidenceModal: document.querySelector("#evidence-modal"),
    modalTitle: document.querySelector("#modal-title"),
    modalCloseBtn: document.querySelector("#modal-close-btn"),
    modalImg: document.querySelector("#modal-img"),
    modalVideo: document.querySelector("#modal-video"),
    modalNoMedia: document.querySelector("#modal-no-media"),
    modalDetailsGrid: document.querySelector("#modal-details-grid"),
    modalConfirmBtn: document.querySelector("#modal-confirm-btn"),
    modalRejectBtn: document.querySelector("#modal-reject-btn"),
    modalDismissBtn: document.querySelector("#modal-dismiss-btn"),

    // Print headers
    printTimestamp: document.querySelector("#print-timestamp"),
    printScope: document.querySelector("#print-scope"),
    printCount: document.querySelector("#print-count"),
  };

  window.addEventListener("DOMContentLoaded", initApp);

  function initApp() {
    setupMockBadge();
    setupSoundToggle();
    setupReviewedToggle();
    setupFilters();
    setupEvidenceModal();
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

  function setupSoundToggle() {
    if (!dom.soundAlarmToggle) return;
    updateSoundToggleUI();
    dom.soundAlarmToggle.addEventListener("click", () => {
      state.soundEnabled = !state.soundEnabled;
      try {
        localStorage.setItem("ibvap_sound_enabled", String(state.soundEnabled));
      } catch (_) {}
      updateSoundToggleUI();
      if (state.soundEnabled) {
        playAlarmBeep(); // Short test chirp
      }
    });
  }

  function updateSoundToggleUI() {
    if (!dom.soundAlarmToggle) return;
    dom.soundAlarmToggle.setAttribute("aria-pressed", String(state.soundEnabled));
    if (state.soundEnabled) {
      dom.soundAlarmToggle.classList.add("active");
      if (dom.soundToggleIcon) dom.soundToggleIcon.textContent = "\uD83D\uDD0A";
      if (dom.soundToggleText) dom.soundToggleText.textContent = "Sound: ON";
    } else {
      dom.soundAlarmToggle.classList.remove("active");
      if (dom.soundToggleIcon) dom.soundToggleIcon.textContent = "\uD83D\uDD07";
      if (dom.soundToggleText) dom.soundToggleText.textContent = "Sound: OFF";
    }
  }

  function setupReviewedToggle() {
    if (dom.reviewedToggle) {
      dom.reviewedToggle.addEventListener("click", toggleReviewed);
      updateReviewedToggleUI(0);
    }
    if (dom.reviewedFeed) {
      dom.reviewedFeed.hidden = !showReviewed;
    }
  }

  function setupEventListeners() {
    document.addEventListener("visibilitychange", handleVisibilityChange);
    document.addEventListener("pointerdown", unlockAudio, { once: true });

    if (dom.exitFocusBtn) {
      dom.exitFocusBtn.addEventListener("click", exitFocusMode);
    }

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (state.activeModalEvent) {
          closeEvidenceModal();
        } else if (state.focusedCameraId) {
          exitFocusMode();
        }
      }
    });

    window.addEventListener("hashchange", handleHashChange);
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
      populateFilterCameraOptions(cameras);
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
    } catch (_) {
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
      renderAnalyticsCharts(events);
      state.initialPollComplete = true;
    } catch (_) {
      // Handled through setConnectionFailure if general network issue
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
    if (dom.filterCameraSelect) {
      dom.filterCameraSelect.value = cameraId;
      state.filters.camera = cameraId;
      syncFiltersToHash();
      if (latestEvents.length > 0) {
        renderAlerts(latestEvents, false);
      }
    }
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

  // --- Reviewed Toggle Functionality (Divyansh) ---

  function toggleReviewed() {
    showReviewed = !showReviewed;
    try {
      localStorage.setItem(STORAGE_KEY_SHOW_REVIEWED, String(showReviewed));
    } catch (_) {}

    if (dom.reviewedFeed) {
      dom.reviewedFeed.hidden = !showReviewed;
    }

    if (showReviewed) {
      populateReviewedFeed();
    } else {
      clearReviewedFeed();
    }

    const reviewedCount = latestEvents.filter(
      (e) => (e.ack_status && e.ack_status !== "pending") || dismissedIds.has(e.event_id)
    ).length;
    updateReviewedToggleUI(reviewedCount);
  }

  function updateReviewedToggleUI(count) {
    if (!dom.reviewedToggle) return;
    if (typeof count === "number") {
      cachedReviewedCount = count;
    }
    const n = cachedReviewedCount;
    dom.reviewedToggle.setAttribute("aria-pressed", String(showReviewed));
    dom.reviewedToggle.classList.toggle("active", showReviewed);
    dom.reviewedToggle.textContent = showReviewed
      ? `Hide reviewed (${n})`
      : `Show reviewed (${n})`;
  }

  function populateReviewedFeed() {
    if (!dom.reviewedFeed) return;
    const reviewedEvents = latestEvents.filter(
      (e) => (e.ack_status && e.ack_status !== "pending") || dismissedIds.has(e.event_id)
    );
    for (const event of [...reviewedEvents].reverse()) {
      if (!renderedReviewed.has(event.event_id)) {
        const effectiveStatus = dismissedIds.has(event.event_id)
          ? (event.ack_status !== "pending" ? event.ack_status : "confirmed")
          : event.ack_status;
        const element = createAlertElement({ ...event, ack_status: effectiveStatus }, true);
        renderedReviewed.set(event.event_id, element);
        dom.reviewedFeed.prepend(element);
      }
    }
  }

  function clearReviewedFeed() {
    for (const element of renderedReviewed.values()) {
      element.remove();
    }
    renderedReviewed.clear();
  }

  function addReviewedCard(eventId, status, cached, originalElement) {
    if (renderedReviewed.has(eventId)) return;

    let eventData = cached;
    if (!eventData && originalElement) {
      eventData = {
        event_id: eventId,
        event_type: originalElement.querySelector(".event-type-badge")?.textContent || "EVENT",
        ack_status: status,
        object_type: originalElement.querySelector(".details dd")?.textContent || "unknown",
        track_id: 0,
        confidence: 1.0,
        camera_id: "cam_01",
        timestamp: new Date().toISOString(),
        evidence_path: "",
      };
    }

    if (eventData) {
      const reviewedCard = createAlertElement({ ...eventData, ack_status: status }, true);
      renderedReviewed.set(eventId, reviewedCard);
      if (dom.reviewedFeed) {
        dom.reviewedFeed.prepend(reviewedCard);
      }
    }
  }

  function syncReviewedState(element, status) {
    element.dataset.ackStatus = status;
    const badge = element.querySelector(".ack-badge");
    if (badge) {
      badge.textContent = status;
    }
    const label = element.querySelector(".handled-label");
    if (label) {
      label.textContent = status;
    }
  }

  // --- Alert Feed & Events (Safe DOM Rendering) ---

  function resolveCameraLocation(cameraId) {
    if (!cameraId) return "Unknown";
    const cam = state.cameras.find((c) => c.camera_id === cameraId);
    return cam ? `${cam.location} (${cam.camera_id})` : cameraId;
  }

  function matchesFilter(event) {
    // 1. Camera filter
    if (state.filters.camera && event.camera_id !== state.filters.camera) {
      return false;
    }

    // 2. Event type filter
    if (state.filters.type && event.event_type !== state.filters.type) {
      return false;
    }

    // 3. Severity filter
    const sev = (event.severity || "MEDIUM").toUpperCase();
    if (!state.filters.severities.has(sev)) {
      return false;
    }

    // 4. Ack status filter
    const status = event.ack_status || "pending";
    if (state.filters.status && status !== state.filters.status) {
      return false;
    }

    // 5. Time range filter
    if (state.filters.timeRange && state.filters.timeRange !== "all") {
      const eventTime = new Date(event.timestamp).getTime();
      const now = Date.now();
      if (state.filters.timeRange === "15m" && now - eventTime > 15 * 60 * 1000) {
        return false;
      }
      if (state.filters.timeRange === "1h" && now - eventTime > 60 * 60 * 1000) {
        return false;
      }
      if (state.filters.timeRange === "today") {
        const dEvent = new Date(event.timestamp).toDateString();
        const dNow = new Date().toDateString();
        if (dEvent !== dNow) {
          return false;
        }
      }
    }

    // 6. Free-text search filter (debounced)
    if (state.filters.search) {
      const q = state.filters.search.toLowerCase();
      const haystack = [
        event.event_id,
        event.event_type,
        event.object_type,
        event.track_id !== null && event.track_id !== undefined ? String(event.track_id) : "",
        event.camera_id,
        resolveCameraLocation(event.camera_id),
        JSON.stringify(event.extra || {}),
      ]
        .join(" ")
        .toLowerCase();

      if (!haystack.includes(q)) {
        return false;
      }
    }

    return true;
  }

  function renderAlerts(events, shouldAlarm) {
    latestEvents = events;
    const filteredEvents = events.filter(matchesFilter);

    if (dom.filterCount) {
      dom.filterCount.textContent = `Showing ${filteredEvents.length} of ${events.length} alerts`;
    }

    let reviewedCount = 0;

    for (const event of [...filteredEvents].reverse()) {
      const isPending = (event.ack_status || "pending") === "pending" && !dismissedIds.has(event.event_id);

      if (isPending) {
        const existing = renderedPending.get(event.event_id);
        if (!existing) {
          const element = createAlertElement(event, false);
          renderedPending.set(event.event_id, element);
          dom.alertFeed.prepend(element);

          // Trigger audible and visual alarm on new unreviewed high-severity events
          if (shouldAlarm && event.severity === "HIGH") {
            if (!state.playedAlarmEventIds.has(event.event_id)) {
              state.playedAlarmEventIds.add(event.event_id);
              triggerAlarm();
            }
          }
        }
      } else {
        reviewedCount++;
        if (showReviewed && dom.reviewedFeed) {
          const effectiveStatus = dismissedIds.has(event.event_id)
            ? (event.ack_status !== "pending" ? event.ack_status : "confirmed")
            : event.ack_status;
          const existing = renderedReviewed.get(event.event_id);
          if (existing) {
            syncReviewedState(existing, effectiveStatus);
          } else {
            const element = createAlertElement({ ...event, ack_status: effectiveStatus }, true);
            renderedReviewed.set(event.event_id, element);
            dom.reviewedFeed.prepend(element);
          }
        }
      }
    }

    // Prune pending cards that either no longer match filters or no longer exist on server
    for (const [id, element] of renderedPending.entries()) {
      if (!filteredEvents.some((e) => e.event_id === id)) {
        element.remove();
        renderedPending.delete(id);
      }
    }

    // Prune reviewed cards if needed
    for (const [id, element] of renderedReviewed.entries()) {
      if (!filteredEvents.some((e) => e.event_id === id) && !dismissedIds.has(id)) {
        element.remove();
        renderedReviewed.delete(id);
      }
    }

    updateSummary();
    updateReviewedToggleUI(reviewedCount);
  }

  function getEvidenceUrl(event) {
    if (event.evidence_url) {
      return event.evidence_url.startsWith("http")
        ? event.evidence_url
        : `${window.IBVAP_API.getApiBase()}${event.evidence_url}`;
    }
    if (event.evidence_path) {
      const filename = encodeURIComponent(event.evidence_path.split(/[\\/]/).pop());
      return `${window.IBVAP_API.getApiBase()}/evidence/${filename}`;
    }
    return null;
  }

  function createAlertElement(event, isReviewed = false) {
    const article = document.createElement("article");
    const sev = (event.severity || "MEDIUM").toLowerCase();
    article.className = `alert severity-${sev}`;
    article.dataset.eventId = event.event_id;
    const status = event.ack_status || "pending";
    article.dataset.ackStatus = status;

    // Compact mode for routine object detections
    if (event.event_type === "NEW_OBJECT_DETECTED") {
      article.classList.add("compact");
    }

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
    ackBadge.textContent = status;

    heading.append(typeBadge, ackBadge);

    // Details List
    const details = document.createElement("dl");
    details.className = "details";
    addDetail(details, "Object", event.object_type || "N/A");
    addDetail(details, "Track", event.track_id !== null && event.track_id !== undefined ? `#${event.track_id}` : "N/A");
    addDetail(details, "Confidence", Number(event.confidence || 0).toFixed(2));
    addDetail(details, "Camera", resolveCameraLocation(event.camera_id));
    addDetail(details, "Time", event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : "--:--");

    // Extra Telemetry Tags (Task H3)
    const metaTags = document.createElement("div");
    metaTags.className = "alert-meta-tags";
    if (event.extra) {
      if (event.extra.dwell_seconds !== undefined) {
        const tag = document.createElement("span");
        tag.className = "meta-tag";
        tag.textContent = `Dwell: ${event.extra.dwell_seconds}s`;
        metaTags.appendChild(tag);
      }
      if (event.extra.direction) {
        const tag = document.createElement("span");
        tag.className = "meta-tag";
        tag.textContent = `Dir: ${event.extra.direction}`;
        metaTags.appendChild(tag);
      }
      if (event.extra.kind) {
        const tag = document.createElement("span");
        tag.className = "meta-tag";
        tag.textContent = `Tamper: ${event.extra.kind}`;
        metaTags.appendChild(tag);
      }
      if (event.extra.repeat_count) {
        const tag = document.createElement("span");
        tag.className = "meta-tag";
        tag.textContent = `Seq: ${event.extra.repeat_count}x`;
        metaTags.appendChild(tag);
      }
    }

    // Actions
    const actions = document.createElement("div");
    actions.className = "actions";

    const inspectBtn = document.createElement("button");
    inspectBtn.type = "button";
    inspectBtn.className = "btn-subtle btn-evidence";
    inspectBtn.textContent = "Inspect";
    inspectBtn.title = "View evidence snapshot, clip and full details";
    inspectBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openEvidenceModal(event);
    });
    actions.append(inspectBtn);

    if (!isReviewed && status === "pending") {
      actions.append(
        createAckButton("Confirm", "confirmed", event.event_id),
        createAckButton("Reject", "rejected", event.event_id)
      );
    } else {
      const label = document.createElement("span");
      label.className = "handled-label";
      label.textContent = status;
      actions.append(label);
    }

    body.append(heading, details);
    if (metaTags.children.length > 0) {
      body.append(metaTags);
    }
    body.append(actions);

    // Evidence thumbnail
    const evidenceSrc = getEvidenceUrl(event);
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

    if (isReviewed || status !== "pending") {
      article.classList.add("handled");
    }

    // Click anywhere on card (except buttons) to open modal
    article.addEventListener("click", () => openEvidenceModal(event));

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
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      acknowledge(eventId, status);
    });
    return button;
  }

  async function acknowledge(eventId, status) {
    const element = renderedPending.get(eventId);
    if (element) {
      setButtonsDisabled(element, true);
      const existingError = element.querySelector(".inline-error");
      if (existingError) {
        existingError.remove();
      }
    }

    try {
      const updatedEvent = await window.IBVAP_API.acknowledgeEvent(eventId, status);
      const finalStatus = updatedEvent.ack_status || status;
      dismissedIds.add(eventId);

      const cached = latestEvents.find((e) => e.event_id === eventId);
      if (cached) {
        cached.ack_status = finalStatus;
      }

      // Update active modal if showing this event
      if (state.activeModalEvent && state.activeModalEvent.event_id === eventId) {
        state.activeModalEvent.ack_status = finalStatus;
        if (dom.modalConfirmBtn) dom.modalConfirmBtn.hidden = true;
        if (dom.modalRejectBtn) dom.modalRejectBtn.hidden = true;
      }

      // Immediately remove from pending tracking and update counter
      if (element) {
        renderedPending.delete(eventId);
        updateSummary();

        // Trigger collapse/fade animation (~250 ms)
        element.style.maxHeight = `${element.offsetHeight}px`;
        void element.offsetHeight;
        element.classList.add("dismissing");

        setTimeout(() => {
          element.remove();
          if (showReviewed && dom.reviewedFeed) {
            addReviewedCard(eventId, finalStatus, cached, element);
          }
        }, 250);
      }

      const reviewedCount = latestEvents.filter(
        (e) => (e.ack_status && e.ack_status !== "pending") || dismissedIds.has(e.event_id)
      ).length;
      updateReviewedToggleUI(reviewedCount);

      // Refresh charts
      renderAnalyticsCharts(latestEvents);
    } catch (error) {
      dom.connectionStatus.textContent = `Ack failed: ${error.message}`;
      if (element) {
        setButtonsDisabled(element, false);
        const actions = element.querySelector(".actions");
        if (actions) {
          const errorMsg = document.createElement("span");
          errorMsg.className = "inline-error";
          errorMsg.textContent = "Could not save, try again";
          actions.append(errorMsg);
          setTimeout(() => errorMsg.remove(), 4000);
        }
      }
    }
  }

  function setButtonsDisabled(element, disabled) {
    element.querySelectorAll("button").forEach((btn) => {
      btn.disabled = disabled;
    });
  }

  function updateSummary() {
    const activeCount = renderedPending.size;
    dom.alertCount.textContent = String(activeCount);
    dom.emptyState.hidden = activeCount > 0;
  }

  // --- Audio Alarm (Task H3) ---

  function triggerAlarm() {
    if (state.soundEnabled) {
      playAlarmBeep();
    }
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

    const t = state.audioContext.currentTime;
    const osc = state.audioContext.createOscillator();
    const gain = state.audioContext.createGain();

    osc.type = "sawtooth";
    osc.frequency.setValueAtTime(880, t);
    osc.frequency.setValueAtTime(1174.66, t + 0.1); // Two-tone urgent buzzer chirp

    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.12, t + 0.1);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);

    osc.connect(gain);
    gain.connect(state.audioContext.destination);

    osc.start(t);
    osc.stop(t + 0.3);
  }

  function unlockAudio() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    state.audioContext = state.audioContext || new AudioCtx();
    if (state.audioContext.state === "suspended") {
      state.audioContext.resume();
    }
  }

  // --- Evidence Modal Inspection (Task H3) ---

  function setupEvidenceModal() {
    if (!dom.evidenceModal) return;

    if (dom.modalCloseBtn) {
      dom.modalCloseBtn.addEventListener("click", closeEvidenceModal);
    }
    if (dom.modalDismissBtn) {
      dom.modalDismissBtn.addEventListener("click", closeEvidenceModal);
    }

    dom.evidenceModal.addEventListener("click", (e) => {
      if (e.target === dom.evidenceModal) {
        closeEvidenceModal();
      }
    });

    if (dom.modalConfirmBtn) {
      dom.modalConfirmBtn.addEventListener("click", () => {
        if (state.activeModalEvent) {
          acknowledge(state.activeModalEvent.event_id, "confirmed");
        }
      });
    }

    if (dom.modalRejectBtn) {
      dom.modalRejectBtn.addEventListener("click", () => {
        if (state.activeModalEvent) {
          acknowledge(state.activeModalEvent.event_id, "rejected");
        }
      });
    }
  }

  function openEvidenceModal(event) {
    if (!dom.evidenceModal) return;
    state.activeModalEvent = event;
    dom.evidenceModal.hidden = false;

    // Header Title
    dom.modalTitle.textContent = `Incident Evidence: ${event.event_type} (${event.event_id})`;

    // Media Handling (Video clip or Snapshot image)
    dom.modalImg.hidden = true;
    dom.modalVideo.hidden = true;
    dom.modalNoMedia.hidden = true;

    if (event.clip_url) {
      dom.modalVideo.hidden = false;
      const clipSrc = event.clip_url.startsWith("http")
        ? event.clip_url
        : `${window.IBVAP_API.getApiBase()}${event.clip_url}`;
      dom.modalVideo.src = clipSrc;
      dom.modalVideo.load();
    } else {
      const evidenceUrl = getEvidenceUrl(event);
      if (evidenceUrl) {
        dom.modalImg.hidden = false;
        dom.modalImg.src = evidenceUrl;
      } else {
        // Fallback: request snapshot of camera at time of inspection
        const snapUrl = window.IBVAP_API.getCameraSnapshotUrl(event.camera_id);
        if (snapUrl) {
          dom.modalImg.hidden = false;
          dom.modalImg.src = snapUrl;
        } else {
          dom.modalNoMedia.hidden = false;
        }
      }
    }

    // Details Grid Construction (Safe Text Only)
    while (dom.modalDetailsGrid.firstChild) {
      dom.modalDetailsGrid.removeChild(dom.modalDetailsGrid.firstChild);
    }

    addModalDetail("Event ID", event.event_id);
    addModalDetail("Camera", resolveCameraLocation(event.camera_id));
    addModalDetail("Event Type", event.event_type);
    addModalDetail("Severity", event.severity || "MEDIUM");
    addModalDetail("Status", event.ack_status || "pending");
    addModalDetail("Timestamp", event.timestamp ? new Date(event.timestamp).toLocaleString() : "--");
    addModalDetail("Object Kind", event.object_type || "N/A");
    addModalDetail("Track ID", event.track_id !== null && event.track_id !== undefined ? `#${event.track_id}` : "N/A");
    addModalDetail("Confidence", `${(Number(event.confidence || 0) * 100).toFixed(1)}%`);

    if (event.extra) {
      if (event.extra.direction) addModalDetail("Crossing Direction", event.extra.direction);
      if (event.extra.dwell_seconds !== undefined) addModalDetail("Dwell Duration", `${event.extra.dwell_seconds} seconds`);
      if (event.extra.kind) addModalDetail("Tamper Category", event.extra.kind);
      if (event.extra.latency_ms !== undefined) addModalDetail("Pipeline Latency", `${event.extra.latency_ms} ms`);
    }

    // Modal action buttons visibility
    const isPending = (event.ack_status || "pending") === "pending" && !dismissedIds.has(event.event_id);
    if (dom.modalConfirmBtn) dom.modalConfirmBtn.hidden = !isPending;
    if (dom.modalRejectBtn) dom.modalRejectBtn.hidden = !isPending;
  }

  function addModalDetail(label, val) {
    const item = document.createElement("div");
    item.className = "modal-detail-item";
    const lblSpan = document.createElement("span");
    lblSpan.textContent = label;
    const valSpan = document.createElement("span");
    valSpan.textContent = String(val);
    item.append(lblSpan, valSpan);
    dom.modalDetailsGrid.append(item);
  }

  function closeEvidenceModal() {
    if (!dom.evidenceModal) return;
    dom.evidenceModal.hidden = true;
    state.activeModalEvent = null;
    if (dom.modalVideo) {
      dom.modalVideo.pause();
      dom.modalVideo.src = "";
    }
  }

  // --- Filters & Search Controls (Task H4) ---

  function setupFilters() {
    loadFiltersFromHash();

    if (dom.filterCameraSelect) {
      dom.filterCameraSelect.value = state.filters.camera;
      dom.filterCameraSelect.addEventListener("change", (e) => {
        state.filters.camera = e.target.value;
        syncFiltersToHash();
        applyFilters();
      });
    }

    if (dom.filterTypeSelect) {
      dom.filterTypeSelect.value = state.filters.type;
      dom.filterTypeSelect.addEventListener("change", (e) => {
        state.filters.type = e.target.value;
        syncFiltersToHash();
        applyFilters();
      });
    }

    if (dom.filterAckSelect) {
      dom.filterAckSelect.value = state.filters.status;
      dom.filterAckSelect.addEventListener("change", (e) => {
        state.filters.status = e.target.value;
        syncFiltersToHash();
        applyFilters();
      });
    }

    if (dom.filterTimerangeSelect) {
      dom.filterTimerangeSelect.value = state.filters.timeRange;
      dom.filterTimerangeSelect.addEventListener("change", (e) => {
        state.filters.timeRange = e.target.value;
        syncFiltersToHash();
        applyFilters();
      });
    }

    // Severity Checkboxes
    [dom.filterSevHigh, dom.filterSevMedium, dom.filterSevLow].forEach((cb) => {
      if (!cb) return;
      cb.checked = state.filters.severities.has(cb.value);
      cb.parentElement.classList.toggle("checked", cb.checked);
      cb.addEventListener("change", () => {
        if (cb.checked) {
          state.filters.severities.add(cb.value);
        } else {
          state.filters.severities.delete(cb.value);
        }
        cb.parentElement.classList.toggle("checked", cb.checked);
        syncFiltersToHash();
        applyFilters();
      });
    });

    // Debounced Search Input
    if (dom.filterSearchInput) {
      dom.filterSearchInput.value = state.filters.search;
      dom.filterSearchInput.addEventListener("input", (e) => {
        if (state.filters.searchDebounceTimer) {
          clearTimeout(state.filters.searchDebounceTimer);
        }
        state.filters.searchDebounceTimer = setTimeout(() => {
          state.filters.search = e.target.value.trim();
          syncFiltersToHash();
          applyFilters();
        }, 250);
      });
    }

    if (dom.clearFiltersBtn) {
      dom.clearFiltersBtn.addEventListener("click", resetFilters);
    }

    if (dom.exportCsvBtn) {
      dom.exportCsvBtn.addEventListener("click", exportFilteredCsv);
    }

    if (dom.printReportBtn) {
      dom.printReportBtn.addEventListener("click", printReport);
    }
  }

  function populateFilterCameraOptions(cameras) {
    if (!dom.filterCameraSelect) return;
    const currentVal = dom.filterCameraSelect.value;
    while (dom.filterCameraSelect.firstChild) {
      dom.filterCameraSelect.removeChild(dom.filterCameraSelect.firstChild);
    }
    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = "All Cameras";
    dom.filterCameraSelect.appendChild(defaultOpt);

    cameras.forEach((cam) => {
      const opt = document.createElement("option");
      opt.value = cam.camera_id;
      opt.textContent = `${cam.location} (${cam.camera_id})`;
      dom.filterCameraSelect.appendChild(opt);
    });
    dom.filterCameraSelect.value = currentVal;
  }

  function applyFilters() {
    if (latestEvents.length > 0) {
      renderAlerts(latestEvents, false);
      renderAnalyticsCharts(latestEvents);
    }
  }

  function resetFilters() {
    state.filters.camera = "";
    state.filters.type = "";
    state.filters.severities = new Set(["HIGH", "MEDIUM", "LOW"]);
    state.filters.status = "";
    state.filters.timeRange = "all";
    state.filters.search = "";

    if (dom.filterCameraSelect) dom.filterCameraSelect.value = "";
    if (dom.filterTypeSelect) dom.filterTypeSelect.value = "";
    if (dom.filterAckSelect) dom.filterAckSelect.value = "";
    if (dom.filterTimerangeSelect) dom.filterTimerangeSelect.value = "all";
    if (dom.filterSearchInput) dom.filterSearchInput.value = "";

    [dom.filterSevHigh, dom.filterSevMedium, dom.filterSevLow].forEach((cb) => {
      if (!cb) return;
      cb.checked = true;
      cb.parentElement.classList.add("checked");
    });

    if (window.location.hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }

    applyFilters();
  }

  function loadFiltersFromHash() {
    const raw = window.location.hash.replace(/^#/, "");
    if (!raw) return;
    const params = new URLSearchParams(raw);

    if (params.has("cam")) state.filters.camera = params.get("cam");
    if (params.has("type")) state.filters.type = params.get("type");
    if (params.has("status")) state.filters.status = params.get("status");
    if (params.has("time")) state.filters.timeRange = params.get("time");
    if (params.has("q")) state.filters.search = params.get("q");

    if (params.has("sev")) {
      const sevs = params.get("sev").split(",").map((s) => s.toUpperCase());
      state.filters.severities = new Set(sevs);
    }
  }

  function syncFiltersToHash() {
    const params = new URLSearchParams();
    if (state.filters.camera) params.set("cam", state.filters.camera);
    if (state.filters.type) params.set("type", state.filters.type);
    if (state.filters.status) params.set("status", state.filters.status);
    if (state.filters.timeRange && state.filters.timeRange !== "all") params.set("time", state.filters.timeRange);
    if (state.filters.search) params.set("q", state.filters.search);

    if (state.filters.severities.size < 3) {
      params.set("sev", Array.from(state.filters.severities).join(","));
    }

    const str = params.toString();
    const newHash = str ? `#${str}` : "";
    if (window.location.hash !== newHash) {
      history.replaceState(null, "", window.location.pathname + window.location.search + newHash);
    }
  }

  function handleHashChange() {
    loadFiltersFromHash();
    if (dom.filterCameraSelect) dom.filterCameraSelect.value = state.filters.camera;
    if (dom.filterTypeSelect) dom.filterTypeSelect.value = state.filters.type;
    if (dom.filterAckSelect) dom.filterAckSelect.value = state.filters.status;
    if (dom.filterTimerangeSelect) dom.filterTimerangeSelect.value = state.filters.timeRange;
    if (dom.filterSearchInput) dom.filterSearchInput.value = state.filters.search;
    [dom.filterSevHigh, dom.filterSevMedium, dom.filterSevLow].forEach((cb) => {
      if (!cb) return;
      cb.checked = state.filters.severities.has(cb.value);
      cb.parentElement.classList.toggle("checked", cb.checked);
    });
    applyFilters();
  }

  // --- Analytics Visualizations Canvas Charts (Task H5) ---

  function renderAnalyticsCharts(events) {
    if (!events) return;
    drawHourlyChart(dom.chartHourlyCanvas, events);
    drawSeverityChart(dom.chartSeverityCanvas, events);
    drawStatusChart(dom.chartStatusCanvas, events);
  }

  function setupCanvasDpi(canvas) {
    if (!canvas) return null;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return null;

    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    return { ctx, width: rect.width, height: rect.height };
  }

  function drawHourlyChart(canvas, events) {
    const setup = setupCanvasDpi(canvas);
    if (!setup) return;
    const { ctx, width, height } = setup;

    ctx.clearRect(0, 0, width, height);

    // Create 12 hourly buckets ending at current hour
    const now = new Date();
    const buckets = [];
    for (let i = 11; i >= 0; i--) {
      const d = new Date(now.getTime() - i * 3600 * 1000);
      buckets.push({
        hour: d.getHours(),
        label: `${d.getHours()}:00`,
        count: 0,
      });
    }

    events.forEach((e) => {
      if (!e.timestamp) return;
      const t = new Date(e.timestamp).getTime();
      const diffHrs = Math.floor((now.getTime() - t) / (3600 * 1000));
      if (diffHrs >= 0 && diffHrs < 12) {
        buckets[11 - diffHrs].count++;
      }
    });

    const maxCount = Math.max(1, ...buckets.map((b) => b.count));
    const padX = 30;
    const padY = 24;
    const chartW = width - padX * 2;
    const chartH = height - padY * 2;
    const barWidth = chartW / buckets.length - 6;

    // Grid baseline
    ctx.strokeStyle = "rgba(255, 255, 255, 0.1)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padX, height - padY);
    ctx.lineTo(width - padX, height - padY);
    ctx.stroke();

    buckets.forEach((b, i) => {
      const x = padX + i * (barWidth + 6) + 3;
      const barH = (b.count / maxCount) * (chartH - 10);
      const y = height - padY - barH;

      // Bar fill
      ctx.fillStyle = b.count > 0 ? "#68c7ff" : "rgba(104, 199, 255, 0.15)";
      ctx.fillRect(x, y, barWidth, barH);

      // Value label on top
      if (b.count > 0) {
        ctx.fillStyle = "#edf2f7";
        ctx.font = "bold 10px monospace";
        ctx.textAlign = "center";
        ctx.fillText(String(b.count), x + barWidth / 2, y - 4);
      }

      // X Axis label every 2 bars
      if (i % 2 === 0 || i === buckets.length - 1) {
        ctx.fillStyle = "#95a5b8";
        ctx.font = "9px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(`${b.hour}h`, x + barWidth / 2, height - 8);
      }
    });

    if (dom.hourlyChartSummary) {
      const totalIn12h = buckets.reduce((acc, b) => acc + b.count, 0);
      dom.hourlyChartSummary.textContent = `${totalIn12h} total event${totalIn12h === 1 ? "" : "s"} recorded across the last 12 hours.`;
    }
  }

  function drawSeverityChart(canvas, events) {
    const setup = setupCanvasDpi(canvas);
    if (!setup) return;
    const { ctx, width, height } = setup;

    ctx.clearRect(0, 0, width, height);

    let high = 0;
    let med = 0;
    let low = 0;
    events.forEach((e) => {
      const s = (e.severity || "MEDIUM").toUpperCase();
      if (s === "HIGH") high++;
      else if (s === "MEDIUM") med++;
      else low++;
    });

    const total = high + med + low;
    const cx = width / 2;
    const cy = height / 2;
    const rOuter = Math.min(width, height) * 0.38;
    const rInner = rOuter * 0.58;

    if (total === 0) {
      ctx.beginPath();
      ctx.arc(cx, cy, rOuter, 0, Math.PI * 2);
      ctx.arc(cx, cy, rInner, Math.PI * 2, 0, true);
      ctx.fillStyle = "rgba(255, 255, 255, 0.08)";
      ctx.fill();
      ctx.fillStyle = "#95a5b8";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No Events", cx, cy + 4);
      return;
    }

    const slices = [
      { count: high, color: "#ff5a66" },
      { count: med, color: "#ffb454" },
      { count: low, color: "#68c7ff" },
    ];

    let startAngle = -Math.PI / 2;
    slices.forEach((s) => {
      if (s.count === 0) return;
      const angle = (s.count / total) * Math.PI * 2;
      ctx.beginPath();
      ctx.arc(cx, cy, rOuter, startAngle, startAngle + angle);
      ctx.arc(cx, cy, rInner, startAngle + angle, startAngle, true);
      ctx.closePath();
      ctx.fillStyle = s.color;
      ctx.fill();
      startAngle += angle;
    });

    // Center count label
    ctx.fillStyle = "#edf2f7";
    ctx.font = "bold 18px monospace";
    ctx.textAlign = "center";
    ctx.fillText(String(total), cx, cy + 2);
    ctx.fillStyle = "#95a5b8";
    ctx.font = "10px sans-serif";
    ctx.fillText("TOTAL", cx, cy + 16);
  }

  function drawStatusChart(canvas, events) {
    const setup = setupCanvasDpi(canvas);
    if (!setup) return;
    const { ctx, width, height } = setup;

    ctx.clearRect(0, 0, width, height);

    let pending = 0;
    let confirmed = 0;
    let rejected = 0;

    events.forEach((e) => {
      const st = e.ack_status || "pending";
      if (st === "confirmed") confirmed++;
      else if (st === "rejected") rejected++;
      else pending++;
    });

    const total = pending + confirmed + rejected;
    const barX = 20;
    const barY = height / 2 - 16;
    const barW = width - 40;
    const barH = 32;

    if (total === 0) {
      ctx.fillStyle = "rgba(255, 255, 255, 0.08)";
      ctx.fillRect(barX, barY, barW, barH);
      ctx.fillStyle = "#95a5b8";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No Data", width / 2, barY + 20);
      return;
    }

    let currentX = barX;
    const segs = [
      { name: "Pending", count: pending, color: "#ffb454" },
      { name: "Confirmed", count: confirmed, color: "#5ee0a0" },
      { name: "Rejected", count: rejected, color: "#8c9ba5" },
    ];

    segs.forEach((seg, idx) => {
      if (seg.count === 0) return;
      const w = (seg.count / total) * barW;
      ctx.fillStyle = seg.color;
      ctx.fillRect(currentX, barY, w, barH);

      // Text inside segment if wide enough
      if (w > 36) {
        ctx.fillStyle = "#090d13";
        ctx.font = "bold 11px monospace";
        ctx.textAlign = "center";
        ctx.fillText(String(seg.count), currentX + w / 2, barY + 20);
      }
      currentX += w;
    });

    // Summary text below bar
    ctx.fillStyle = "#95a5b8";
    ctx.font = "11px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(`${pending} Pending · ${confirmed} Confirmed · ${rejected} Rejected`, width / 2, height - 12);
  }

  // --- CSV Export & Print Report (Task H6) ---

  function exportFilteredCsv() {
    const eventsToExport = latestEvents.filter(matchesFilter);
    if (eventsToExport.length === 0) {
      alert("No events match the current filter criteria to export.");
      return;
    }

    const headers = [
      "Event ID",
      "Timestamp (UTC)",
      "Camera ID",
      "Camera Location",
      "Event Type",
      "Object Kind",
      "Track ID",
      "Confidence",
      "Severity",
      "Ack Status",
      "Latency (ms)",
      "Extra Telemetry",
    ];

    const escapeCsv = (val) => {
      if (val === null || val === undefined) return '""';
      let str = String(val);
      // Prevent formula injection in spreadsheet applications (=, +, -, @)
      if (/^[=+\-@]/.test(str)) {
        str = `'${str}`;
      }
      return `"${str.replace(/"/g, '""')}"`;
    };

    const rows = [headers.map(escapeCsv).join(",")];

    eventsToExport.forEach((e) => {
      const cam = state.cameras.find((c) => c.camera_id === e.camera_id);
      const loc = cam ? cam.location : "";
      const extraJson = JSON.stringify(e.extra || {});
      const latency = e.extra && e.extra.latency_ms !== undefined ? e.extra.latency_ms : "";

      const row = [
        escapeCsv(e.event_id),
        escapeCsv(e.timestamp),
        escapeCsv(e.camera_id),
        escapeCsv(loc),
        escapeCsv(e.event_type),
        escapeCsv(e.object_type || "N/A"),
        escapeCsv(e.track_id !== null && e.track_id !== undefined ? e.track_id : ""),
        escapeCsv(e.confidence !== undefined ? Number(e.confidence).toFixed(2) : ""),
        escapeCsv(e.severity || "MEDIUM"),
        escapeCsv(e.ack_status || "pending"),
        escapeCsv(latency),
        escapeCsv(extraJson),
      ];
      rows.push(row.join(","));
    });

    // UTF-8 BOM (\uFEFF) for Microsoft Excel compatibility
    const csvContent = "\uFEFF" + rows.join("\r\n");
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);

    const now = new Date();
    const ts = now.toISOString().replace(/[:\-T]/g, "").slice(0, 14);
    const filename = `ibvap_audit_report_${ts}.csv`;

    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      a.remove();
      URL.revokeObjectURL(url);
    }, 2000);
  }

  function printReport() {
    const eventsToPrint = latestEvents.filter(matchesFilter);
    if (dom.printTimestamp) {
      dom.printTimestamp.textContent = new Date().toLocaleString();
    }
    if (dom.printScope) {
      const scopeParts = [];
      if (state.filters.camera) scopeParts.push(`Camera: ${state.filters.camera}`);
      if (state.filters.type) scopeParts.push(`Type: ${state.filters.type}`);
      if (state.filters.status) scopeParts.push(`Status: ${state.filters.status}`);
      dom.printScope.textContent = scopeParts.length > 0 ? scopeParts.join(" | ") : "All Surveillance Feeds";
    }
    if (dom.printCount) {
      dom.printCount.textContent = `${eventsToPrint.length} incident record${eventsToPrint.length === 1 ? "" : "s"}`;
    }

    window.print();
  }
})();
