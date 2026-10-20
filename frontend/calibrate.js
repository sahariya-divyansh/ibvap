/**
 * IBVAP Calibration Controller
 * Interactive canvas overlay over live snapshot for configuring virtual fences and restricted zones.
 * Vanilla JavaScript only. Zero innerHTML for server/user data.
 */
(function () {
  "use strict";

  // State
  const state = {
    cameras: [],
    selectedCameraId: null,
    currentTool: "fence", // "fence" | "zone"
    alertDirection: "ANY", // "ANY" | "A_TO_B" | "B_TO_A"
    fencePoints: [], // [[x, y], [x, y]] normalized [0, 1]
    zonePoints: [], // [[x, y], ...] normalized [0, 1]
    zoneClosed: false,
    hoverPoint: null, // [x, y] normalized for live preview
    savedConfig: null,
    isLoading: false,
    isSaving: false,
  };

  // DOM Elements
  const dom = {
    cameraSelect: document.querySelector("#camera-select"),
    refreshSnapshotBtn: document.querySelector("#refresh-snapshot-btn"),
    toolFenceBtn: document.querySelector("#tool-fence-btn"),
    toolZoneBtn: document.querySelector("#tool-zone-btn"),
    directionSelect: document.querySelector("#direction-select"),
    closeZoneBtn: document.querySelector("#close-zone-btn"),
    undoBtn: document.querySelector("#undo-btn"),
    clearBtn: document.querySelector("#clear-btn"),
    saveConfigBtn: document.querySelector("#save-config-btn"),
    calStatusBanner: document.querySelector("#cal-status-banner"),
    canvasContainer: document.querySelector("#canvas-container"),
    snapshotImg: document.querySelector("#snapshot-img"),
    calCanvas: document.querySelector("#cal-canvas"),
    calLoadingOverlay: document.querySelector("#cal-loading-overlay"),
    calErrorOverlay: document.querySelector("#cal-error-overlay"),
    calErrorText: document.querySelector("#cal-error-text"),
    calRetryBtn: document.querySelector("#cal-retry-btn"),
    geometryStatusBadge: document.querySelector("#geometry-status-badge"),
    debugDraftJson: document.querySelector("#debug-draft-json"),
    debugSavedJson: document.querySelector("#debug-saved-json"),
    mockBadge: document.querySelector("#mock-badge"),
    unreachableBanner: document.querySelector("#unreachable-banner"),
    unreachableText: document.querySelector("#unreachable-text"),
    connectionStatus: document.querySelector("#connection-status"),
  };

  const ctx = dom.calCanvas.getContext("2d");

  window.addEventListener("DOMContentLoaded", initCalibration);

  async function initCalibration() {
    setupMockBadge();
    setupEventListeners();
    await loadCameraList();
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
    dom.cameraSelect.addEventListener("change", (e) => {
      selectCamera(e.target.value);
    });

    dom.refreshSnapshotBtn.addEventListener("click", () => {
      if (state.selectedCameraId) {
        loadSnapshotAndConfig(state.selectedCameraId);
      }
    });

    dom.toolFenceBtn.addEventListener("click", () => setTool("fence"));
    dom.toolZoneBtn.addEventListener("click", () => setTool("zone"));

    dom.directionSelect.addEventListener("change", (e) => {
      state.alertDirection = e.target.value;
      updateUI();
    });

    dom.closeZoneBtn.addEventListener("click", closeZone);
    dom.undoBtn.addEventListener("click", handleUndo);
    dom.clearBtn.addEventListener("click", handleClear);
    dom.saveConfigBtn.addEventListener("click", saveConfiguration);
    dom.calRetryBtn.addEventListener("click", () => {
      if (state.selectedCameraId) {
        loadSnapshotAndConfig(state.selectedCameraId);
      }
    });

    // Canvas Mouse Interactions
    dom.calCanvas.addEventListener("click", handleCanvasClick);
    dom.calCanvas.addEventListener("dblclick", handleCanvasDblClick);
    dom.calCanvas.addEventListener("mousemove", handleCanvasMouseMove);
    dom.calCanvas.addEventListener("mouseleave", handleCanvasMouseLeave);

    // Keyboard support
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        cancelCurrentDrawing();
      } else if (e.key === "Backspace" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) {
        handleUndo();
      }
    });

    // Handle responsive container resizing
    const resizeObserver = new ResizeObserver(() => {
      resizeCanvasToImage();
      draw();
    });
    resizeObserver.observe(dom.canvasContainer);

    dom.snapshotImg.addEventListener("load", () => {
      resizeCanvasToImage();
      draw();
    });
  }

  async function loadCameraList() {
    try {
      const cameras = await window.IBVAP_API.fetchCameras();
      state.cameras = cameras;
      dom.cameraSelect.replaceChildren();

      if (!cameras || cameras.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No cameras detected";
        dom.cameraSelect.appendChild(opt);
        showStatus("No active cameras found", "error");
        return;
      }

      cameras.forEach((cam) => {
        const opt = document.createElement("option");
        opt.value = cam.camera_id;
        opt.textContent = `${cam.camera_id} — ${cam.location} [${cam.status}]`;
        dom.cameraSelect.appendChild(opt);
      });

      // Check URL search params for ?camera_id=
      const urlParams = new URLSearchParams(window.location.search);
      const requestedId = urlParams.get("camera_id");
      const defaultId = requestedId && cameras.some((c) => c.camera_id === requestedId)
        ? requestedId
        : cameras[0].camera_id;

      dom.cameraSelect.value = defaultId;
      await selectCamera(defaultId);
    } catch (err) {
      showUnreachable(err.message);
    }
  }

  async function selectCamera(cameraId) {
    if (!cameraId) return;
    state.selectedCameraId = cameraId;
    await loadSnapshotAndConfig(cameraId);
  }

  async function loadSnapshotAndConfig(cameraId) {
    state.isLoading = true;
    dom.calLoadingOverlay.hidden = false;
    dom.calErrorOverlay.hidden = true;
    dom.saveConfigBtn.disabled = true;

    try {
      // 1. Snapshot URL with cache buster
      const snapshotUrl = window.IBVAP_API.getCameraSnapshotUrl(cameraId);
      dom.snapshotImg.src = snapshotUrl;

      // 2. Fetch existing configuration
      const config = await window.IBVAP_API.fetchCameraConfig(cameraId);
      state.savedConfig = config;

      // Parse existing geometry
      if (config.fence && Array.isArray(config.fence.start) && Array.isArray(config.fence.end)) {
        state.fencePoints = [
          [Number(config.fence.start[0]), Number(config.fence.start[1])],
          [Number(config.fence.end[0]), Number(config.fence.end[1])],
        ];
        state.alertDirection = config.fence.alert_direction || "ANY";
        dom.directionSelect.value = state.alertDirection;
      } else {
        state.fencePoints = [];
      }

      if (Array.isArray(config.zone) && config.zone.length >= 3) {
        state.zonePoints = config.zone.map((pt) => [Number(pt[0]), Number(pt[1])]);
        state.zoneClosed = true;
      } else {
        state.zonePoints = [];
        state.zoneClosed = false;
      }

      dom.debugSavedJson.textContent = JSON.stringify(config, null, 2);
      hideUnreachable();
      showStatus(`Loaded configuration for ${cameraId}`, "info");
    } catch (err) {
      dom.calErrorOverlay.hidden = false;
      dom.calErrorText.textContent = `Error loading camera: ${err.message}`;
    } finally {
      state.isLoading = false;
      dom.calLoadingOverlay.hidden = true;
      updateUI();
    }
  }

  // --- Geometry Normalization & Coordinate Mapping ---

  function resizeCanvasToImage() {
    const rect = dom.snapshotImg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const dpr = window.devicePixelRatio || 1;
    dom.calCanvas.width = rect.width * dpr;
    dom.calCanvas.height = rect.height * dpr;
    dom.calCanvas.style.width = `${rect.width}px`;
    dom.calCanvas.style.height = `${rect.height}px`;

    ctx.resetTransform();
    ctx.scale(dpr, dpr);
  }

  function getNormalizedCoordinates(e) {
    const rect = dom.calCanvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return [0, 0];

    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));

    return [Number(x.toFixed(4)), Number(y.toFixed(4))];
  }

  function denormalizePoint(pt) {
    const rect = dom.calCanvas.getBoundingClientRect();
    return [pt[0] * rect.width, pt[1] * rect.height];
  }

  // --- Canvas Interaction Handlers ---

  function handleCanvasClick(e) {
    if (state.isLoading || state.isSaving) return;
    const pt = getNormalizedCoordinates(e);

    if (state.currentTool === "fence") {
      if (state.fencePoints.length >= 2) {
        // Reset and start new fence on click if already completed
        state.fencePoints = [pt];
      } else {
        state.fencePoints.push(pt);
      }
    } else if (state.currentTool === "zone") {
      if (state.zoneClosed) {
        state.zonePoints = [pt];
        state.zoneClosed = false;
      } else {
        state.zonePoints.push(pt);
      }
    }

    updateUI();
  }

  function handleCanvasDblClick(e) {
    e.preventDefault();
    if (state.currentTool === "zone" && !state.zoneClosed) {
      closeZone();
    }
  }

  function handleCanvasMouseMove(e) {
    state.hoverPoint = getNormalizedCoordinates(e);
    draw();
  }

  function handleCanvasMouseLeave() {
    state.hoverPoint = null;
    draw();
  }

  function closeZone() {
    if (state.zonePoints.length >= 3) {
      state.zoneClosed = true;
      state.hoverPoint = null;
      updateUI();
    } else {
      showStatus("Zone requires at least 3 points before closing", "warning");
    }
  }

  function handleUndo() {
    if (state.currentTool === "fence") {
      if (state.fencePoints.length > 0) {
        state.fencePoints.pop();
      }
    } else if (state.currentTool === "zone") {
      if (state.zoneClosed) {
        state.zoneClosed = false;
      } else if (state.zonePoints.length > 0) {
        state.zonePoints.pop();
      }
    }
    updateUI();
  }

  function handleClear() {
    if (state.currentTool === "fence") {
      state.fencePoints = [];
    } else if (state.currentTool === "zone") {
      state.zonePoints = [];
      state.zoneClosed = false;
    }
    state.hoverPoint = null;
    updateUI();
  }

  function cancelCurrentDrawing() {
    handleClear();
    showStatus("Current drawing canceled", "info");
  }

  function setTool(toolName) {
    state.currentTool = toolName;
    dom.toolFenceBtn.classList.toggle("active", toolName === "fence");
    dom.toolFenceBtn.setAttribute("aria-checked", String(toolName === "fence"));
    dom.toolZoneBtn.classList.toggle("active", toolName === "zone");
    dom.toolZoneBtn.setAttribute("aria-checked", String(toolName === "zone"));

    dom.closeZoneBtn.hidden = toolName !== "zone";
    updateUI();
  }

  // --- Validation and UI Updates ---

  function validateGeometry() {
    const fenceValid = state.fencePoints.length === 2;
    const zoneValid = state.zonePoints.length === 0 || (state.zonePoints.length >= 3 && state.zoneClosed);

    return {
      isValid: fenceValid && zoneValid,
      fenceValid,
      zoneValid,
    };
  }

  function buildDraftConfig() {
    return {
      fence: {
        start: state.fencePoints[0] || [0.0, 0.5],
        end: state.fencePoints[1] || [1.0, 0.5],
        alert_direction: state.alertDirection,
      },
      zone: state.zoneClosed && state.zonePoints.length >= 3 ? state.zonePoints : [],
    };
  }

  function updateUI() {
    const { isValid, fenceValid, zoneValid } = validateGeometry();

    // Save Button
    dom.saveConfigBtn.disabled = !isValid || state.isSaving || state.isLoading;

    // Status Badge
    if (isValid) {
      dom.geometryStatusBadge.textContent = "Geometry Valid (Ready to Save)";
      dom.geometryStatusBadge.className = "badge-status valid";
    } else {
      let issues = [];
      if (!fenceValid) issues.push(`Fence (${state.fencePoints.length}/2 pts)`);
      if (!zoneValid) issues.push(`Zone (${state.zonePoints.length} pts, unclosed)`);
      dom.geometryStatusBadge.textContent = `Invalid: ${issues.join(" · ")}`;
      dom.geometryStatusBadge.className = "badge-status invalid";
    }

    // Close Zone Button Visibility
    dom.closeZoneBtn.hidden = state.currentTool !== "zone" || state.zoneClosed || state.zonePoints.length < 3;

    // Debug Code Inspector
    const draft = buildDraftConfig();
    dom.debugDraftJson.textContent = JSON.stringify(draft, null, 2);

    draw();
  }

  // --- Canvas Rendering ---

  function draw() {
    const rect = dom.calCanvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    ctx.clearRect(0, 0, rect.width, rect.height);

    // 1. Draw Restricted Zone
    drawZone(rect);

    // 2. Draw Virtual Fence Line & Direction Arrow
    drawFence(rect);

    // 3. Draw In-Progress Preview
    drawLiveCursorPreview(rect);
  }

  function drawZone(rect) {
    if (state.zonePoints.length === 0) return;

    const pts = state.zonePoints.map(denormalizePoint);

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      ctx.lineTo(pts[i][0], pts[i][1]);
    }

    if (state.zoneClosed) {
      ctx.closePath();
      ctx.fillStyle = "rgba(255, 180, 84, 0.22)";
      ctx.fill();
      ctx.strokeStyle = "#ffb454";
      ctx.lineWidth = 2.5;
      ctx.stroke();
    } else {
      ctx.strokeStyle = "rgba(255, 180, 84, 0.75)";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.stroke();
    }

    // Draw vertex handles
    pts.forEach(([x, y], idx) => {
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fillStyle = "#ffb454";
      ctx.fill();
      ctx.strokeStyle = "#090d13";
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Label vertex index
      ctx.font = "bold 10px monospace";
      ctx.fillStyle = "#ffffff";
      ctx.fillText(`Z${idx + 1}`, x + 7, y - 5);
    });

    ctx.restore();
  }

  function drawFence(rect) {
    if (state.fencePoints.length === 0) return;

    const pts = state.fencePoints.map(denormalizePoint);

    ctx.save();
    if (pts.length === 2) {
      // Completed Fence Line
      const [p1, p2] = pts;

      ctx.beginPath();
      ctx.moveTo(p1[0], p1[1]);
      ctx.lineTo(p2[0], p2[1]);
      ctx.strokeStyle = "#ff5a66";
      ctx.lineWidth = 3;
      ctx.stroke();

      // Draw Direction Arrow
      drawDirectionArrow(p1, p2);

      // Endpoint Labels (A and B)
      drawFenceHandle(p1[0], p1[1], "A (Start)");
      drawFenceHandle(p2[0], p2[1], "B (End)");
    } else if (pts.length === 1) {
      drawFenceHandle(pts[0][0], pts[0][1], "A (Start)");
    }
    ctx.restore();
  }

  function drawFenceHandle(x, y, label) {
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fillStyle = "#ff5a66";
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.font = "bold 11px monospace";
    ctx.fillStyle = "#ffffff";
    ctx.fillText(label, x + 8, y - 7);
  }

  function drawDirectionArrow(p1, p2) {
    const midX = (p1[0] + p2[0]) / 2;
    const midY = (p1[1] + p2[1]) / 2;
    const dx = p2[0] - p1[0];
    const dy = p2[1] - p1[1];
    const len = Math.hypot(dx, dy);
    if (len < 10) return;

    // Normal vector perpendicular to start -> end (Points to the left side A)
    // Left normal = (-dy / len, dx / len)
    const nx = -dy / len;
    const ny = dx / len;
    const arrowLen = 22;

    ctx.save();
    ctx.strokeStyle = "#5ee0a0";
    ctx.fillStyle = "#5ee0a0";
    ctx.lineWidth = 2;

    if (state.alertDirection === "A_TO_B") {
      // Crossing from left to right (A side to B side) -> vector points to right side: -normal
      const tipX = midX - nx * arrowLen;
      const tipY = midY - ny * arrowLen;
      drawArrowHead(midX, midY, tipX, tipY);
      ctx.font = "bold 11px sans-serif";
      ctx.fillText("ALERT: A \u2192 B", tipX + 6, tipY + 4);
    } else if (state.alertDirection === "B_TO_A") {
      // Crossing from right to left (B side to A side) -> vector points to left side: +normal
      const tipX = midX + nx * arrowLen;
      const tipY = midY + ny * arrowLen;
      drawArrowHead(midX, midY, tipX, tipY);
      ctx.font = "bold 11px sans-serif";
      ctx.fillText("ALERT: B \u2192 A", tipX + 6, tipY + 4);
    } else {
      // ANY direction: bilateral indicator
      const tip1X = midX + nx * (arrowLen * 0.7);
      const tip1Y = midY + ny * (arrowLen * 0.7);
      const tip2X = midX - nx * (arrowLen * 0.7);
      const tip2Y = midY - ny * (arrowLen * 0.7);
      drawArrowHead(midX, midY, tip1X, tip1Y);
      drawArrowHead(midX, midY, tip2X, tip2Y);
      ctx.font = "bold 11px sans-serif";
      ctx.fillText("ALERT: ANY", tip1X + 6, tip1Y + 4);
    }
    ctx.restore();
  }

  function drawArrowHead(fromX, fromY, toX, toY) {
    const headLen = 8;
    const angle = Math.atan2(toY - fromY, toX - fromX);

    ctx.beginPath();
    ctx.moveTo(fromX, fromY);
    ctx.lineTo(toX, toY);
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(toX, toY);
    ctx.lineTo(toX - headLen * Math.cos(angle - Math.PI / 6), toY - headLen * Math.sin(angle - Math.PI / 6));
    ctx.lineTo(toX - headLen * Math.cos(angle + Math.PI / 6), toY - headLen * Math.sin(angle + Math.PI / 6));
    ctx.closePath();
    ctx.fill();
  }

  function drawLiveCursorPreview(rect) {
    if (!state.hoverPoint) return;
    const hoverPx = denormalizePoint(state.hoverPoint);

    ctx.save();
    if (state.currentTool === "fence" && state.fencePoints.length === 1) {
      const p1 = denormalizePoint(state.fencePoints[0]);
      ctx.beginPath();
      ctx.moveTo(p1[0], p1[1]);
      ctx.lineTo(hoverPx[0], hoverPx[1]);
      ctx.strokeStyle = "rgba(255, 90, 102, 0.7)";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.stroke();
    } else if (state.currentTool === "zone" && !state.zoneClosed && state.zonePoints.length > 0) {
      const lastPt = denormalizePoint(state.zonePoints[state.zonePoints.length - 1]);
      ctx.beginPath();
      ctx.moveTo(lastPt[0], lastPt[1]);
      ctx.lineTo(hoverPx[0], hoverPx[1]);
      ctx.strokeStyle = "rgba(255, 180, 84, 0.7)";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.stroke();
    }
    ctx.restore();
  }

  // --- Save Configuration ---

  async function saveConfiguration() {
    const { isValid } = validateGeometry();
    if (!isValid || !state.selectedCameraId) return;

    state.isSaving = true;
    dom.saveConfigBtn.disabled = true;
    dom.saveConfigBtn.textContent = "Saving...";
    showStatus("Submitting configuration to edge node...", "info");

    const payload = buildDraftConfig();

    try {
      const validatedConfig = await window.IBVAP_API.updateCameraConfig(state.selectedCameraId, payload);
      state.savedConfig = validatedConfig;
      dom.debugSavedJson.textContent = JSON.stringify(validatedConfig, null, 2);

      showStatus(`Configuration saved and validated for ${state.selectedCameraId}`, "success");
    } catch (err) {
      showStatus(`Save failed: ${err.message}`, "error");
    } finally {
      state.isSaving = false;
      dom.saveConfigBtn.textContent = "Save Configuration";
      updateUI();
    }
  }

  // --- Status & Error Banners ---

  function showStatus(message, type = "info") {
    dom.calStatusBanner.hidden = false;
    dom.calStatusBanner.className = `cal-status-banner status-${type}`;
    dom.calStatusBanner.textContent = message;

    if (type === "success" || type === "info") {
      setTimeout(() => {
        if (dom.calStatusBanner.textContent === message) {
          dom.calStatusBanner.hidden = true;
        }
      }, 5000);
    }
  }

  function showUnreachable(msg) {
    dom.unreachableBanner.hidden = false;
    dom.unreachableText.textContent = msg;
    dom.connectionStatus.textContent = `Error: ${msg}`;
  }

  function hideUnreachable() {
    dom.unreachableBanner.hidden = true;
    const modeLabel = window.IBVAP_API.isMockMode() ? "Mock Mode" : "Live";
    dom.connectionStatus.textContent = `${modeLabel} — synced ${new Date().toLocaleTimeString()}`;
  }
})();
