<div align="center">

# 💰 TokenSaver · Smart Router

**OpenAI-compatible smart routing gateway that saves you money**
Easy questions automatically go to lightweight models — expensive models are only used when they're actually needed

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)](#quick-start)
[![Release](https://img.shields.io/github/v/tag/lmcsh9527/smart-router?label=release&color=green)](https://github.com/lmcsh9527/smart-router/releases)
[![Stars](https://img.shields.io/github/stars/lmcsh9527/smart-router?style=social)](https://github.com/lmcsh9527/smart-router/stargazers)

![Live usage dashboard](docs/screenshots/hero-dashboard.png)

*Live call monitoring — volume / success rate / P95 / tokens / cost, at a glance*

[中文](README.md) · English

</div>

---

## Why you need it

Once you connect multiple LLM providers, two kinds of waste are common:

- **Using a sledgehammer to crack a nut**: casual chats and single-step Q&A hitting your most expensive model
- **Manual model picking**: pausing before every message to wonder "is this one hard?"

TokenSaver adds a gateway in between: clients always send `model=auto`, a built-in classifier grades each request's complexity (tiers c0-c3), **easy requests route to lightweight models and hard ones escalate to strong models**, with an automatic fallback chain when anything fails.

## ✨ Features

| | Feature | Details |
|---|---|---|
| 🧠 | **Smart tiered routing** | OpenSquilla SquillaRouter classifier (c0-c3), vendored in-repo, works offline |
| 🔌 | **OpenAI compatible** | Streaming (SSE) & non-streaming; point any client at `base_url` + `model=auto` |
| 🗂️ | **Model pool + provider registry** | Visual admin panel; per-model base_url / key / tier / priority |
| 🔍 | **Discovery-based setup** | Enter Base URL + Key → fetch model list → bulk-add to pool |
| ✅ | **Real functional testing** | Not just a ping — sends `1+1=?` and verifies the answer; image models tested via image API |
| 📊 | **Live call monitoring** | Volume / success rate / P95 / tokens / cost cards, breakdowns by model/tier/provider, 👍👎 feedback on every request |
| 💸 | **Cost insights** | Cost distribution + "easy task on an expensive model" waste alerts |
| 🔄 | **Fallback chain** | Failures/timeouts on higher tiers degrade automatically |
| 🧠 | **Self-learning** (experimental) | Sample collection, correction feedback, training gates, model versioning |
| 📏 | **Context auto-detection** | Reads context length from `/v1/models` or the local catalog (1M / 200K…) |

## 📸 Screenshots

| Live monitoring | Model pool |
|---|---|
| ![Overview](docs/screenshots/01-overview-top.png) | ![Model pool](docs/screenshots/02-model-pool.png) |

## 🚀 Quick Start

```bash
bash install.sh             # ① install dependencies + initialize config
bash install.sh --launchd   # ② auto-start on boot + crash recovery (macOS, optional)
bash scripts/doctor.sh      # ③ health check
```

Then open the admin panel at **http://localhost:20130/admin**, add your provider (Base URL + Key → fetch models → add to pool), and connect from any client:

```
Base URL: http://localhost:20130/v1
Model:    auto          ← automatic tiered routing
API Key:  anything (the local gateway does not validate it)
```

<details>
<summary><b>Manual install / smoke test</b></summary>

```bash
# Requires Python ≥3.10
pip install -r requirements.txt     # classifier assets are vendored under vendor/
python3 gateway.py                  # or uvicorn gateway:app --host 0.0.0.0 --port 20130

# Smoke test
curl http://localhost:20130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"1+1等于几"}]}'
```

Config template: `cp models_pool.example.json models_pool.json`
</details>

<details>
<summary><b>Integrating with DSH Desktop</b></summary>

The admin panel offers one-click DSH setup; or add the provider manually in `settings.yaml` (see [docs/dsh-integration.md](docs/dsh-integration.md)).
</details>

## 🎛️ Routing tiers

| Tier | Meaning | Suggested model |
|---|---|---|
| c0 | Simplest (chat / single-step Q&A) | lightweight flash |
| c1 | Simple | lightweight flash |
| c2 | Medium (analysis / coding) | strong pro model |
| c3 | Complex (deep reasoning / long-form) | strongest model |

- Upgrade words: "use the best model" / "think deeply" → forced c3
- Explicit model names pass through directly (matched against the pool)

## 🏗️ Architecture

```
Any OpenAI client (DSH / Hermes / curl ...)
        │  base_url=http://localhost:20130/v1, model=auto
        ▼
┌─────────────────────────────┐
│  TokenSaver gateway (FastAPI)│
│  1. classify() → tier c0-c3 │
│  2. look up model pool      │
│  3. forward to provider API │
└─────────────────────────────┘
        │
        ├── Provider A ── flash / pro
        ├── Provider B ── other models ...
        └── Provider C ... (add freely in the panel)

Classifier: vendor/opensquilla (SquillaRouter assets bundled in-repo)
```

> Fully independent of 9router: classification, forwarding, and fallbacks are all handled by TokenSaver itself.

## 📡 API overview

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Chat (auto routing / explicit passthrough) |
| `GET /v1/models` | Model list |
| `GET /healthz` | Health check |
| `GET /admin` | Admin panel |
| `GET/POST /admin/api/pool` | Model pool CRUD |
| `POST /admin/api/pool/{id}/test` | Functional verification (text 1+1 / image) |
| `GET/POST /admin/api/providers` | Provider CRUD |
| `POST /admin/api/discover` | Model discovery (auto-saves provider) |
| `GET /admin/api/usage` | Live usage statistics |
| `GET /admin/api/selflearning/status` | Self-learning status |
| `POST /admin/api/selflearning/feedback` | Correction feedback |

More docs: [docs/config.md](docs/config.md) · [docs/design.md](docs/design.md) · [docs/faq.md](docs/faq.md) · [CHANGELOG.md](CHANGELOG.md)

## 🔒 Key safety

`models_pool.json` / `providers.json` / `*_api_key` contain real keys and channel details — all git-ignored, **never committed**. Keys shown in the admin panel are masked.

## 📄 License

- This project: MIT (see [LICENSE](LICENSE))
- Bundled classifier from [OpenSquilla](https://github.com/opensquilla/opensquilla) (Apache-2.0, model assets distributed with the repo)

## 🧑‍💻 Development

- [CONTRIBUTING.md](CONTRIBUTING.md): contribution guide
- [docs/design.md](docs/design.md): design notes
