<#
.SYNOPSIS
  Onboard a new MDS customer. Fully automated — no manual code edits needed.
.DESCRIPTION
  Self-service JWT mode (-SelfServiceJwt):
    1. Creates Azure resource group + storage + ML registry (Bicep)
    2. Grants MDS Managed Identity access to the new resources
    3. Outputs FL Core appsettings.json snippet and IdP setup instructions
    4. NO MDS registration needed — JWT token carries all claims

  API registration mode (-MdsUrl + -AdminKey):
    1. Creates Azure resource group + storage + ML registry (Bicep)
    2. Calls POST /admin/register on MDS to register the customer
    3. MDS generates API key, stores config, and returns the key
    4. Grants MDS Managed Identity access to the new resources

  Legacy mode (no -MdsUrl, no -SelfServiceJwt):
    1-5 as above but writes to customers.json and sets App Service env vars directly
.EXAMPLE
  # Self-service JWT (recommended — works with any IdP):
  .\onboard.ps1 -Customer acme -Issuer "https://acme.us.auth0.com/" -SelfServiceJwt `
    -TokenEndpoint "https://acme.us.auth0.com/oauth/token" -ClientId "abc123"

  # API registration (customer runs this):
  .\onboard.ps1 -Customer acme -Issuer "https://auth.acme.com" -MdsUrl "https://mds.azurewebsites.net" -AdminKey "admin-key"

  # Legacy (MDS team runs this):
  .\onboard.ps1 -Customer acme -Issuer "https://auth.acme.com"
#>
param(
    [Parameter(Mandatory)][string]$Customer,
    [Parameter(Mandatory)][string]$Issuer,
    [string]$JwksUrl = "",
    [string]$Location = "centralus",
    [string]$Models = "*",
    [string]$MdsAppName = "mds-model-distribution",
    [string]$MdsResourceGroup = "customer-ml-registry-rg",
    # Self-service JWT mode (recommended for new customers)
    [switch]$SelfServiceJwt,
    [string]$TokenEndpoint = "",
    [string]$ClientId = "",
    [string]$Audience = "model-distribution-service",
    # API registration mode: call MDS API instead of writing config directly
    [string]$MdsUrl = "",
    [string]$AdminKey = ""
)

$ErrorActionPreference = "Stop"
$name = $Customer.ToLower()
$upper = $Customer.ToUpper()
$rg = "mds-$name-rg"
$template = Join-Path $PSScriptRoot "onboard.bicep"

# Auto-derive JWKS URL if not provided (used in legacy mode only)
if (-not $JwksUrl) {
    $JwksUrl = "$($Issuer.TrimEnd('/'))/.well-known/jwks.json"
}

Write-Host "`n════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  MDS Customer Onboarding: $Customer"          -ForegroundColor Cyan
if ($SelfServiceJwt) {
    Write-Host "  Mode: Self-service JWT (no MDS registration)" -ForegroundColor Cyan
} elseif ($MdsUrl) {
    Write-Host "  Mode: API registration"                       -ForegroundColor Cyan
} else {
    Write-Host "  Mode: Legacy (direct config)"                 -ForegroundColor Cyan
}
Write-Host "════════════════════════════════════════════`n" -ForegroundColor Cyan

# ── 1. Create Azure resources (idempotent) ───────────────────
$rgExists = az group exists -n $rg 2>$null
if ($rgExists -eq 'true') {
    Write-Host "1. Resource group $rg already exists, skipping creation" -ForegroundColor DarkGray
} else {
    Write-Host "1. Creating resource group $rg..." -ForegroundColor Yellow
    az group create -n $rg -l $Location -o none
}

# Check if registry already exists
$existingReg = az ml registry show --name "mds-$name-registry" --resource-group $rg --query name -o tsv 2>$null
if ($existingReg) {
    Write-Host "2. Registry already exists, skipping deployment" -ForegroundColor DarkGray
    $registryName = $existingReg
    $storageName = (az storage account list -g $rg --query "[0].name" -o tsv)
} else {
    Write-Host "2. Deploying storage + ML registry..." -ForegroundColor Yellow
    $result = az deployment group create `
        -g $rg `
        --template-file $template `
        --parameters customer=$Customer `
        -o json | ConvertFrom-Json
    $cfg = $result.properties.outputs.config.value
    $registryName = $cfg.registry
    $storageName = $cfg.storage
}

