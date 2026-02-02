// Role Assignment for Azure AI Search
// Assigns a role to a principal for the search service

@description('Name of the Azure AI Search service')
param searchServiceName string

@description('Role Definition ID to assign')
param roleDefinitionID string

@description('Principal ID to assign the role to')
param principalID string

@description('Principal type (ServicePrincipal, User, Group)')
@allowed(['ServicePrincipal', 'User', 'Group'])
param principalType string = 'ServicePrincipal'

@description('Optional suffix to make role assignment name unique across deployments')
param nameSuffix string = ''

// Reference existing search service
resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: searchServiceName
}

// Role Assignment - name is deterministic based on scope, principal, and role to ensure idempotency
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: empty(nameSuffix) ? guid(searchService.id, principalID, roleDefinitionID) : guid(searchService.id, principalID, roleDefinitionID, nameSuffix)
  scope: searchService
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionID)
    principalId: principalID
    principalType: principalType
  }
}

output roleAssignmentId string = roleAssignment.id
