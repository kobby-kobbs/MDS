# Private Catalog Integration Guide

> **Audience**: Developers integrating private model catalogs with Foundry Local.
> Works alongside the public catalog — no disruption to existing functionality.

---

## How It Works

Foundry Local ships with a **public catalog** of Microsoft-curated models (Phi, Qwen, etc.).
With the Private Catalog feature, you can add your own models alongside the public catalog.

```
┌───────────────────────────────────────────────────┐
│                  Your App                         │
│                                                   │
│   FoundryLocalManager.CreateAsync(config)         │
│         │                                         │
│         ▼                                         │
│   ┌─────────────────────────────────┐             │
│   │     Aggregate Catalog           │             │
│   │  ┌──────────┐  ┌─────────────┐ │             │
│   │  │  Public   │  │   Private   │ │             │
│   │  │  Catalog  │  │   Catalog   │ │             │
│   │  │ (default) │  │ (your MDS)  │ │             │
│   │  └──────────┘  └──────┬──────┘ │             │
│   └────────────────────────┼────────┘             │
│                            │                      │
│              Auth0 / Okta / Azure AD              │
│              (OAuth2 client credentials)          │
│                            │                      │
│                            ▼                      │
│                   MDS (your models)               │
└───────────────────────────────────────────────────┘
```

**Key behavior**:
- Public catalog loads instantly (bundled model list)
- Private catalog authenticates and fetches models during startup (~1-3 seconds)
- By the time `CreateAsync()` completes, **both catalogs are ready**
- `ListModelsAsync()` returns models from both catalogs by default

---

## Quick Start (5 Minutes)

### 1. Set Credentials

Choose **one** of these methods. Environment variables take priority over config file.

#### Option A — Environment Variables (recommended for cloud/containers)

```powershell
# PowerShell
$env:MDS_URI = "https://mds-model-distribution.azurewebsites.net"
$env:MDS_CLIENT_ID = "your-client-id"
$env:MDS_CLIENT_SECRET = "your-client-secret"
$env:MDS_TOKEN_ENDPOINT = "https://your-idp.com/oauth/token"
$env:MDS_AUDIENCE = "model-distribution-service"
```

```bash
# Bash / Linux / Docker
export MDS_URI="https://mds-model-distribution.azurewebsites.net"
export MDS_CLIENT_ID="your-client-id"
export MDS_CLIENT_SECRET="your-client-secret"
export MDS_TOKEN_ENDPOINT="https://your-idp.com/oauth/token"
export MDS_AUDIENCE="model-distribution-service"
```

#### Option B — Config File (recommended for desktop/on-prem)

Add to your `appsettings.json`:

```json
{
  "PrivateCatalogUri": "https://mds-model-distribution.azurewebsites.net",
  "PrivateCatalogClientId": "your-client-id",
  "PrivateCatalogClientSecret": "your-client-secret",
  "PrivateCatalogTokenEndpoint": "https://your-idp.com/oauth/token",
  "PrivateCatalogAudience": "model-distribution-service"
}
```

> **Security note**: For production, prefer environment variables injected from a
> secrets manager (Azure Key Vault, K8s Secrets, etc.) over plaintext config files.

### 2. Run Your App — No Code Changes Needed

```csharp
// This is standard Foundry Local code — nothing private-catalog-specific
var config = new Configuration { AppName = "my-app" };
await FoundryLocalManager.CreateAsync(config);
var mgr = FoundryLocalManager.Instance;

var catalog = await mgr.GetCatalogAsync();
var models = await catalog.ListModelsAsync();

// If credentials are set: returns public + private models
// If no credentials:      returns public models only
foreach (var model in models)
    Console.WriteLine($"  {model.Variants.First().Alias}");
```

That's it. **Same code** works whether private catalog is configured or not.

---

## Controlling What's Visible

By default, both catalogs are visible after auto-connect. Your app can filter at runtime:

### Show Only Private Models

```csharp
await catalog.SelectCatalogAsync("private");
var models = await catalog.ListModelsAsync();
// → only your private models
```

### Show Only Public Models

```csharp
await catalog.SelectCatalogAsync("public");
var models = await catalog.ListModelsAsync();
// → only Microsoft public models
```

### Show Both (default)

```csharp
await catalog.SelectCatalogAsync(null);
var models = await catalog.ListModelsAsync();
// → all models from all catalogs
```

### List Available Catalogs

```csharp
var names = await catalog.GetCatalogNamesAsync();
// → ["public", "private"]
// or just ["public"] if no credentials are configured
```

### Build a UI Toggle

Your app can let users switch views at runtime — no restart needed:

