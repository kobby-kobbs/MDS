<#
.SYNOPSIS
  Onboard a new MDS customer. Creates dedicated storage + ML registry.
.DESCRIPTION
  Creates an Azure resource group, deploys a Bicep template that provisions
  a storage account and ML registry, then outputs the env vars to add to
  the MDS App Service (or .env file) so the new customer is routed to
  their own registry and storage.
.EXAMPLE
  .\onboard.ps1 phonepe
  .\onboard.ps1 acme -Location eastus
#>
param(
    [Parameter(Mandatory)][string]$Customer,
    [string]$Location = "centralus"
)

$ErrorActionPreference = "Stop"
$rg = "mds-$Customer-rg"
$template = Join-Path $PSScriptRoot "onboard.bicep"
$customersDir = Join-Path $PSScriptRoot ".." "customers"

Write-Host "`n=== MDS Customer Onboarding: $Customer ===" -ForegroundColor Cyan

# 1. Create resource group
Write-Host "`n1. Creating resource group $rg..." -ForegroundColor Yellow
az group create -n $rg -l $Location -o none

# 2. Deploy Bicep template (storage + registry)
Write-Host "2. Deploying storage + registry..." -ForegroundColor Yellow
$result = az deployment group create `
    -g $rg `
    --template-file $template `
    --parameters customer=$Customer `
    -o json | ConvertFrom-Json

$cfg = $result.properties.outputs.config.value

# 3. Save customer config JSON
if (-not (Test-Path $customersDir)) { New-Item -ItemType Directory -Path $customersDir | Out-Null }
$cfg | ConvertTo-Json | Out-File (Join-Path $customersDir "$Customer.json")

# 4. Print results
$upperCustomer = $Customer.ToUpper()
Write-Host "`nResources created!" -ForegroundColor Green
Write-Host "  Storage account : $($cfg.storage)"
Write-Host "  Blob container  : $($cfg.container)"
Write-Host "  ML Registry     : $($cfg.registry)"
Write-Host "  Config saved to : customers\$Customer.json"

Write-Host "`n=== Add these env vars to MDS App Service ===" -ForegroundColor Cyan
Write-Host "  CUSTOMER_${upperCustomer}_REGISTRY=$($cfg.registry)"
Write-Host "  CUSTOMER_${upperCustomer}_STORAGE=$($cfg.storage)"
Write-Host "  CUSTOMER_${upperCustomer}_ISSUER=https://auth.${Customer}.com"
Write-Host "  CUSTOMER_${upperCustomer}_JWKS_URL=https://auth.${Customer}.com/.well-known/jwks.json"

Write-Host "`nTo apply via Azure CLI:" -ForegroundColor Yellow
Write-Host @"
az webapp config appsettings set \
  --name mds-model-distribution \
  --resource-group customer-auth-service-rg \
  --settings \
    CUSTOMER_${upperCustomer}_REGISTRY=$($cfg.registry) \
    CUSTOMER_${upperCustomer}_STORAGE=$($cfg.storage) \
    CUSTOMER_${upperCustomer}_ISSUER=https://auth.${Customer}.com \
    CUSTOMER_${upperCustomer}_JWKS_URL=https://auth.${Customer}.com/.well-known/jwks.json
"@

Write-Host "`nThen add the customer entry to src/mds/customers.py CUSTOMERS dict." -ForegroundColor Yellow
Write-Host "Done!`n" -ForegroundColor Green
