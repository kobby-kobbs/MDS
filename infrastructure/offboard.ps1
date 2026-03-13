<#
.SYNOPSIS
  Offboard an MDS customer. Removes config, secrets, and optionally Azure resources.
.DESCRIPTION
  Self-service mode (-MdsUrl + -AdminKey):
    1. Calls POST /admin/offboard on MDS to deregister the customer
    2. API key is revoked immediately — no restart needed
    3. Optionally deletes the customer's Azure resource group

  Legacy mode (no -MdsUrl):
    1. Removes customer from customers/customers.json
    2. Removes App Service env vars
    3. Optionally deletes Azure resources
.EXAMPLE
  # Self-service:
  .\offboard.ps1 -Customer acme -MdsUrl "https://mds.azurewebsites.net" -AdminKey "admin-key"

  # Legacy:
  .\offboard.ps1 -Customer acme
  .\offboard.ps1 -Customer acme -DeleteResources
#>
param(
    [Parameter(Mandatory)][string]$Customer,
    [switch]$DeleteResources,
    [string]$MdsAppName = "mds-model-distribution",
    [string]$MdsResourceGroup = "customer-ml-registry-rg",
    # Self-service mode
    [string]$MdsUrl = "",
    [string]$AdminKey = ""
)

$ErrorActionPreference = "Stop"
$name = $Customer.ToLower()
$upper = $Customer.ToUpper()
$rg = "mds-$name-rg"

Write-Host "`n════════════════════════════════════════════" -ForegroundColor Red
Write-Host "  MDS Customer Offboarding: $Customer"          -ForegroundColor Red
if ($MdsUrl) {
    Write-Host "  Mode: Self-service (API deregistration)"  -ForegroundColor Red
} else {
    Write-Host "  Mode: Legacy (direct config)"             -ForegroundColor Red
}
Write-Host "════════════════════════════════════════════`n" -ForegroundColor Red

if ($MdsUrl -and $AdminKey) {
    # ── Self-service mode: call MDS offboard API ─────────────
    Write-Host "1. Offboarding via MDS API..." -ForegroundColor Yellow
    $body = @{ customer_id = $name } | ConvertTo-Json
    $headers = @{
        "Content-Type" = "application/json"
        "X-Admin-Key"  = $AdminKey
    }
    try {
        Invoke-RestMethod -Uri "$MdsUrl/admin/offboard" -Method POST -Body $body -Headers $headers | Out-Null
        Write-Host "   Offboarded successfully. API key revoked immediately." -ForegroundColor Green
    } catch {
        Write-Host "   Offboarding failed: $_" -ForegroundColor Red
        exit 1
    }
    Write-Host "2. App Service secrets handled by API" -ForegroundColor DarkGray
    Write-Host "3. No legacy config to clean up" -ForegroundColor DarkGray
} else {
    # ── Legacy mode ──────────────────────────────────────────
    $customersDir = Join-Path $PSScriptRoot ".." "customers"
    $customersFile = Join-Path $customersDir "customers.json"

    # 1. Remove from customers.json
    if (Test-Path $customersFile) {
        $allCustomers = Get-Content $customersFile -Raw | ConvertFrom-Json -AsHashtable
        if ($allCustomers.ContainsKey($name)) {
            $allCustomers.Remove($name)
            $allCustomers | ConvertTo-Json -Depth 5 | Set-Content $customersFile -Encoding UTF8
            Write-Host "1. Removed '$name' from customers.json" -ForegroundColor Green
        } else {
            Write-Host "1. Customer '$name' not found in customers.json" -ForegroundColor Yellow
        }
    } else {
        Write-Host "1. customers.json not found" -ForegroundColor Yellow
    }

    # 2. Remove App Service secrets
    Write-Host "2. Removing App Service secrets..." -ForegroundColor Yellow
    $settings = @("CUSTOMER_${upper}_API_KEY")
    foreach ($s in $settings) {
        az webapp config appsettings delete `
            --name $MdsAppName `
            --resource-group $MdsResourceGroup `
            --setting-names $s `
            -o none 2>$null
    }
    Write-Host "   Removed: $($settings -join ', ')" -ForegroundColor Green

    # 3. Remove per-customer config file (legacy)
    $legacyFile = Join-Path $customersDir "$name.json"
    if (Test-Path $legacyFile) {
        Remove-Item $legacyFile -Force
        Write-Host "3. Removed legacy config: customers/$name.json" -ForegroundColor Green
    } else {
        Write-Host "3. No legacy config file to remove" -ForegroundColor DarkGray
    }
}

# ── 4. Optionally delete Azure resources ─────────────────────
if ($DeleteResources) {
    Write-Host "`n4. Deleting resource group $rg..." -ForegroundColor Red
    $confirm = Read-Host "   This will delete ALL resources (storage + registry + models). Continue? [y/N]"
    if ($confirm -eq 'y') {
        az group delete -n $rg --yes --no-wait
        Write-Host "   Resource group deletion initiated (async)" -ForegroundColor Green
    } else {
        Write-Host "   Skipped resource deletion" -ForegroundColor Yellow
    }
} else {
    Write-Host "`n4. Azure resources preserved (use -DeleteResources to remove)" -ForegroundColor DarkGray
}

# ── Summary ──────────────────────────────────────────────────
Write-Host "`n════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Offboarding Complete: $Customer"               -ForegroundColor Green
Write-Host "════════════════════════════════════════════"   -ForegroundColor Green
Write-Host ""
if ($MdsUrl) {
    Write-Host "  Customer access revoked immediately. No restart needed." -ForegroundColor Green
} else {
    Write-Host "  Restart MDS to apply changes." -ForegroundColor Yellow
}
Write-Host ""