Write-Host "   Storage:  $storageName" -ForegroundColor Green
Write-Host "   Registry: $registryName" -ForegroundColor Green

# ── 2. Register with MDS ─────────────────────────────────────

if ($SelfServiceJwt) {
    # ── Self-service JWT mode: NO MDS registration needed ─────
    Write-Host "`n3. Self-service JWT mode — no MDS registration needed." -ForegroundColor Green
    Write-Host "   MDS validates tokens via JWKS derived from the iss claim." -ForegroundColor Green
    Write-Host "   Token carries registry_name, storage_account, and entitlements." -ForegroundColor Green
    $catalogUrl = "https://$MdsAppName.azurewebsites.net"
} elseif ($MdsUrl -and $AdminKey) {
    # ── Self-service mode: call MDS register API ─────────────
    Write-Host "`n3. Registering with MDS API..." -ForegroundColor Yellow

    $modelsList = if ($Models -eq "*") { @("*") } else { ($Models -split "," | ForEach-Object { $_.Trim() }) }

    $body = @{
        customer_name   = $Customer
        issuer          = $Issuer
        models          = $modelsList
        registry_name   = $registryName
        storage_account = $storageName
    } | ConvertTo-Json -Depth 3

    $headers = @{
        "Content-Type" = "application/json"
        "X-Admin-Key"  = $AdminKey
    }

    try {
        $response = Invoke-RestMethod -Uri "$MdsUrl/admin/register" -Method POST -Body $body -Headers $headers
        $apiKey = $response.api_key
        $catalogUrl = $response.catalog_url
        Write-Host "   Registered successfully!" -ForegroundColor Green
        Write-Host "   Customer ID: $($response.customer_id)" -ForegroundColor Green
        Write-Host "   JWKS URL:    $($response.jwks_url)" -ForegroundColor Green
    } catch {
        Write-Host "   Registration failed: $_" -ForegroundColor Red
        exit 1
    }
} else {
    # ── Legacy mode: direct config + env var ──────────────────
    Write-Host "`n3. Generating API key..." -ForegroundColor Yellow
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $apiKey = [Convert]::ToBase64String($bytes)
    Write-Host "   API key: $($apiKey.Substring(0,8))..." -ForegroundColor Green

    Write-Host "`n4. Setting App Service secrets..." -ForegroundColor Yellow
    az webapp config appsettings set `
        --name $MdsAppName `
        --resource-group $MdsResourceGroup `
        --settings "CUSTOMER_${upper}_API_KEY=$apiKey" `
        -o none
    Write-Host "   App settings updated (API key stored as env var only)" -ForegroundColor Green

    Write-Host "`n5. Writing to customers.json..." -ForegroundColor Yellow
    $customersDir = Join-Path $PSScriptRoot ".." "customers"
    $customersFile = Join-Path $customersDir "customers.json"
    if (-not (Test-Path $customersDir)) { New-Item -ItemType Directory -Path $customersDir | Out-Null }

    $modelsList = if ($Models -eq "*") { @("*") } else { $Models -split "," | ForEach-Object { $_.Trim() } }

    $newCustomer = @{
        name            = $Customer
        sub             = "$name-client"
        issuer          = $Issuer
        jwks_url        = $JwksUrl
        models          = $modelsList
        registry_name   = $registryName
        storage_account = $storageName
    }

    if (Test-Path $customersFile) {
        $allCustomers = Get-Content $customersFile -Raw | ConvertFrom-Json -AsHashtable
    } else {
        $allCustomers = @{}
    }
    $allCustomers[$name] = $newCustomer
    $allCustomers | ConvertTo-Json -Depth 5 | Set-Content $customersFile -Encoding UTF8
    Write-Host "   Saved to: customers/customers.json" -ForegroundColor Green

    $catalogUrl = "https://$MdsAppName.azurewebsites.net/catalog/foundrylocal"
}

