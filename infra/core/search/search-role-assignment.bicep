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

// Reference existing search service
resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: searchServiceName
}

// Role Assignment
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchService.id, principalID, roleDefinitionID)
  scope: searchService
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionID)
    principalId: principalID
    principalType: principalType
  }
}

output roleAssignmentId string = roleAssignment.id
