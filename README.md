# bunnysplit

Maintainer's deployed version: https://bunnysplit.zliang.me/

Split bills fairly. Add people, log items, settle up — **all local**.

A privacy-first, zero-backend bill splitter that runs entirely in your browser using [PyScript](https://pyscript.net/) + MicroPython. No accounts, no servers — your bill data never leaves your device.

![License: CCAI-1.0](https://img.shields.io/badge/license-CCAI--1.0-blue.svg)
![Tests](https://img.shields.io/badge/tests-54%20passing-success)
![Python](https://img.shields.io/badge/python-3.x-blue)

---

## Why bunnysplit?

- **Private by design** — Everything lives in your browser’s `localStorage`.
- **Works offline** — Once loaded, the app and PyScript runtime are cached (initial load requires network access to fetch the pinned PyScript runtime from CDN). Close your laptop on a plane and it still works.
- **Exact money math** — All money values are stored and settled as integer cents with guaranteed conservation (no lost or invented pennies). Uneven splits use floating-point only for weight proportions before converting back to exact integer shares.
- **Weighted splits** — Split items equally or by custom weights (great for different appetites or shared dishes).
- **Settle up instantly** — See exactly who owes whom with a minimal transfer list.
- **Installable** — A real PWA with icons, standalone mode, and a service worker.

No build step. No bundler. Just static files + Python in the browser.

As a purely static application, it can be hosted on virtually any static website hosting service.

---

## Quick start

### Try it locally

No build or install step required.

```bash
# Any static file server works
python3 -m http.server 8000

# Then open http://localhost:8000
```

Or simply drag `index.html` into any modern browser (core functionality works this way). For full PWA and offline support (including the service worker), use a local HTTP server such as `python3 -m http.server`. The first time you load it you’ll see the empty workspace — add a few people, then start adding expenses. Everything saves automatically.

### Run the tests

The money logic is pure Python and runs under normal CPython:

```bash
python -m unittest discover -s tests
```

### Syntax checks (matches CI)

```bash
python -m py_compile main.py ui.py storage.py splitcore/*.py
python -m py_compile infra/__main__.py
```

Additional service-worker and staging syntax checks:

```bash
node --check sw.js
bash -n scripts/stage-assets.sh
```

---

## Features

- Add/remove people with stable per-person avatars
- Add expenses with:
  - Description and amount
  - Who paid
  - Which subset of people participated
  - Equal split or per-person weights
- Live updating:
  - Total billed + per-person average
  - Per-person amounts owed
  - “Settle up” transfer list (greedy, minimal transfers)
- Delete items; removing people is blocked if they are referenced by existing items
- State is saved automatically and survives reloads (and private browsing gracefully degrades)
- Back up the whole bill as a JSON file and restore it later or on another device; importing over an existing bill asks before replacing it
- Responsive layout that works on phones and desktops

---

## How it works

```
Browser
┌─────────────────────────────────────────────────────────────┐
│ index.html  +  PyScript (MicroPython)                       │
│                                                             │
│  main.py ──► storage.load() ──► ui.start()                  │
│               │                                             │
│               ▼                                             │
│         localStorage["bunnysplit"]  (JSON)                  │
│                                                             │
│  ui.py (DOM + events)                                       │
│    └── splitcore/                                           │
│          ├── model.py   (Person, Item, AppState)            │
│          └── calc.py    (split_item, per_person_totals,     │
│                           settle_up)                        │
│                                                             │
│  sw.js  (service worker for offline + cache)                │
└─────────────────────────────────────────────────────────────┘
```

### Key architectural boundaries

- **`splitcore/`** — Pure Python. No DOM, no `pyscript`, no browser APIs. This is the only code that needs to behave identically under CPython (tests) and MicroPython (browser).
- **`ui.py`** + **`storage.py`** — Browser-only. Talk to the DOM and `localStorage`.
- **`main.py`** — Tiny bootstrap: load state then hand off to the UI.
- All money values are stored and settled as integer cents. `MAX_CENTS` and weight caps (`_MAX_WEIGHT`) keep the uneven-split math safe and exact. Floating point is used only for proportions during uneven splits and immediately converted to integer pennies.

---

## Project layout

```
.
├── index.html           # App shell + PyScript bootstrap
├── pyscript.toml        # MicroPython import allowlist
├── main.py              # Entry point
├── ui.py                # Rendering + event handlers
├── storage.py           # localStorage persistence + backup file format
├── splitcore/
│   ├── __init__.py
│   ├── model.py         # Data model + (de)serialization
│   └── calc.py          # Pure split & settle logic
├── sw.js                # Service worker (cache plumbing only)
├── styles.css
├── manifest.webmanifest # PWA manifest
├── icons/               # PWA icons (referenced by manifest, index.html, sw.js, stage)
├── scripts/
│   └── stage-assets.sh  # Prepares ./dist for deploy
├── tests/               # Core, storage/UI, staging, and service-worker tests
├── infra/               # Pulumi deployment (Cloudflare)
└── docs/                # Historical specs & audits
```

---

## Development

- Prefer `createElement` + `textContent` over `innerHTML` in `ui.py`.
- Never introduce floats into settlement math.
- Before touching `splitcore/`, understand that `AppState.from_dict()` is the persisted-state trust boundary.
- Run `./scripts/stage-assets.sh` before any manual `pulumi up`.
- `dist/` is generated — do not edit it by hand.

---

## Deployment

bunnysplit is a completely static front-end application. It consists only of HTML, CSS, a minimal service worker, and Python code that executes in the browser through PyScript. There is no backend, no server-side logic, and no build process required to run the app.

### Deploying to any static host

Run the staging script to assemble the front-end assets:

```bash
./scripts/stage-assets.sh
```

The resulting `dist/` directory holds the complete static application. Because it is purely static, the contents of `dist/` can be deployed to **any static website hosting service**. Common options include:

- Netlify
- Vercel
- GitHub Pages
- Cloudflare Pages/Workers
- AWS S3 (standalone or fronted by CloudFront)
- Firebase Hosting
- Render
- Any traditional web server (nginx, Caddy, Apache, etc.)

Most static hosts automatically provide HTTPS, which is required for the service worker to enable the full PWA and offline experience in production environments.

Staging uses an explicit asset manifest and replaces `dist/` with only those files. When adding a browser/runtime asset, add it to `scripts/stage-assets.sh` and its regression test.

For the simplest experience, deploy at the root of a domain (or subdomain). Deployments under a subdirectory may require adjustments to service worker scope and asset paths.

### Current official deployment

The project is currently deployed on a Cloudflare **assets-only Worker** using Pulumi. This choice mainly simplifies custom domain setup, CI/CD with deployment gates, and fits the existing infrastructure (Pulumi state is stored in an S3-compatible object storage backend).

- `./scripts/stage-assets.sh` prepares `./dist` and derives its service-worker cache version from the staged content
- `pulumi/actions` deploys from `infra/` (no wrangler)

See [infra/README.md](infra/README.md) for complete setup instructions, including required secrets, the DIY S3 backend, and custom domain configuration.

If you host the app elsewhere, you generally only need the contents of `dist/` — the Pulumi configuration in `infra/` is specific to the current Cloudflare deployment.

---

## Tech stack

- **Runtime**: PyScript 2024.11.1 + MicroPython
- **Language**: Python (plain classes, no dataclasses)
- **UI**: Vanilla DOM + CSS (no framework)
- **Persistence**: `localStorage`
- **Offline**: Service worker + explicit app shell
- **Deploy**: Any static host (current setup uses Pulumi + Cloudflare assets-only Worker)
- **Tests**: stdlib `unittest` (runs under plain CPython)

---

## License

CCAI License v1.0 (Contextual Copyleft AI). See [LICENSE.txt](LICENSE.txt).

A copyleft license that extends to models, datasets, and systems trained on this code or its outputs. It supports open-source AI and pushes back against closed labs that train on communal data but withhold their results.

---

## Credits

Built as a demonstration of what a fully static, high-fidelity Python-in-the-browser application can look like while still maintaining strong correctness guarantees around money.

Enjoy splitting bills with zero drama (and zero servers).
