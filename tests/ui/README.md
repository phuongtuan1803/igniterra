# UI verification with isolated fake providers

Start from the repository root:

```sh
UV_CACHE_DIR=/tmp/igniterra-uv-cache uv run python tests/ui/fixture_server.py
```

The fixture uses a new temporary data directory, the existing fake model/GPU providers, a local authenticated Portal/ComfyUI HTTP server, and generated test media. It binds only `127.0.0.1:8765`. Its Python audit hook rejects non-loopback connections. It does not load the repository's operational `studio.toml`. The `__fixture` and `__reference` routes exist only in this test server and are not part of `studio.web`.

Wait for “Isolated fake UI fixture ready”. In another terminal:

```sh
npm install --prefix /tmp/igniterra-browser playwright --no-audit --no-fund
node tests/ui/verify.cjs
```

The script uses installed Google Chrome (`/usr/bin/google-chrome`); override `CHROME_PATH` and `PLAYWRIGHT_MODULE` if needed. No npm dependency or browser package is added to the application. Some restricted sandboxes require permission for localhost sockets and Chrome execution.

The browser blocks external requests. It checks 14 production views at 1440×1000, 768×1024 and 390×1000, saves reference screenshots at the same widths, checks document overflow/field labels/duplicate IDs, and exercises navigation, dialogs, forms, real stored state and error recovery. Later mobile menu interaction uses 390×844. Screenshot images are full-page captures, so a fixed bottom navigation appears at the viewport boundary within a tall image.

Workflow approval invokes the existing manifest CLI against the temporary database. Fake runtime controls advance mock provider/worker outcomes, including an unknown destroy and a failed output download. Application actions still use their production routes, CSRF checks, nonces and confirmations. Run success still requires actual downloaded bytes and verification by the service.

Use a fresh fixture process for every complete run: the browser checks intentionally mutate its test data. Stop the fixture with Ctrl+C. Evidence is written to `docs/ux/evidence/`; the final JSON report is `browser-results.json`.
