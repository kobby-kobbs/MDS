# ============================================================
# Deploy MDS FastAPI App to Azure App Service
# ============================================================
# Deploys Model Distribution Service to Azure.
# Prerequisite: az login
# ============================================================

# CONFIGURATION
$RESOURCE_GROUP       = "customer-ml-registry-rg"
$LOCATION             = "centralus"
$APP_SERVICE_PLAN     = "mds-app-plan"
$WEB_APP_NAME         = "mds-model-distribution"
$REGISTRY_NAME        = "customer-phone"
$STORAGE_ACCOUNT      = "customermodelstorage"
$PHONEPE_API_KEY      = $env:PHONEPE_API_KEY
if (-not $PHONEPE_API_KEY) {
    $PHONEPE_API_KEY = Read-Host "Enter PhonePe API key (or set PHONEPE_API_KEY env var)"
}

# ============================================================
# STEP 1: Check Azure login
# ============================================================
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "  STEP 1: Checking Azure Login"              -ForegroundColor Cyan
Write-Host "========================================`n"  -ForegroundColor Cyan

$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "Not logged in. Running 'az login'..." -ForegroundColor Yellow
    az login
} else {
    Write-Host "Logged in as: $($account.user.name)"  -ForegroundColor Green
    Write-Host "Subscription: $($account.name)"       -ForegroundColor Green
}

# ============================================================
# STEP 2: Create App Service Plan (Linux)
# ============================================================
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "  STEP 2: App Service Plan"                  -ForegroundColor Cyan
Write-Host "========================================`n"  -ForegroundColor Cyan

az appservice plan create `
    --name $APP_SERVICE_PLAN `
    --resource-group $RESOURCE_GROUP `
    --sku B2 `
    --is-linux

Write-Host "App Service Plan ready: $APP_SERVICE_PLAN" -ForegroundColor Green

# ============================================================
# STEP 3: Create Web App
# ============================================================
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "  STEP 3: Web App"                           -ForegroundColor Cyan
Write-Host "========================================`n"  -ForegroundColor Cyan

az webapp create `
    --name $WEB_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --plan $APP_SERVICE_PLAN `
    --runtime "PYTHON:3.11"

Write-Host "Web App ready: $WEB_APP_NAME" -ForegroundColor Green

# ============================================================
# STEP 4: Configure environment variables
# ============================================================
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "  STEP 4: App Settings"                      -ForegroundColor Cyan
Write-Host "========================================`n"  -ForegroundColor Cyan

az webapp config appsettings set `
    --name $WEB_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --settings `
        REGISTRY_NAME=$REGISTRY_NAME `
        STORAGE_ACCOUNT=$STORAGE_ACCOUNT `
        STORAGE_CONTAINER=models `
        SCM_DO_BUILD_DURING_DEPLOYMENT=true `
        WEBSITES_PORT=8000 `
        WEBSITES_CONTAINER_START_TIME_LIMIT=600 `
        CUSTOMER_PHONEPE_JWKS_URL="https://${STORAGE_ACCOUNT}.blob.core.windows.net/jwks/jwks.json" `
        CUSTOMER_PHONEPE_API_KEY=$PHONEPE_API_KEY

Write-Host "Environment variables configured" -ForegroundColor Green

az webapp log config `
    --name $WEB_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --application-logging filesystem `
    --docker-container-logging filesystem `
    --level verbose

Write-Host "Logging enabled" -ForegroundColor Green

# ============================================================
# STEP 5: Startup command (gunicorn with app:app)
# ============================================================
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "  STEP 5: Startup Command"                   -ForegroundColor Cyan
Write-Host "========================================`n"  -ForegroundColor Cyan

# app.py at the project root re-exports mds.main:app.
# PYTHONPATH includes src/ so the mds package is importable.
az webapp config appsettings set `
    --name $WEB_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --settings PYTHONPATH="/home/site/wwwroot/src"

az webapp config set `
    --name $WEB_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --startup-file "gunicorn -w 2 -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:8000 --timeout 300"

Write-Host "Startup command: gunicorn app:app" -ForegroundColor Green

# ============================================================
# STEP 6: Managed Identity + RBAC
# ============================================================
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "  STEP 6: Managed Identity"                  -ForegroundColor Cyan
Write-Host "========================================`n"  -ForegroundColor Cyan

$identityResult = az webapp identity assign `
    --name $WEB_APP_NAME `
    --resource-group $RESOURCE_GROUP | ConvertFrom-Json

