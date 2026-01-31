@description('Name of the foundry resource')
param foundryName string

@description('Name of the Bing Grounding resource')
param bingName string

@description('Location for all resources')
param location string

@description('Model deployment name')
param modelDeploymentName string

@description('Model name')
param modelName string

@description('Model version')
param modelVersion string

@description('Model capacity')
param modelCapacity int

@description('Tags for resources')
param tags object = {}

@description('Enable private endpoint')
param enablePrivateEndpoint bool = false

@description('Public network access setting')
param publicNetworkAccess string = 'Enabled'

// Create Bing Grounding resource
resource bingGrounding 'Microsoft.Bing/accounts@2020-06-10' = {
  name: bingName
  location: 'global'
  sku: {
    name: 'G1'
  }
  kind: 'Bing.Grounding'
  properties: {
    statisticsEnabled: false
  }
}

// Create AI Services foundry account
resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  tags: tags
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    apiProperties: {}
    customSubDomainName: foundryName
    networkAcls: {
      defaultAction: enablePrivateEndpoint ? 'Deny' : 'Allow'
      virtualNetworkRules: []
      ipRules: []
    }
    allowProjectManagement: true
    defaultProject: 'proj-default'
    associatedProjects: [
      'proj-default'
    ]
    publicNetworkAccess: publicNetworkAccess
    disableLocalAuth: true
  }
}

// Create Agents capability host
resource agentsCapabilityHost 'Microsoft.CognitiveServices/accounts/capabilityHosts@2025-06-01' = {
  parent: foundryAccount
  name: 'Agents'
  properties: {
    capabilityHostKind: 'Agents'
  }
}

// Deploy GPT model
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    currentCapacity: modelCapacity
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// Create default project
resource defaultProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundryAccount
  name: 'proj-default'
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'Default project for web summarization with Bing grounding'
    displayName: 'proj-default'
  }
}

// Create Bing connection at project level
resource bingConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: defaultProject
  name: bingName
  properties: {
    authType: 'ApiKey'
    category: 'ApiKey'
    target: 'https://api.bing.microsoft.com/'
    credentials: {
      key: listKeys(bingGrounding.id, bingGrounding.apiVersion).key1
    }
    useWorkspaceManagedIdentity: false
    isSharedToAll: false
    sharedUserList: []
    peRequirement: 'NotRequired'
    peStatus: 'NotApplicable'
    metadata: {
      type: 'bing_grounding'
      ApiType: 'Azure'
      ResourceId: bingGrounding.id
    }
  }
}

output foundryAccountId string = foundryAccount.id
output foundryAccountName string = foundryAccount.name
output foundryEndpoint string = foundryAccount.properties.endpoint
output projectId string = defaultProject.id
output projectEndpoint string = 'https://${foundryName}.services.ai.azure.com/api/projects/proj-default'
output bingConnectionId string = bingConnection.id
output bingConnectionName string = bingConnection.name
output bingResourceId string = bingGrounding.id
output modelDeploymentName string = modelDeployment.name
