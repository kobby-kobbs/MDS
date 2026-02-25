/**
 * MDS — Foundry Local JS Sample
 *
 * Uses the Foundry Local JS SDK with AzureCatalogUri to browse the
 * Azure AI catalog, download a model, load it, and run a chat completion.
 *
 * Usage:
 *   npm install
 *   node index.js
 */

import { FoundryLocalManager } from "@prathikrao/foundry-local-sdk";

// ── Configuration ───────────────────────────────────────────────────

const MODEL_ALIAS = "qwen2.5-7b-instruct-generic-cpu:4";

// ── Initialize the Foundry Local manager with Azure catalog ─────────

const manager = FoundryLocalManager.create({
  appName: "mds-foundry-local-js",
  serviceEndpoint: "http://localhost:5000",
  logLevel: "info",
  additionalSettings: {
    AzureCatalogUri: "https://ai.azure.com/api/eastus/ux/v1.0",
  },
});

async function main() {
  console.log("=== Foundry Local JS — MDS Sample ===\n");

  // 1. List catalog models (from Azure catalog)
  console.log("📦 Fetching catalog models...");
  const catalog = manager.catalog;
  const catalogModels = await catalog.getModels();

  if (catalogModels.length === 0) {
    console.log("  No models found in catalog.");
  } else {
    console.log(`  ${catalogModels.length} model(s) available:\n`);
    for (const model of catalogModels) {
      const size = model.fileSizeMB ? `${model.fileSizeMB} MB` : "?";
      console.log(`  - ${model.alias.padEnd(45)} ${size.padStart(10)}`);
    }
  }

  // 2. List cached (already downloaded) models
  console.log("\n💾 Cached models:");
  const cachedModels = await catalog.getCachedModels();

  if (cachedModels.length === 0) {
    console.log("  No models cached locally.");
  } else {
    for (const model of cachedModels) {
      console.log(`  ✓ ${model.alias}`);
    }
  }

  // 3. Download model if not cached
  const isAlreadyCached = cachedModels.some((m) => m.alias === MODEL_ALIAS);

  if (!isAlreadyCached) {
    console.log(`\n⬇️  Downloading ${MODEL_ALIAS}...`);
    await catalog.downloadModel(MODEL_ALIAS);
    console.log("  Download complete.");
  } else {
    console.log(`\n  ${MODEL_ALIAS} is already cached.`);
  }

  // 4. Load the model
  console.log(`\n🔄 Loading ${MODEL_ALIAS}...`);
  await manager.loadModel(MODEL_ALIAS);
  console.log("  Model loaded.");

  // 5. Run a chat completion
  console.log("\n💬 Running chat completion...\n");

  const chatClient = manager.getChatClient();
  const response = await chatClient.chat.completions.create({
    model: MODEL_ALIAS,
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "What is Foundry Local?" },
    ],
    max_tokens: 256,
  });

  console.log("  Assistant:", response.choices[0].message.content);

  // 6. Unload
  console.log(`\n🛑 Unloading ${MODEL_ALIAS}...`);
  await manager.unloadModel(MODEL_ALIAS);
  console.log("  Done.\n");
}

main().catch((err) => {
  console.error("Error:", err.message || err);
  process.exit(1);
});
