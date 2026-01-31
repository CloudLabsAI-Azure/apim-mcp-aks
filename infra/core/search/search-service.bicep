// Azure AI Search Service with Private Endpoint Support
// Enables Foundry IQ Knowledge Base integration for intelligent retrieval

@description('Name of the Azure AI Search service')
param name string

@description('Location for the resource')
param location string = resourceGroup().location

@description('Tags for the resource')
param tags object = {}

@description('SKU for the search service')
@allowed(['basic', 'standard', 'standard2', 'standard3'])
param sku string = 'basic'

@description('Number of replicas (1-12 for standard, 1-3 for basic)')
@minValue(1)
@maxValue(12)
param replicaCount int = 1

@description('Number of partitions (1, 2, 3, 4, 6, or 12)')
@allowed([1, 2, 3, 4, 6, 12])
param partitionCount int = 1

@description('Enable semantic search capability')
@allowed(['disabled', 'free', 'standard'])
param semanticSearch string = 'standard'

@description('Public network access setting')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

@description('Disable local authentication (API keys)')
param disableLocalAuth bool = true

@description('Enable private endpoint')
param enablePrivateEndpoint bool = false

@description('Virtual network name for private endpoint')
param virtualNetworkName string = ''

@description('Subnet name for private endpoint')
param subnetName string = ''

// Azure AI Search Service
resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: sku
  }
  properties: {
    replicaCount: replicaCount
    partitionCount: partitionCount
    hostingMode: 'default'
    publicNetworkAccess: publicNetworkAccess
    networkRuleSet: {
      bypass: 'AzurePortal'
    }
    disableLocalAuth: disableLocalAuth
    authOptions: disableLocalAuth ? null : {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
    semanticSearch: semanticSearch
  }
}

// Private DNS Zone for Search (created when private endpoint is enabled)
resource privateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (enablePrivateEndpoint) {
  name: 'privatelink.search.windows.net'
  location: 'global'
  tags: tags
}

// Link Private DNS Zone to VNet
resource privateDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (enablePrivateEndpoint && !empty(virtualNetworkName)) {
  parent: privateDnsZone
  name: '${name}-dns-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: resourceId('Microsoft.Network/virtualNetworks', virtualNetworkName)
    }
  }
}

// Private Endpoint for Azure AI Search
resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (enablePrivateEndpoint && !empty(virtualNetworkName) && !empty(subnetName)) {
  name: '${name}-pe'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: resourceId('Microsoft.Network/virtualNetworks/subnets', virtualNetworkName, subnetName)
    }
    privateLinkServiceConnections: [
      {
        name: '${name}-psc'
        properties: {
          privateLinkServiceId: searchService.id
          groupIds: ['searchService']
        }
      }
    ]
  }
}

// Private DNS Zone Group for Private Endpoint
resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (enablePrivateEndpoint) {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'privatelink-search-windows-net'
        properties: {
          privateDnsZoneId: privateDnsZone.id
        }
      }
    ]
  }
}

// Outputs
output name string = searchService.name
output endpoint string = 'https://${searchService.name}.search.windows.net'
output principalId string = searchService.identity.principalId
output resourceId string = searchService.id
