# MDS Product Roadmap

## Vision
Transform MDS from a demo into a **paid, production-ready service** that provides:
1. **Private Model Catalog** - Secure model distribution (current)
2. **Model Optimization Pipeline** - Quantization, platform-specific optimization
3. **Fine-tuning Pipeline** - Customer model customization
4. **Multi-platform Output** - One upload → multiple optimized outputs for different devices

---

## Immediate Milestone: Foundry Local Private Catalog
**Goal**: PhonePe (or any customer) can discover and download private models via Foundry Local.

---

## Checklist

### Phase 1: Production Infrastructure (Priority: NOW)

#### Completed
- [x] **JWKS Integration** — Fetch customer public keys from JWKS endpoints (1hr cache, stale fallback)
- [x] **POST /catalog** — Foundry Local SDK compatible catalog endpoint (indexEntitiesResponse format)
- [x] **GET /models/{name}** — Single model detail in Foundry Local format
- [x] **Catalog Schema Aligned** — Tags match public catalog (qwen2.5 reference): alias, task, inputModalities, outputModalities, promptTemplate, license, directoryPath, foundryLocal, maxOutputTokens, supportsToolCalling, tool* fields
- [x] **Test Harness** — Unit tests (auth, catalog, jwks, customers, API endpoints) + integration smoke tests
- [x] **Project Structure** — src/mds/ package, tests/unit + tests/integration, pyproject.toml, .gitignore