$principalId = $identityResult.principalId
Write-Host "Managed Identity Principal: $principalId" -ForegroundColor Green

# ML Registry access
Write-Host "Granting Contributor on ML Registry..." -ForegroundColor Yellow
$registryId = az ml registry show --name $REGISTRY_NAME --resource-group $RESOURCE_GROUP --query "id" -o tsv 2>$null
if ($registryId) {
    az role assignment create --assignee $principalId --role "Contributor" --scope $registryId 2>$null
    Write-Host "  Contributor on ML Registry: OK" -ForegroundColor Green
} else {
    Write-Host "  ML Registry not found - skipping" -ForegroundColor Yellow
}

# Storage access
Write-Host "Granting Storage Blob Data Contributor..." -ForegroundColor Yellow
$storageId = az storage account show --name $STORAGE_ACCOUNT --resource-group $RESOURCE_GROUP --query "id" -o tsv 2>$null
if ($storageId) {
    az role assignment create --assignee $principalId --role "Storage Blob Data Contributor" --scope $storageId 2>$null
    Write-Host "  Storage Blob Data Contributor: OK" -ForegroundColor Green
} else {
    Write-Host "  Storage account not found - skipping" -ForegroundColor Yellow
}

# ============================================================
# STEP 7: Package and deploy
# ============================================================
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "  STEP 7: Deploy Code"                       -ForegroundColor Cyan
Write-Host "========================================`n"  -ForegroundColor Cyan

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$zipPath = Join-Path $projectRoot "deploy.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

# Build deployment zip using Python to ensure forward-slash paths.
# Windows Compress-Archive creates entries with backslashes (\) which
# Linux Kudu rejects with EINVAL.  Python's zipfile module always
# writes entries with forward slashes.
#
# Zip layout:
#   app.py                  (thin entry point)
#   requirements.txt
#   src/mds/__init__.py
#   src/mds/main.py
#   src/mds/auth.py
#   src/mds/azure_clients.py
#   src/mds/catalog.py
#   src/mds/customers.py
#   src/mds/jwks.py
#   src/mds/metadata.py

Write-Host "Creating deployment package (Python zipfile)..." -ForegroundColor Yellow

$pyScript = @"
import zipfile, os, pathlib, sys

root  = pathlib.Path(r'$($projectRoot.Replace("'","''"))').resolve()
out   = root / 'deploy.zip'

files = ['app.py', 'requirements.txt']

mds_dir = root / 'src' / 'mds'
for f in sorted(mds_dir.glob('*.py')):
    files.append(f'src/mds/{f.name}')

with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
    for rel in files:
        full = root / rel
        if not full.exists():
            print(f'  WARNING: {rel} not found, skipping', file=sys.stderr)
            continue
        arc = rel.replace('\\\\', '/')
        zf.write(str(full), arc)
        print(f'  {arc}')

print(f'\nCreated {out}  ({out.stat().st_size:,} bytes)')
"@

python -c $pyScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: failed to create zip" -ForegroundColor Red
    exit 1
}

Write-Host "`nDeploying to Azure (Oryx remote build)..." -ForegroundColor Yellow

az webapp deployment source config-zip `
    --name $WEB_APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --src $zipPath `
    --timeout 600

Write-Host "Deployment complete!" -ForegroundColor Green

# Clean up
Remove-Item $zipPath -ErrorAction SilentlyContinue

# ============================================================
# STEP 8: Summary
# ============================================================
$appUrl = "https://$WEB_APP_NAME.azurewebsites.net"

Write-Host "`n========================================"  -ForegroundColor Green
Write-Host "  DEPLOYMENT COMPLETE"                       -ForegroundColor Green
Write-Host "========================================`n"  -ForegroundColor Green

Write-Host "URL: $appUrl"                                -ForegroundColor Yellow
Write-Host ""
Write-Host "Test:"                                        -ForegroundColor Green
Write-Host "  Invoke-RestMethod -Uri '$appUrl/health'"   -ForegroundColor Yellow
Write-Host ""
Write-Host "Logs:"                                        -ForegroundColor Green
Write-Host "  az webapp log tail --name $WEB_APP_NAME --resource-group $RESOURCE_GROUP" -ForegroundColor Yellow
Write-Host ""
Write-Host "Resource layout:"
Write-Host "  $RESOURCE_GROUP -> App Service + ML Registry + Blob Storage (all in one RG)"
Write-Host ""
Write-Host "The App Service uses Managed Identity to access ML Registry and Storage."
Write-Host ""