```csharp
// Example: dropdown handler
async Task OnCatalogFilterChanged(string? selection)
{
    // selection = "private", "public", or null (all)
    await catalog.SelectCatalogAsync(selection);
    var models = await catalog.ListModelsAsync();
    RefreshModelListUI(models);
}
```

---

## Advanced: Manual Catalog Management

If you need more control (multiple private catalogs, custom names, etc.),
disable auto-connect and manage catalogs yourself:

```csharp
var config = new Configuration
{
    AppName = "my-app",
    AutoConnectPrivateCatalog = false    // disable auto-connect
};

await FoundryLocalManager.CreateAsync(config);
var catalog = await mgr.GetCatalogAsync();

// Add catalogs manually with custom names
await catalog.AddCatalogAsync("dev-models",
    new Uri("https://dev.example.com/mds"),
    clientId: "dev-client-id",
    clientSecret: "dev-secret",
    tokenEndpoint: "https://auth.example.com/oauth/token",
    audience: "model-distribution-service");

await catalog.AddCatalogAsync("prod-models",
    new Uri("https://prod.example.com/mds"),
    clientId: "prod-client-id",
    clientSecret: "prod-secret",
    tokenEndpoint: "https://auth.example.com/oauth/token",
    audience: "model-distribution-service");

// Switch between them
await catalog.SelectCatalogAsync("prod-models");
var models = await catalog.ListModelsAsync();
```

---

## Credential Priority

When both environment variables and config file are present:

```
1. Environment variables   ← checked first (highest priority)
2. appsettings.json        ← fallback
3. (nothing)               ← public catalog only
```

If `MDS_URI` env var is set, config file values for private catalog are ignored.

---

## API Reference

| Method | Description |
|--------|-------------|
| `AddCatalogAsync(name, uri, clientId?, clientSecret?, bearerToken?, tokenEndpoint?, audience?)` | Add a private catalog with OAuth2 or bearer token auth |
| `SelectCatalogAsync(name)` | Filter model list to a specific catalog |
| `SelectCatalogAsync(null)` | Show models from all catalogs |
| `GetCatalogNamesAsync()` | List all registered catalog names |
| `ListModelsAsync()` | List models (respects current catalog filter) |

### Authentication Options

| Parameter | When to use |
|-----------|-------------|
| `clientId` + `clientSecret` | OAuth2 client credentials flow (recommended) |
| `bearerToken` | Pre-obtained token (e.g., from your own auth flow) |
| `tokenEndpoint` | Required for OAuth2 flow — your IdP's token URL |
| `audience` | OAuth2 audience claim (defaults to `"model-distribution-service"`) |

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `MDS_URI` | Yes | Your MDS instance URL |
| `MDS_CLIENT_ID` | Yes* | OAuth2 client ID |
| `MDS_CLIENT_SECRET` | Yes* | OAuth2 client secret |
| `MDS_TOKEN_ENDPOINT` | Yes* | Your IdP's token endpoint URL |
| `MDS_AUDIENCE` | No | OAuth2 audience (defaults to `"model-distribution-service"`) |

\* Required when using OAuth2 client credentials flow. If using a pre-obtained bearer token, set `MDS_BEARER_TOKEN` instead.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Only public models show up | Credentials not set or not visible to app process | Verify env vars with `echo $env:MDS_URI` (PowerShell) or `echo $MDS_URI` (bash) |
| `AddCatalogAsync` throws 401 | Invalid client credentials | Check client ID, secret, and token endpoint |
| `AddCatalogAsync` throws timeout | MDS or IdP unreachable | Verify network connectivity to MDS URI and token endpoint |
| Models appear in catalog but download fails | Entitlements don't cover this model | Check JWT `entitlements` claim includes the model |
| `GetCatalogNamesAsync` returns only `["public"]` | Auto-connect failed silently or credentials missing | Check logs for auth errors; verify all 4 required env vars are set |

---

## FAQ

**Q: Does this affect existing Foundry Local customers?**
A: No. Without credentials, behavior is identical to standard Foundry Local. The private catalog is entirely opt-in.

**Q: Can I use both public and private models in the same app?**
A: Yes. By default, both catalogs are visible. You can download and load models from either catalog in the same session.

**Q: Do I need to restart to switch between catalog views?**
A: No. `SelectCatalogAsync()` switches the view instantly at runtime.

**Q: What happens if the private catalog is unreachable at startup?**
A: `CreateAsync()` will throw an exception. Your app should catch this and decide whether to continue with public-only or show an error.

**Q: Can I add credentials after startup?**
A: Yes. Call `AddCatalogAsync()` at any time — it doesn't have to happen during startup.

**Q: What IdPs are supported?**
A: Any IdP that supports OAuth2 client credentials flow: Auth0, Okta, Azure AD / Entra ID, AWS Cognito, etc. You can also pass a pre-obtained bearer token.