#### In Progress
- [ ] **Alternate Catalog URI (SDK Integration)**
  - MDS side: DONE — `POST /catalog` returns correct `indexEntitiesResponse` schema
  - Customer side: configure Foundry Local SDK with `AzureCatalogUri` pointing to MDS
  - JS example: `additionalSettings: { "AzureCatalogUri": "https://mds-model-distribution.azurewebsites.net" }`
  - Reference: [natke/foundry-local-js](https://github.com/natke/foundry-local-js)
  - See `docs/SDK_INTEGRATION.md` for full guide
  - **Note**: MDS is the catalog/distribution server. The customer's app initializes the
    Foundry Local SDK, points it at MDS, and handles local inference. MDS never runs models.

- [ ] **3P Identity Provider Simulation**
  - For testing: use `login.microsoft.com` (MSFT Test App) to simulate PhonePe's auth
  - Or: continue using demo RSA keypair (current approach)
  - Decision: delegate auth entirely to customer app — MDS only validates the signed token

- [ ] **Azure Subscription Setup**
  - Service Tree registration
  - AIRS setup
  - AFX Finance contact for staging + production subscriptions
  - Currently blocked on internal navigation

---

### Phase 2: SDK Integration & End-to-End Testing (Next)

#### Foundry Local SDK Integration
- [ ] **JS Sample App** — Clone [natke/foundry-local-js](https://github.com/natke/foundry-local-js), configure `AzureCatalogUri` to point at MDS, run full flow: list models → download → load → chat
  - Requires Foundry Local runtime installed on test machine
  - This is a *customer-side* test app, not part of MDS itself
- [ ] **Python SDK Sample** — Same flow using the Python Foundry Local SDK
- [ ] **C# SDK Sample** — Same flow using the .NET NuGet package
- [ ] **Auth Token Injection** — SDK must attach JWT `Authorization` header on every MDS call; may require SDK fork or middleware proxy
  - Alternative: run a thin auth proxy in front of MDS for testing
- [ ] **Download Flow Validation** — Confirm SDK receives SAS URL from MDS and downloads model binary directly from Blob Storage

#### Metadata Automation (Future Enhancement)
- [ ] **Tag Inheritance from Public Catalog** — If model is derived from a public catalog model, inherit base tags automatically

#### Customer App Integration
- [ ] Document the API between customer app and Foundry Local SDK
- [ ] Provide sample integration code (JS, Python, C#)
- [ ] Error handling and retry logic

---

### Phase 3: Value-Added Services (Future Revenue)

#### Model Optimization Pipeline
- [ ] **Quantization Service**
  - Customer uploads FP32 model
  - MDS outputs INT8/FP16 optimized versions
  - Automatic based on target platform

- [ ] **Platform-Specific Optimization**
  - One model → multiple outputs:
    - Android (low-end): Heavily quantized
    - Android (high-end): Less quantization, more features
    - iOS: CoreML optimized
    - Edge devices: TensorRT/ONNX Runtime optimized
  - Customer specifies target platforms, we spit out optimal versions

- [ ] **Auto-Optimization Pipeline**
  ```
  Upload Model → Analyze → Generate N optimized variants → Store all versions
  ```

#### Fine-Tuning Pipeline (Longer Term)
- [ ] Customer provides base model + training data
- [ ] MDS runs fine-tuning job
- [ ] Output: Fine-tuned model in their catalog
- [ ] Potential: Integration with Azure ML compute

---

### Phase 4: Billing & Monetization

#### Billing Dimensions to Consider
| Metric | Description |
|--------|-------------|
| Storage | GB-months of model storage |
| Downloads | Number of model downloads |
| Bandwidth | Data transferred |
| Optimization | Per-model optimization jobs |
| Fine-tuning | Compute hours for training |

#### Implementation
- [ ] Integrate Azure Cost Management
- [ ] Usage tracking per customer
- [ ] Billing dashboard/reports
- [ ] Define pricing tiers

---

## Technical Debt to Address

### Current Issues
1. **Double Storage**: Models stored in blob AND ML registry storage
   - Fix: Use ML registry for metadata only
   
2. **Hardcoded Keys**: RSA keys in `customers.py`
   - Fix: ✅ JWKS endpoint fetching (implemented)
   
3. **No Rate Limiting**: API can be abused
   - Fix: Add per-customer rate limits

4. **No Audit Trail**: Can't track who downloaded what
   - Fix: Add logging to Azure Table Storage or App Insights

5. **Single Region**: Only deployed to one region
   - Fix: Multi-region for latency/reliability

---

## Architecture Evolution

### Current (Demo)
```
Customer → MDS API → Azure ML Registry + Blob Storage
                     (shared resources)
```

### Target (Production)
```
┌─────────────────────────────────────────────────────────────────────────┐
│                         MDS Control Plane                                │
│  - Customer management                                                   │
│  - Billing & usage tracking                                             │
│  - Optimization job orchestration                                        │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────────┐   ┌───────────────────┐   ┌───────────────────┐
│  PhonePe Tenant   │   │  Customer B       │   │  Customer C       │
│                   │   │                   │   │                   │
│  - Their Registry │   │  - Their Registry │   │  - Their Registry │
│  - Their Storage  │   │  - Their Storage  │   │  - Their Storage  │
│  - Their Keys     │   │  - Their Keys     │   │  - Their Keys     │
└───────────────────┘   └───────────────────┘   └───────────────────┘
```

### Multi-Platform Output Flow (Future)
```
Customer uploads: fraud-model-v1.onnx (FP32, 50MB)
                          │
                          ▼
              ┌───────────────────────┐
              │  Optimization Pipeline │
              │                       │
              │  - Analyze model      │
              │  - Detect layers      │
              │  - Run quantization   │
              │  - Platform optimize  │
              └───────────────────────┘
                          │
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Android Low  │  │ Android High │  │   iOS        │
│ INT8, 12MB   │  │ FP16, 25MB   │  │ CoreML, 20MB │
└──────────────┘  └──────────────┘  └──────────────┘
```

---

## Immediate Next Steps (This Week)

### Emmanuel
1. [ ] Create customer onboarding Bicep/Terraform script
2. [ ] Fix double-storage issue (ML registry metadata only)
3. [ ] Document JWT token fields with industry comparison

### Manager
1. [ ] Get production subscriptions from X Carell
2. [ ] Schedule PhonePe token integration call via Natalie
3. [ ] Define initial billing model

---

## Questions to Resolve

1. **Billing Model**: Per-download? Storage-based? Subscription tiers?
2. **Multi-tenancy**: Shared MDS instance or per-customer deployment?
3. **PhonePe Token**: What IdP do they use? Can they issue JWTs to our spec?
4. **Optimization Targets**: Which platforms are priority? (Android first?)
5. **Fine-tuning**: Will this use customer's Azure ML compute or ours?

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Customer onboarding time | < 1 hour (scripted) |
| Download latency | < 2 seconds to get SAS URL |
| Optimization time | < 10 minutes per variant |
| Uptime | 99.9% |

---

*Last Updated: February 5, 2026*
