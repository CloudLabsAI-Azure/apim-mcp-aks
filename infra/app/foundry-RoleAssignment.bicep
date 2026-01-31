@description('Name of the Foundry/Cognitive Services account')
param foundryAccountName string

@description('The role definition ID to assign')
param roleDefinitionID string

@description('The principal ID to assign the role to')
param principalID string

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, principalID, roleDefinitionID)
  scope: foundryAccount
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionID)
    principalId: principalID
    principalType: 'ServicePrincipal'
  }
}
