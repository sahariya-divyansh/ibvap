const API_BASE_URL = "http://localhost:8000";
const POLL_INTERVAL_MS = 2500;
const STATS_POLL_INTERVAL_MS = 1500;
const STORAGE_KEY_SHOW_REVIEWED = "ibvap_show_reviewed";
const ALARM_EVENT_TYPES = new Set([
  "VIRTUAL_FENCE_CROSS",
  "RESTRICTED_ZONE_ENTRY",
]);

const feed = document.querySelector("#alert-feed");
const emptyState = document.querySelector("#empty-state");
const connectionStatus = document.querySelector("#connection-status");
const alertCount = document.querySelector("#alert-count");
const cameraToggle = document.querySelector("#camera-toggle");
const cameraPanel = document.querySelector("#camera-panel");
const cameraFeed = document.querySelector("#camera-feed");
const alarmBanner = document.querySelector("#alarm-banner");
const personCount = document.querySelector("#person-count");
const vehicleCount = document.querySelector("#vehicle-count");
const animalCount = document.querySelector("#animal-count");
const reviewedToggle = document.querySelector("#reviewed-toggle");
const reviewedFeed = document.querySelector("#reviewed-feed");

const renderedPending = new Map();
const renderedReviewed = new Map();
const dismissedIds = new Set();
let latestEvents = [];
let cachedReviewedCount = 0;
let initialPollComplete = false;
let showReviewed = getStoredShowReviewed();
let audioContext;

window.addEventListener("DOMContentLoaded", () => {
  cameraToggle.addEventListener("click", toggleCamera);
  if (reviewedToggle) {
    reviewedToggle.addEventListener("click", toggleReviewed);
    updateReviewedToggleUI(0);
  }
  if (reviewedFeed) {
    reviewedFeed.hidden = !showReviewed;
  }
  document.addEventListener("pointerdown", unlockAudio, { once: true });
  pollEvents();
  pollStats();
  setInterval(pollEvents, POLL_INTERVAL_MS);
  setInterval(pollStats, STATS_POLL_INTERVAL_MS);
});

function getStoredShowReviewed() {
  try {
    return localStorage.getItem(STORAGE_KEY_SHOW_REVIEWED) === "true";
  } catch {
    return false;
  }
}

function setStoredShowReviewed(value) {
  try {
    localStorage.setItem(STORAGE_KEY_SHOW_REVIEWED, String(value));
  } catch {
    // Ignore localStorage failures (e.g. private mode or disabled storage)
  }
}

