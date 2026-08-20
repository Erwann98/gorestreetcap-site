# GridVault GitHub Pages build

This directory is a self-contained static build of GridVault. The market engine runs
inside the player's browser with Pyodide/WebAssembly; no FastAPI server is required.

## Deploy into an existing GitHub Pages repository

If the repository already publishes a website, copy the **contents** of this `docs/`
directory into the folder that GitHub Pages currently publishes. The application uses
relative URLs, so it works at both user sites (`https://user.github.io/`) and project
sites (`https://user.github.io/repository/`).

If the repository does not have Pages configured yet:

1. Copy this `docs/` directory into the repository root.
2. In GitHub open **Settings → Pages**.
3. Under **Build and deployment**, choose **Deploy from a branch**.
4. Select the site's branch (normally `main`) and folder **`/docs`**.
5. Save.

## Runtime requirements

GitHub Pages must be served over HTTPS (the normal Pages default). On first use the
browser downloads Pyodide plus NumPy/SciPy from jsDelivr. GridVault's own Python engine
is served from `./engine/` and runs in a module Web Worker. Before the UI opens a market
day the worker executes tiny LP and MILP self-tests; a failed solver load is surfaced in
the status bar instead of silently starting a broken game.

The browser edition also stores the deterministic game inputs in local storage. If the
page is refreshed, **Resume saved browser day** rebuilds the same scenario and replays
the locked DA/RT decisions through the engine.

## Rebuild after source changes

From the project root:

```bash
python scripts/build_github_pages.py
```

Then commit the refreshed `docs/` directory.

## Existing-site integration

If GridVault needs to live under a route such as `/game/` in an existing public GitHub
site, put all files from this directory under that route/folder. Do not rename the
`engine/` directory unless you also update `engine-worker.js`.
