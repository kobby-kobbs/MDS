// Customer Onboarding - Creates storage + ML registry for a new MDS customer
// Usage: az deployment group create -g mds-<customer>-rg --template-file onboard.bicep --parameters customer=phonepe
//
// After deployment, set these env vars in the MDS App Service:
//   CUSTOMER_<UPPER>_REGISTRY = <registry.name>
//   CUSTOMER_<UPPER>_STORAGE  = <storage.name>
//   CUSTOMER_<UPPER>_ISSUER   = https://auth.<customer>.com
//   CUSTOMER_<UPPER>_JWKS_URL = https://auth.<customer>.com/.well-known/jwks.json

@minLength(3) @maxLength(15)
param customer string

param location string = resourceGroup().location

var name = toLower(customer)
var storageName = take('mds${name}store', 24)

// Storage for model files
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobServices
  name: 'models'
  properties: {
    publicAccess: 'None'
  }
}

// ML Registry for model metadata
resource registry 'Microsoft.MachineLearningServices/registries@2023-10-01' = {
  name: 'mds-${name}-registry'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: { publicNetworkAccess: 'Enabled' }
}

// Outputs
output config object = {
  customer: customer
  storage: storage.name
  container: 'models'
  registry: registry.name
  endpoint: storage.properties.primaryEndpoints.blob
  envVars: {
    registry: 'CUSTOMER_${toUpper(name)}_REGISTRY=${registry.name}'
    storage: 'CUSTOMER_${toUpper(name)}_STORAGE=${storage.name}'
  }
}
