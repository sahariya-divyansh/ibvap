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

  // In-memory mock camera config store matching CONTRACTS §7
  const mockConfigStore = {};

  function getDefaultMockConfig(cameraId) {
    if (cameraId === "cam_02") {
      return {
        fence: { start: [0.1, 0.4], end: [0.9, 0.4], alert_direction: "A_TO_B" },
        zone: [[0.2, 0.2], [0.8, 0.2], [0.8, 0.7], [0.2, 0.7]],
        frame_size: [640, 360],
      };
    }
    return {
      fence: { start: [0.0, 0.5], end: [1.0, 0.5], alert_direction: "ANY" },
      zone: [[0.62, 0.18], [0.95, 0.18], [0.95, 0.82], [0.62, 0.82]],
      frame_size: [640, 360],
    };
  }

  function createMockSnapshotSvg(cameraId) {
    const safeId = String(cameraId).replace(/[^a-zA-Z0-9_-]/g, "");
    const svg = `
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" width="100%" height="100%">
        <defs>
          <linearGradient id="sky" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="#0f1926"/>
            <stop offset="100%" stop-color="#18273a"/>
          </linearGradient>
          <linearGradient id="ground" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="#151e28"/>
            <stop offset="100%" stop-color="#0a1017"/>
          </linearGradient>
          <pattern id="cal-grid" width="32" height="32" patternUnits="userSpaceOnUse">
            <path d="M 32 0 L 0 0 0 32" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>
          </pattern>
        </defs>
        <rect width="640" height="180" fill="url(#sky)"/>
        <rect y="180" width="640" height="180" fill="url(#ground)"/>
        <rect width="640" height="360" fill="url(#cal-grid)"/>
        <line x1="0" y1="180" x2="640" y2="180" stroke="rgba(104,199,255,0.25)" stroke-width="1"/>
        <text x="24" y="36" fill="#68c7ff" font-family="monospace" font-size="13" font-weight="bold">SNAPSHOT // ${safeId}</text>
        <text x="24" y="56" fill="rgba(255,255,255,0.5)" font-family="monospace" font-size="11">RAW RESOLUTION: 640x360 · STANDBY</text>
        <circle cx="320" cy="180" r="4" fill="#ffb454"/>
      </svg>
    `.trim();
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  }

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
        let results = [...mockEventsCache];
        if (params.camera_id) {
          results = results.filter((e) => e.camera_id === params.camera_id);
        }
        if (params.event_type) {
          results = results.filter((e) => e.event_type === params.event_type);
        }
        if (params.severity) {
          results = results.filter((e) => e.severity === params.severity);
        }
        if (params.ack_status) {
          results = results.filter((e) => (e.ack_status || "pending") === params.ack_status);
        }
        if (params.start_time) {
          const startTime = new Date(params.start_time).getTime();
          results = results.filter((e) => new Date(e.timestamp).getTime() >= startTime);
        }
        if (params.end_time) {
          const endTime = new Date(params.end_time).getTime();
          results = results.filter((e) => new Date(e.timestamp).getTime() <= endTime);
        }
        if (params.limit) {
          const lim = parseInt(params.limit, 10);
          if (Number.isFinite(lim) && lim > 0) {
            results = results.slice(0, lim);
          }
        }
        return results;
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

    getCameraSnapshotUrl(cameraId) {
      if (isMock) {
        return createMockSnapshotSvg(cameraId);
      }
      return `${API_BASE}/cameras/${encodeURIComponent(cameraId)}/snapshot?t=${Date.now()}`;
    },

    async fetchCameraConfig(cameraId) {
      if (isMock) {
        if (isFail) {
          throw new Error("Mock mode simulated client failure (?fail=1)");
        }
        if (!mockConfigStore[cameraId]) {
          mockConfigStore[cameraId] = getDefaultMockConfig(cameraId);
        }
        // Deep copy
        return JSON.parse(JSON.stringify(mockConfigStore[cameraId]));
      }
      return await fetchWithTimeout(`${API_BASE}/cameras/${encodeURIComponent(cameraId)}/config`);
    },

    async updateCameraConfig(cameraId, configBody) {
      if (isMock) {
        if (isFail) {
          throw new Error("Mock mode simulated client failure (?fail=1)");
        }
        // Validate geometry according to CONTRACTS §5 & §7
        if (!configBody.fence || !Array.isArray(configBody.fence.start) || !Array.isArray(configBody.fence.end)) {
          throw new Error("Invalid fence configuration: start and end points required");
        }
        if (configBody.fence.start.length !== 2 || configBody.fence.end.length !== 2) {
          throw new Error("Fence points must have exactly 2 coordinates [x, y]");
        }
        if (configBody.zone && configBody.zone.length > 0 && configBody.zone.length < 3) {
          throw new Error("Zone must be empty or contain at least 3 points");
        }
        const validated = {
          fence: {
            start: [
              Math.max(0, Math.min(1, Number(configBody.fence.start[0]))),
              Math.max(0, Math.min(1, Number(configBody.fence.start[1]))),
            ],
            end: [
              Math.max(0, Math.min(1, Number(configBody.fence.end[0]))),
              Math.max(0, Math.min(1, Number(configBody.fence.end[1]))),
            ],
            alert_direction: ["ANY", "A_TO_B", "B_TO_A"].includes(configBody.fence.alert_direction)
              ? configBody.fence.alert_direction
              : "ANY",
          },
          zone: Array.isArray(configBody.zone)
            ? configBody.zone.map((pt) => [
                Math.max(0, Math.min(1, Number(pt[0]))),
                Math.max(0, Math.min(1, Number(pt[1]))),
              ])
            : [],
          frame_size: [640, 360],
        };
        mockConfigStore[cameraId] = validated;
        return JSON.parse(JSON.stringify(validated));
      }
      return await fetchWithTimeout(`${API_BASE}/cameras/${encodeURIComponent(cameraId)}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(configBody),
      });
    },
  };

  global.IBVAP_API = api;
})(window);