# ── 3. Grant MDS access to new resources ─────────────────────
Write-Host "`n6. Granting MDS Managed Identity access..." -ForegroundColor Yellow
$principalId = (az webapp identity show --name $MdsAppName --resource-group $MdsResourceGroup --query principalId -o tsv 2>$null)
if ($principalId) {
    $registryId = (az ml registry show --name $registryName --resource-group $rg --query id -o tsv 2>$null)
    if ($registryId) {
        az role assignment create --assignee $principalId --role "Contributor" --scope $registryId -o none 2>$null
        Write-Host "   Contributor on ML Registry: OK" -ForegroundColor Green
    }
    $storageId = (az storage account show --name $storageName --resource-group $rg --query id -o tsv 2>$null)
    if ($storageId) {
        az role assignment create --assignee $principalId --role "Storage Blob Data Contributor" --scope $storageId -o none 2>$null
        Write-Host "   Storage Blob Data Contributor: OK" -ForegroundColor Green
    }
} else {
    Write-Host "   [WARN] Could not get MDS Managed Identity. Set RBAC manually." -ForegroundColor Yellow
}

# ── Summary ──────────────────────────────────────────────────
Write-Host "`n════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Onboarding Complete: $Customer"               -ForegroundColor Green
Write-Host "════════════════════════════════════════════"   -ForegroundColor Green
Write-Host ""
Write-Host "  Resources:"
Write-Host "    Resource group:  $rg"
Write-Host "    Storage:         $storageName"
Write-Host "    ML Registry:     $registryName"
Write-Host ""

if ($SelfServiceJwt) {
    # ── Self-service JWT summary ──
    Write-Host "  Customer's appsettings.json for Foundry Local:" -ForegroundColor Cyan
    if ($TokenEndpoint) {
        Write-Host @"
  {
    "PrivateCatalogUri": "$catalogUrl",
    "PrivateCatalogTokenEndpoint": "$TokenEndpoint",
    "PrivateCatalogClientId": "$ClientId",
    "PrivateCatalogClientSecret": "<your-client-secret>",
    "PrivateCatalogAudience": "$Audience"
  }
"@ -ForegroundColor Cyan
    } else {
        Write-Host @"
  {
    "PrivateCatalogUri": "$catalogUrl",
    "PrivateCatalogBearerToken": "<your-jwt-token>"
  }
"@ -ForegroundColor Cyan
    }
    Write-Host ""
    Write-Host "  Required JWT claims (configure in your IdP):" -ForegroundColor Yellow
    Write-Host "    iss:              $Issuer"
    Write-Host "    aud:              $Audience"
    Write-Host "    registry_name:    $registryName"
    Write-Host "    storage_account:  $storageName"
    Write-Host "    entitlements:     {`"models`":[`"$Models`"],`"versions`":[`"*`"]}"
    Write-Host ""
    Write-Host "  For Auth0:  use setCustomClaim() in an M2M Action (onExecuteCredentialsExchange)" -ForegroundColor DarkGray
    Write-Host "  For Okta:   add custom claims to your authorization server" -ForegroundColor DarkGray
    Write-Host "  For Entra:  use claims-mapping policy" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Customer is live immediately. No MDS restart needed." -ForegroundColor Green
} else {
    Write-Host "  Auth (customer provides):"
    Write-Host "    Issuer:   $Issuer"
    Write-Host ""
    Write-Host "  Customer's appsettings.json for Foundry Local:"
    Write-Host @"
  {
    "AppName": "$name-app",
    "PrivateCatalogUri": "$catalogUrl"
  }
"@ -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  API Key (store securely, shown only once):"
    Write-Host "    $apiKey" -ForegroundColor Yellow
    Write-Host ""
    if ($MdsUrl) {
        Write-Host "  Customer is live immediately. No restart needed." -ForegroundColor Green
    } else {
        Write-Host "  Restart MDS to pick up the new customer." -ForegroundColor Green
    }
}
Write-Host ""