function toggleReviewed() {
  showReviewed = !showReviewed;
  setStoredShowReviewed(showReviewed);
  if (reviewedFeed) {
    reviewedFeed.hidden = !showReviewed;
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
  if (!reviewedToggle) {
    return;
  }
  if (count !== undefined) {
    cachedReviewedCount = count;
  }
  const n = cachedReviewedCount;
  reviewedToggle.setAttribute("aria-pressed", String(showReviewed));
  reviewedToggle.classList.toggle("active", showReviewed);
  reviewedToggle.textContent = showReviewed
    ? `Hide reviewed (${n})`
    : `Show reviewed (${n})`;
}

function populateReviewedFeed() {
  if (!reviewedFeed) {
    return;
  }
  const reviewedEvents = latestEvents.filter(
    (e) => (e.ack_status && e.ack_status !== "pending") || dismissedIds.has(e.event_id)
  );
  for (const event of [...reviewedEvents].reverse()) {
    if (!renderedReviewed.has(event.event_id)) {
      const element = createAlertElement(event, true);
      renderedReviewed.set(event.event_id, element);
      reviewedFeed.prepend(element);
    }
  }
}

function clearReviewedFeed() {
  for (const element of renderedReviewed.values()) {
    element.remove();
  }
  renderedReviewed.clear();
}

async function pollEvents() {
  try {
    const response = await fetch(`${API_BASE_URL}/events`);
    if (!response.ok) {
      throw new Error(`Backend returned ${response.status}`);
    }

    const events = await response.json();
    connectionStatus.textContent = `Live - refreshed ${new Date().toLocaleTimeString()}`;
    renderEvents(events, initialPollComplete);
    initialPollComplete = true;
  } catch (error) {
    connectionStatus.textContent = `Backend unavailable: ${error.message}`;
  }
}

async function pollStats() {
  try {
    const response = await fetch(`${API_BASE_URL}/stats`);
    if (!response.ok) {
      throw new Error(`Backend returned ${response.status}`);
    }

    const stats = await response.json();
    personCount.textContent = String(stats.person || 0);
    vehicleCount.textContent = String(stats.vehicle || 0);
    animalCount.textContent = String(stats.animal || 0);
  } catch (error) {
    personCount.textContent = "-";
    vehicleCount.textContent = "-";
    animalCount.textContent = "-";
  }
}

function renderEvents(events, shouldAlarm) {
  latestEvents = events;
  const serverEventIds = new Set(events.map((e) => e.event_id));

  // Prune pending cards whose IDs the server no longer returns
  for (const [id, element] of renderedPending.entries()) {
    if (!serverEventIds.has(id)) {
      element.remove();
      renderedPending.delete(id);
    }
  }

  // Prune reviewed cards whose IDs the server no longer returns
  for (const [id, element] of renderedReviewed.entries()) {
    if (!serverEventIds.has(id)) {
      element.remove();
      renderedReviewed.delete(id);
      dismissedIds.delete(id);
    }
  }

  let reviewedCount = 0;

  for (const event of [...events].reverse()) {
    const isDismissed = dismissedIds.has(event.event_id);
    const isPending = (event.ack_status === "pending" || !event.ack_status) && !isDismissed;

    if (isPending) {
      if (!renderedPending.has(event.event_id)) {
        const element = createAlertElement(event, false);
        renderedPending.set(event.event_id, element);
        feed.prepend(element);
        if (shouldAlarm && ALARM_EVENT_TYPES.has(event.event_type)) {
          triggerAlarm();
        }
      }
    } else {
      reviewedCount++;
      // If card is currently displayed in pending, remove it
      if (renderedPending.has(event.event_id)) {
        renderedPending.get(event.event_id).remove();
        renderedPending.delete(event.event_id);
      }
      if (showReviewed && reviewedFeed) {
        const effectiveStatus = event.ack_status || "confirmed";
        const existing = renderedReviewed.get(event.event_id);
        if (existing) {
          syncReviewedState(existing, effectiveStatus);
        } else {
          const element = createAlertElement(event, true);
          renderedReviewed.set(event.event_id, element);
          reviewedFeed.prepend(element);
        }
      }
    }
  }

  updateSummary();
  updateReviewedToggleUI(reviewedCount);
}

function createAlertElement(event, isReviewed = false) {
  const article = document.createElement("article");
  const status = event.ack_status || "pending";
  article.className = `alert severity-${severityClass(event.event_type)}`;
  article.dataset.eventId = String(event.event_id);
  article.dataset.ackStatus = status;

  if (isReviewed || status !== "pending") {
    article.classList.add("handled");
  }

  const body = document.createElement("div");
  body.className = "alert-body";

  const heading = document.createElement("div");
  heading.className = "alert-heading";

  const eventType = document.createElement("span");
  eventType.className = "event-type";
  eventType.textContent = event.event_type;

  const ackBadge = document.createElement("span");
  ackBadge.className = "ack-badge";
  ackBadge.textContent = status;

  heading.append(eventType, ackBadge);

  const details = document.createElement("dl");
  details.className = "details";
  addDetail(details, "Object", event.object_type);
  addDetail(details, "Track", `#${event.track_id}`);
  addDetail(details, "Confidence", formatConfidence(event.confidence));
  addDetail(details, "Camera", event.camera_id);
  addDetail(details, "Time", formatTimestamp(event.timestamp));

  const actions = document.createElement("div");
  actions.className = "actions";
  if (!isReviewed && status === "pending") {
    actions.append(
      createAckButton("Confirm", "confirmed", event.event_id),
      createAckButton("Reject", "rejected", event.event_id),
    );
  } else {
    const label = document.createElement("span");
    label.className = "handled-label";
    label.textContent = status;
    actions.append(label);
  }

  body.append(heading, details, actions);

  if (event.evidence_path) {
    const evidence = document.createElement("img");
    evidence.className = "evidence";
    evidence.src = evidenceUrl(event.evidence_path);
    evidence.alt = `Evidence snapshot for ${event.event_type}`;
    evidence.addEventListener("error", () => {
      evidence.classList.add("missing");
      evidence.alt = "Evidence snapshot unavailable";
    });
    article.append(evidence, body);
  } else {
    article.classList.add("no-evidence");
    article.append(body);
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
  const element = renderedPending.get(eventId);
  if (!element) {
    return;
  }

  setButtonsDisabled(element, true);
  const existingError = element.querySelector(".inline-error");
  if (existingError) {
    existingError.remove();
  }

  try {
    const response = await fetch(`${API_BASE_URL}/events/${eventId}/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });

    if (!response.ok) {
      throw new Error(`Backend returned ${response.status}`);
    }

    const updatedEvent = await response.json();
    const finalStatus = updatedEvent.ack_status || status;
    dismissedIds.add(eventId);

    const cached = latestEvents.find((e) => e.event_id === eventId);
    if (cached) {
      cached.ack_status = finalStatus;
    }

    // Immediately remove from pending tracking and update counter
    renderedPending.delete(eventId);
    updateSummary();

    // Trigger collapse/fade animation (~250 ms)
    element.style.maxHeight = `${element.offsetHeight}px`;
    void element.offsetHeight;
    element.classList.add("dismissing");

    setTimeout(() => {
      element.remove();
      if (showReviewed && reviewedFeed) {
        addReviewedCard(eventId, finalStatus, cached, element);
      }
    }, 250);

    const reviewedCount = latestEvents.filter(
      (e) => (e.ack_status && e.ack_status !== "pending") || dismissedIds.has(e.event_id)
    ).length;
    updateReviewedToggleUI(reviewedCount);
  } catch (error) {
    connectionStatus.textContent = `Ack failed: ${error.message}`;
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

function addReviewedCard(eventId, status, cached, originalElement) {
  if (renderedReviewed.has(eventId)) {
    return;
  }
  let eventData = cached;
  if (!eventData && originalElement) {
    eventData = {
      event_id: eventId,
      event_type: originalElement.querySelector(".event-type")?.textContent || "EVENT",
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
    if (reviewedFeed) {
      reviewedFeed.prepend(reviewedCard);
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

function setButtonsDisabled(element, disabled) {
  element.querySelectorAll("button").forEach((button) => {
    button.disabled = disabled;
  });
}

function updateSummary() {
  const activeCount = renderedPending.size;
  alertCount.textContent = String(activeCount);
  emptyState.hidden = activeCount > 0;
}

function toggleCamera() {
  const shouldOpen = cameraPanel.hidden;
  cameraPanel.hidden = !shouldOpen;
  cameraToggle.textContent = shouldOpen ? "Close Camera" : "Open Camera";
  cameraFeed.src = shouldOpen ? `${API_BASE_URL}/video_feed` : "";
}

function evidenceUrl(evidencePath) {
  const filename = String(evidencePath || "").split(/[\\/]/).pop();
  return `${API_BASE_URL}/evidence/${encodeURIComponent(filename)}`;
}

function severityClass(eventType) {
  if (ALARM_EVENT_TYPES.has(eventType)) {
    return "high";
  }
  if (eventType === "NEW_OBJECT_DETECTED") {
    return "info";
  }
  return "normal";
}

function formatConfidence(confidence) {
  return Number(confidence).toFixed(2);
}

function formatTimestamp(timestamp) {
  return new Date(timestamp).toLocaleString();
}

function triggerAlarm() {
  playAlarmBeep();
  alarmBanner.hidden = false;
  alarmBanner.classList.remove("flash");
  void alarmBanner.offsetWidth;
  alarmBanner.classList.add("flash");
  window.setTimeout(() => {
    alarmBanner.hidden = true;
  }, 1800);
}

function playAlarmBeep() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) {
    return;
  }

  audioContext = audioContext || new AudioContext();
  if (audioContext.state === "suspended") {
    audioContext.resume();
  }
  const oscillator = audioContext.createOscillator();
  const gain = audioContext.createGain();
  oscillator.type = "square";
  oscillator.frequency.value = 880;
  gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.18, audioContext.currentTime + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.22);
  oscillator.connect(gain);
  gain.connect(audioContext.destination);
  oscillator.start();
  oscillator.stop(audioContext.currentTime + 0.24);
}

function unlockAudio() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) {
    return;
  }
  audioContext = audioContext || new AudioContext();
  if (audioContext.state === "suspended") {
    audioContext.resume();
  }
}
