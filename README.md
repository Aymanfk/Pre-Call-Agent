# Pre-Call Agent

AI-powered pre-call intelligence briefs for Nutanix sales and SE teams. Upload an RVTools export or Nutanix Collector CSV and get a structured, environment-aware brief in under 30 seconds.

![Pipeline](https://img.shields.io/badge/pipeline-Perplexity%20→%20Claude-green)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Flask](https://img.shields.io/badge/flask-3.x-lightgrey)

## What it does

1. Fill in prospect details (company, contact, what you're selling)
2. Optionally upload an **RVTools `.xlsx`** or **Nutanix Collector `.csv`** export
3. **Stage 1 — Perplexity** runs live web research on the company and contact
4. **Stage 2 — Claude** synthesizes everything into a structured brief

The output includes:
- Company snapshot & recent news
- Contact intel
- Pain points tailored to their environment
- Talking points referencing actual VM/host/storage numbers
- Discovery questions
- Risk flags
- Recommended opening angle

All uploaded file data is processed in memory and never written to disk.

---

## Setup

### Prerequisites

- Python 3.9+
- An [Anthropic API key](https://console.anthropic.com/)
- A [Perplexity API key](https://www.perplexity.ai/settings/api)

### Install

```bash
git clone https://github.com/Aymanfk/Pre-Call-Agent.git
cd Pre-Call-Agent
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
PERPLEXITY_API_KEY=pplx-...
```

### Run locally

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000)

---

## Supported file formats

| Format | Source | Notes |
|--------|--------|-------|
| `.xlsx` | RVTools export | Parses vInfo, vHost, vCluster, vDatastore, vSource, vSnapshot, vHealth |
| `.csv` | Nutanix Collector | Handles BOM, NUL bytes, varied column naming across Collector versions |

RVTools data extracted:
- Physical host count, CPU model, core count
- VM count (total + powered on), vCPU allocation + overcommit ratio
- Physical RAM + vRAM allocation
- Datastore capacity (total across all VMFS/NFS stores)
- vCenter version
- Guest OS distribution
- Snapshot count
- Health warning count

---

## Deploying to Railway

Railway is the fastest way to get this running on a public URL.

**1. Push your code to GitHub** (already done)

**2. Create a Railway project**

Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → select `Pre-Call-Agent`

**3. Set environment variables**

In Railway: **Variables** tab → add:

```
ANTHROPIC_API_KEY=sk-ant-...
PERPLEXITY_API_KEY=pplx-...
```

**4. Deploy**

Railway auto-detects the `Procfile` and deploys. Your app will be live at a `*.up.railway.app` URL within ~2 minutes.

---

## Project structure

```
├── app.py              # Flask routes
├── agent.py            # Pipeline: Perplexity researcher + Claude synthesizer
│   ├── parse_rvtools_xlsx()     # RVTools Excel parser (in-memory)
│   ├── parse_nutanix_csv()      # Nutanix Collector CSV parser (in-memory)
│   ├── PerplexityResearcher     # Stage 1: live web research
│   └── ClaudeSynthesizer        # Stage 2: brief synthesis
├── templates/
│   └── index.html      # Single-page UI
├── requirements.txt
├── Procfile            # For Railway/Heroku deployment
└── .env                # API keys (never committed)
```

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key — [console.anthropic.com](https://console.anthropic.com/) |
| `PERPLEXITY_API_KEY` | Yes | Perplexity API key — [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api) |

---

## Security notes

- Uploaded files are read into memory and immediately discarded — nothing touches disk
- `.env` is in `.gitignore` and never committed
- No database, no user accounts, no persistent storage
