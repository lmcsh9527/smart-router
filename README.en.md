# TokenSaver / Smart Router

[中文](README.md) | **English**

An OpenAI-compatible **smart routing gateway that saves token costs** — it automatically judges task complexity (simple / medium / complex), routes simple questions to cheap lightweight models and complex questions to powerful models, so you save money without manually picking models.

It bundles the OpenSquilla classifier (vendored into this repo) plus a **model pool + provider library** admin UI with discover-style model onboarding and one-click functional verification.

## ✨ Features

- 🧠 **Smart routing**: OpenSquilla SquillaRouter auto-classifies into tiers (c0-c3); simple → lightweight model, complex → strong model
- 🔌 **OpenAI compatible**: `POST /v1/chat/completions` (non-stream + SSE stream); any client works with `base_url=http://localhost:20130/v1` + `model=auto`
- 🗂️ **Model pool + provider library**: manage multiple models in the admin UI, each with its own base_url / key / tiers / priority
- 🔍 **Discover-style onboarding**: enter Base URL + Key → click "Fetch models" → auto-list models → check and batch-add to the pool
- ✅ **Real functional verification**: testing is not just connectivity — it sends `1+1=?` and checks the model **actually replies correctly**; image models are tested via the image API; pass = green / fail = red, persisted
- 📊 **Live call monitoring**: stat cards (total / success rate / avg / P95 / total tokens / total cost), distribution by model / tier / provider / **cost**, failure / degradation / waste alerts, request summaries, stream token accounting
- 💸 **Cost insights**: per-model cost distribution + "simple task hit high-end model" waste alert — see where the money goes at a glance
- 🔄 **Auto fallback chain**: high-end model failure degrades to lower tiers so requests still complete
- 📏 **Context window auto-detection**: reads from `/v1/models` or the local model catalog (1M / 200K…)

## 📸 Screenshots

![Admin overview](docs/screenshots/04-full-page.png)

| Top stats | Model pool |
|---|---|
| ![Top stats](docs/screenshots/01-overview-top.png) | ![Model pool](docs/screenshots/02-model-pool.png) |

## 🏗️ Architecture

```
Any OpenAI client (DSH / Hermes / curl ...)
        │  base_url=http://localhost:20130/v1, model=auto
        ▼
┌─────────────────────────────┐
│  TokenSaver Gateway (FastAPI)│
│  1. classify() tier c0-c3    │
│  2. look up model pool       │
│  3. forward to provider API  │
└─────────────────────────────┘
        │
        ├── Provider A (tokenrhythm.studio) ── flash / pro
        ├── Provider B (lightboat.dpdns.org) ── glm5.2 / minimaxm3 ...
        └── Provider C ... (add freely in admin UI)

Classifier: vendor/opensquilla (SquillaRouter assets vendored in repo, Apache-2.0)
```

> Fully independent of 9router: routing, forwarding and fallback are all handled by TokenSaver itself.

## 🚀 Quick Start

### Requirements

- Python 3.10+
- `pip install -r requirements.txt` (fastapi / httpx / uvicorn; classifier assets already in `vendor/`)

### Run

```bash
cd tokensaver

# 1. Configure API key (recommended: write it to a 0600 file in the project dir,
#    then reference the file in the admin UI). Example: write the key to jy_api_key.
#    Or just paste the key in the admin page.

# 2. Start the gateway (first start loads the router model, takes a few seconds)
python3 gateway.py
#   or uvicorn gateway:app --host 0.0.0.0 --port 20130

# 3. Smoke test
curl http://localhost:20130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"1+1等于几"}]}'

# Streaming
curl -N http://localhost:20130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"写一个B树实现"}],"stream":true}'
```

### Admin UI

Open **http://localhost:20130/admin** in a browser.

| Feature | Description |
|---|---|
| Discover models | Enter Base URL + Key → Fetch models → check & batch-add (provider auto-saved) |
| Model pool | Test (1+1 verify / image verify), edit, delete, enable/disable toggle, inline priority edit |
| Provider library | Manage providers (URL/Key auto-saved, pick next time) |
| Live calls | Stat cards + distributions + failure/degradation alerts + filtered details |

### Example configs

```bash
cp providers.example.json providers.json          # provider template
cp models_pool.example.json models_pool.json      # model pool template
```

> ⚠️ `models_pool.json` / `providers.json` / `*_api_key` contain real credentials and are in `.gitignore` — **never commit them**.

## 🎛️ Routing Tiers

| Tier | Meaning | Suggested model |
|---|---|---|
| c0 | Trivial (chat / one-step Q&A) | lightweight flash |
| c1 | Simple | lightweight flash |
| c2 | Medium (analysis / code) | strong model pro |
| c3 | Complex (deep reasoning / long text) | strongest model |

- Upgrade words: "use the best model / deep thinking" → force c3
- Explicit model: client passes a concrete model name → passthrough (matched against the pool)

## 📡 API

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Chat (auto routing / explicit model passthrough) |
| `GET /v1/models` | Model list |
| `GET /healthz` | Health check |
| `GET /admin` | Admin UI |
| `GET/POST /admin/api/pool` | Model pool CRUD |
| `POST /admin/api/pool/{id}/test` | Functional verification (text 1+1 / image) |
| `GET/POST /admin/api/providers` | Provider CRUD |
| `POST /admin/api/discover` | Discover models (auto-save provider) |
| `GET /admin/api/usage` | Live call stats |
| `GET /admin/api/status` | Routing status preview |

## 📄 License

- This project: Apache-2.0 (see LICENSE)
- Bundled classifier from [OpenSquilla](https://github.com/opensquilla/opensquilla) (Apache-2.0, model assets distributed with this repo)
- Inspired by OpenSquilla smart routing + open gateway ecosystem

## 🧑‍💻 Development

- `CHANGELOG.md`: version history
- `docs/`: design / config / FAQ
- `CONTRIBUTING.md`: contribution guide
