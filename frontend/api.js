/**
 * IBVAP Frontend Data & Network Client
 * Centralized API layer with AbortController timeouts, mock mode isolation,
 * and fail simulation (?fail=1).
 */
(function (global) {
  "use strict";

  const urlParams = new URLSearchParams(global.location.search);
  const isMock = urlParams.get("mock") === "1" || urlParams.get("mock") === "true";
  const isFail = urlParams.get("fail") === "1" || urlParams.get("fail") === "true";
  const camsParam = parseInt(urlParams.get("cams"), 10);
  const camsLimit = Number.isFinite(camsParam) && camsParam > 0 ? camsParam : null;

  const API_BASE = `${global.location.protocol}//${global.location.hostname}:8000`;
  const DEFAULT_TIMEOUT_MS = 4000;

  // Local cache for mock events to support stateful mock acks
  let mockEventsCache = null;

  async function fetchWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      return await response.json();
    } catch (err) {
      if (err.name === "AbortError") {
        throw new Error(`Request timed out after ${timeoutMs}ms`);
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function createMockSvgFeed(cameraId, locationLabel) {
    const safeId = String(cameraId).replace(/[^a-zA-Z0-9_-]/g, "");
    const safeLoc = String(locationLabel).replace(/[^a-zA-Z0-9_ -]/g, "");
    const svg = `
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" width="100%" height="100%">
        <defs>
          <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#0a1017"/>
            <stop offset="100%" stop-color="#141e2b"/>
          </linearGradient>
          <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M 40 0 L 0 0 0 40" fill="none" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>
          </pattern>
        </defs>
        <rect width="640" height="360" fill="url(#g)"/>
        <rect width="640" height="360" fill="url(#grid)"/>
        <circle cx="320" cy="180" r="70" fill="none" stroke="rgba(104,199,255,0.2)" stroke-width="1.5" stroke-dasharray="4 4"/>
        <line x1="160" y1="180" x2="480" y2="180" stroke="rgba(104,199,255,0.15)" stroke-width="1"/>
        <line x1="320" y1="60" x2="320" y2="300" stroke="rgba(104,199,255,0.15)" stroke-width="1"/>
        <text x="24" y="38" fill="#5ee0a0" font-family="monospace" font-size="14" font-weight="bold">● MOCK FEED [${safeId}]</text>
        <text x="24" y="60" fill="#9fb0c3" font-family="sans-serif" font-size="13">${safeLoc}</text>
        <text x="24" y="336" fill="rgba(255,255,255,0.4)" font-family="monospace" font-size="12">STANDBY · SYNTHETIC FEED</text>
      </svg>
    `.trim();
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  }

  const api = {
    isMockMode() {
      return isMock;
    },

    isFailMode() {
      return isFail;
    },

    getCamsLimit() {
      return camsLimit;
    },

    getApiBase() {
      return API_BASE;
    },

    getVideoFeedUrl(cameraId, locationLabel = "") {
      if (isMock) {
        return createMockSvgFeed(cameraId, locationLabel);
      }
      // CONTRACTS §7: Old /video_feed = first camera (compat)
      if (cameraId === "cam_01") {
        return `${API_BASE}/video_feed`;
      }
      return `${API_BASE}/video_feed/${encodeURIComponent(cameraId)}`;
    },

    async fetchCameras() {
      if (isMock) {
        if (isFail) {
          throw new Error("Mock mode simulated client failure (?fail=1)");
        }
        const data = await fetchWithTimeout("mock/cameras.json");
        if (Array.isArray(data) && camsLimit !== null) {
          return data.slice(0, camsLimit);
        }
        return data;
      }
      try {
        return await fetchWithTimeout(`${API_BASE}/cameras`);
      } catch (err) {
        // CONTRACTS §5 & §7 compatibility fallback for baseline single-camera backend prior to D6
        if (err.message && err.message.includes("404")) {
          const stats = await fetchWithTimeout(`${API_BASE}/stats`);
          return [
            {
              camera_id: "cam_01",
              location: "BOP West Gate",
              status: "LIVE",
              fps: 12.0,
              latency_ms: 120,
              last_frame_age_s: 0.1,
              low_visibility: false,
              counts: {
                person: stats.person || 0,
                vehicle: stats.vehicle || 0,
                animal: stats.animal || 0,
              },
            },
          ];
        }
        throw err;
      }
    },

    async fetchStats(cameraId = null) {
      if (isMock) {
        if (isFail) {
          throw new Error("Mock mode simulated client failure (?fail=1)");
        }
        return await fetchWithTimeout("mock/stats.json");
      }
      const url = cameraId
        ? `${API_BASE}/stats?camera_id=${encodeURIComponent(cameraId)}`
        : `${API_BASE}/stats`;
      return await fetchWithTimeout(url);
    },

    async fetchEvents(params = {}) {
      if (isMock) {
        if (isFail) {
          throw new Error("Mock mode simulated client failure (?fail=1)");
        }
        if (!mockEventsCache) {
          mockEventsCache = await fetchWithTimeout("mock/events.json");
        }
        return mockEventsCache;
      }
      const search = new URLSearchParams(params).toString();
      const url = search ? `${API_BASE}/events?${search}` : `${API_BASE}/events`;
      return await fetchWithTimeout(url);
    },

    async acknowledgeEvent(eventId, status) {
      if (isMock) {
        if (isFail) {
          throw new Error("Mock mode simulated client failure (?fail=1)");
        }
        if (!mockEventsCache) {
          mockEventsCache = await fetchWithTimeout("mock/events.json");
        }
        const target = mockEventsCache.find((e) => e.event_id === eventId);
        if (target) {
          target.ack_status = status;
        }
        return { event_id: eventId, ack_status: status };
      }
      return await fetchWithTimeout(`${API_BASE}/events/${encodeURIComponent(eventId)}/ack`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
    },
  };

  global.IBVAP_API = api;
})(window);
