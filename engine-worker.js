import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.5/full/pyodide.mjs";

const ENGINE_FILES = [
  "__init__.py",
  "game.py",
  "game_types.py",
  "market.py",
  "models.py",
  "rolling_rt.py",
  "scenario.py",
  "simulation.py",
  "unit_commitment.py",
];

const baseUrl = new URL(".", import.meta.url);
let readyPromise = null;
let pyodide = null;

function status(message) {
  self.postMessage({ type: "status", message });
}

async function initialise() {
  if (readyPromise) return readyPromise;
  readyPromise = (async () => {
    status("Loading browser market engine…");
    pyodide = await loadPyodide({
      indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.5/full/",
    });
    status("Loading NumPy + SciPy optimiser…");
    await pyodide.loadPackage(["numpy", "scipy"]);

    pyodide.FS.mkdirTree("/gridvault/battery_market_game");
    status("Loading GridVault market model…");
    for (const filename of ENGINE_FILES) {
      const response = await fetch(new URL(`./engine/${filename}`, baseUrl));
      if (!response.ok) throw new Error(`Could not load engine/${filename}: HTTP ${response.status}`);
      pyodide.FS.writeFile(
        `/gridvault/battery_market_game/${filename}`,
        await response.text(),
        { encoding: "utf8" },
      );
    }

    await pyodide.runPythonAsync(`
import json
import sys

if "/gridvault" not in sys.path:
    sys.path.insert(0, "/gridvault")

from battery_market_game.game import GameSession, PeriodBidPlan
from scipy.optimize import Bounds, LinearConstraint, linprog, milp
import numpy as np
import scipy

# Runtime gate: the browser edition is only declared ready after both the LP
# and MILP paths used by GridVault solve a tiny deterministic problem.
_lp_check = linprog([1.0], bounds=[(1.0, None)], method="highs")
_milp_check = milp(
    [-1.0],
    integrality=np.array([1]),
    bounds=Bounds([0.0], [1.0]),
    constraints=LinearConstraint([[1.0]], [-np.inf], [1.0]),
)
if not _lp_check.success or abs(float(_lp_check.x[0]) - 1.0) > 1e-8:
    raise RuntimeError("Browser LP self-test failed")
if not _milp_check.success or abs(float(_milp_check.x[0]) - 1.0) > 1e-8:
    raise RuntimeError("Browser MILP self-test failed")

_browser_diagnostics = {
    "scipy_version": scipy.__version__,
    "lp_ok": True,
    "milp_ok": True,
}
_browser_sessions = {}

def _json_default(value):
    # Defensive serializer for NumPy scalar values returned by optimiser code.
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

def _dump(value):
    return json.dumps(value, default=_json_default, separators=(",", ":"))

def browser_start(payload_json):
    payload = json.loads(payload_json)
    game = GameSession(**payload)
    _browser_sessions[game.id] = game
    return _dump(game.start_payload())

def browser_day_ahead(game_id, payload_json):
    payload = json.loads(payload_json)
    game = _browser_sessions.get(game_id)
    if game is None:
        raise ValueError("Game session not found. Start a new market day.")
    bids = [PeriodBidPlan(**row) for row in payload["bids"]]
    return _dump(game.clear_day_ahead(bids))

def browser_real_time(game_id, payload_json):
    payload = json.loads(payload_json)
    game = _browser_sessions.get(game_id)
    if game is None:
        raise ValueError("Game session not found. Start a new market day.")
    bid = PeriodBidPlan(**payload["bid"])
    return _dump(game.step_real_time(bid))

def browser_state(game_id):
    game = _browser_sessions.get(game_id)
    if game is None:
        raise ValueError("Game session not found. Start a new market day.")
    payload = {
        "game_id": game.id,
        "phase": game.phase,
        "metadata": game._metadata(),
        "forecast": [x.to_dict() for x in game.forecasts],
        "progress": game.progress_payload(),
    }
    if game.da_schedule:
        payload["day_ahead_schedule"] = [x.to_dict() for x in game.da_schedule]
    if game.phase == "real_time":
        payload["briefing"] = game.current_briefing()
    if game.phase == "complete":
        payload["result"] = game.final_result()
    return _dump(payload)
`);
    status("Browser solver ready · no server required");
    return true;
  })();
  return readyPromise;
}

async function runCommand(command, args) {
  await initialise();
  const payload = JSON.stringify(args.payload ?? {});
  let fn;
  let result;
  if (command === "start") {
    fn = pyodide.globals.get("browser_start");
    result = fn(payload);
  } else if (command === "dayAhead") {
    fn = pyodide.globals.get("browser_day_ahead");
    result = fn(args.gameId, payload);
  } else if (command === "realTime") {
    fn = pyodide.globals.get("browser_real_time");
    result = fn(args.gameId, payload);
  } else if (command === "state") {
    fn = pyodide.globals.get("browser_state");
    result = fn(args.gameId);
  } else if (command === "health") {
    fn = pyodide.globals.get("_dump");
    result = fn(pyodide.globals.get("_browser_diagnostics"));
    const diagnostics = JSON.parse(String(result));
    if (result?.destroy) result.destroy();
    if (fn?.destroy) fn.destroy();
    return { ok: true, version: "0.5.0-alpha", runtime: "pyodide", ...diagnostics };
  } else {
    throw new Error(`Unknown browser-engine command: ${command}`);
  }
  try {
    return JSON.parse(String(result));
  } finally {
    if (fn?.destroy) fn.destroy();
    if (result?.destroy) result.destroy();
  }
}

self.onmessage = async (event) => {
  const { id, command, args = {} } = event.data || {};
  if (id == null) return;
  try {
    const result = await runCommand(command, args);
    self.postMessage({ id, result });
  } catch (error) {
    console.error(error);
    self.postMessage({
      id,
      error: error?.message || String(error),
      stack: error?.stack || null,
    });
  }
};
