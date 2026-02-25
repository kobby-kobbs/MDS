# Foundry Local JS Sample

JavaScript sample that connects to Foundry Local using the JS SDK with **Azure catalog** (`AzureCatalogUri`) support.

## Why JavaScript?

The Python SDK v2 (which supports `AzureCatalogUri`) is not published yet. The JavaScript SDK already supports it via `additionalSettings` in `FoundryLocalManager.create()`.

## Prerequisites

- [Node.js](https://nodejs.org/) v18+
- [Foundry Local](https://learn.microsoft.com/en-us/windows/ai/foundry-local/) service running on `http://localhost:5000`

## Setup

```bash
cd samples/foundry-local-js
npm install
```

## Run

```bash
node index.js
```

This will:
1. Connect to the Azure AI catalog via `AzureCatalogUri`
2. List available models
3. List cached (downloaded) models
4. Download a model if not cached
5. Load the model into Foundry Local
6. Run a chat completion
7. Unload the model

## Configuration

Edit `index.js` to change:

- **`MODEL_ALIAS`** — the model to download/load (default: `qwen2.5-7b-instruct-generic-cpu:4`)
- **`AzureCatalogUri`** — the Azure catalog endpoint (default: `https://ai.azure.com/api/eastus/ux/v1.0`)
- **`serviceEndpoint`** — Foundry Local service URL (default: `http://localhost:5000`)
