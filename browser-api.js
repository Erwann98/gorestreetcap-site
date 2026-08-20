(() => {
  const SAVE_KEY = "gridvault-v0.5-browser-session";
  let nextId = 1;
  const pending = new Map();
  const worker = new Worker(new URL("./engine-worker.js", document.baseURI), { type: "module" });

  function setEngineStatus(message) {
    window.dispatchEvent(new CustomEvent("gridvault-engine-status", { detail: message }));
    const status = document.getElementById("status");
    if (status && (!window.__gridvaultGameStarted || message.includes("ready") || message.includes("Restoring"))) {
      status.textContent = message;
    }
  }

  worker.addEventListener("message", (event) => {
    if (event.data?.type === "status") {
      setEngineStatus(event.data.message);
      return;
    }
    const slot = pending.get(event.data?.id);
    if (!slot) return;
    pending.delete(event.data.id);
    if (event.data.error) slot.reject(new Error(event.data.error));
    else slot.resolve(event.data.result);
  });

  worker.addEventListener("error", (event) => {
    const message = `Browser engine failed: ${event.message || "worker error"}`;
    setEngineStatus(message);
    for (const slot of pending.values()) slot.reject(new Error(message));
    pending.clear();
  });

  function call(command, args = {}) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      worker.postMessage({ id, command, args });
    });
  }

  function body(options) {
    if (!options?.body) return {};
    return typeof options.body === "string" ? JSON.parse(options.body) : options.body;
  }

  function readSave() {
    try { return JSON.parse(localStorage.getItem(SAVE_KEY) || "null"); }
    catch { return null; }
  }

  function writeSave(save) {
    try {
      localStorage.setItem(SAVE_KEY, JSON.stringify(save));
      window.dispatchEvent(new CustomEvent("gridvault-save-updated"));
    } catch (error) {
      console.warn("GridVault browser save failed", error);
    }
  }

  window.gridvaultApi = {
    runtime: "browser-pyodide",
    hasSavedGame() {
      return Boolean(readSave()?.startPayload);
    },
    clearSavedGame() {
      localStorage.removeItem(SAVE_KEY);
      window.dispatchEvent(new CustomEvent("gridvault-save-updated"));
    },
    async resumeLast() {
      const saved = readSave();
      if (!saved?.startPayload) throw new Error("No saved browser session found");
      setEngineStatus("Restoring saved market day…");
      const initial = await call("start", { payload: saved.startPayload });
      let daResult = null;
      let last = null;
      const periods = [];
      if (saved.daPayload) {
        daResult = await call("dayAhead", { gameId: initial.game_id, payload: saved.daPayload });
        for (let i = 0; i < (saved.rtPayloads || []).length; i++) {
          setEngineStatus(`Restoring saved market day… ${i + 1}/${saved.rtPayloads.length} RT periods`);
          last = await call("realTime", { gameId: initial.game_id, payload: saved.rtPayloads[i] });
          periods.push(last.cleared_period);
        }
      }
      window.__gridvaultGameStarted = true;
      const phase = last?.phase || daResult?.phase || initial.phase;
      const progress = last?.progress || { periods_cleared: 0, periods_total: 48, cash_pnl: 0, soc_mwh: initial.metadata.starting_soc_mwh, equivalent_cycles: 0 };
      setEngineStatus("Browser solver ready · saved day restored");
      return {
        ...initial,
        game_id: initial.game_id,
        phase,
        day_ahead_schedule: daResult?.day_ahead_schedule || [],
        day_ahead_summary: daResult?.day_ahead_summary || null,
        briefing: last ? last.briefing : daResult?.briefing || null,
        progress,
        periods,
        result: last?.result || null,
      };
    },
    async request(url, options = {}) {
      const method = (options.method || "GET").toUpperCase();
      if (url === "/health" || url === "./health") return call("health");
      if (url === "/api/game/start" && method === "POST") {
        const startPayload = body(options);
        const result = await call("start", { payload: startPayload });
        window.__gridvaultGameStarted = true;
        writeSave({ startPayload, daPayload: null, rtPayloads: [], savedAt: new Date().toISOString() });
        return result;
      }
      const da = url.match(/^\/api\/game\/([^/]+)\/day-ahead$/);
      if (da && method === "POST") {
        const daPayload = body(options);
        const result = await call("dayAhead", { gameId: da[1], payload: daPayload });
        const saved = readSave();
        if (saved) writeSave({ ...saved, daPayload, rtPayloads: [], savedAt: new Date().toISOString() });
        return result;
      }
      const rt = url.match(/^\/api\/game\/([^/]+)\/real-time$/);
      if (rt && method === "POST") {
        const rtPayload = body(options);
        const result = await call("realTime", { gameId: rt[1], payload: rtPayload });
        const saved = readSave();
        if (saved) writeSave({ ...saved, rtPayloads: [...(saved.rtPayloads || []), rtPayload], savedAt: new Date().toISOString() });
        return result;
      }
      const state = url.match(/^\/api\/game\/([^/]+)$/);
      if (state && method === "GET") return call("state", { gameId: state[1] });
      throw new Error(`Unsupported browser API route: ${method} ${url}`);
    },
  };

  setEngineStatus("Browser edition · solver loads when you open a trading day");
})();
