# Security Review: Private Networking and Identity Controls

**Review Date:** February 2026  
**Reviewer:** Azure Agents Control Plane Team  
**Scope:** Private networking, identity architecture, RBAC, and service-to-service authentication

---

## Executive Summary

This document provides a comprehensive security review of the Azure Agents Control Plane implementation, focusing on private networking, identity-first access, least-privilege RBAC, and service-to-service authentication. The review validates alignment with enterprise security expectations for production workloads.

### Key Findings

✅ **Strengths:**
- Strong identity architecture with Entra Agent Identity and Workload Identity federation
- Comprehensive RBAC implementation with least-privilege assignments
- Proper use of managed identities (no hard-coded secrets)
- Private endpoints configured for all data plane services
- Key-based authentication disabled (`disableLocalAuth: true`)
- Microsoft Defender for Cloud integration available

⚠️ **Areas Requiring Attention:**
- **APIM → AKS traffic currently uses public LoadBalancer** (Primary Gap)
- Container Registry lacks private endpoint configuration
- APIM service itself is not configured with VNet integration
- Some monitoring services require public access for Azure portal integration

### Security Posture: **GOOD** (with recommended improvements)

---

## 1. Private Networking Review

### 1.1 Virtual Network Architecture

**Configuration:** `infra/app/vnet.bicep`

```
VNet: 10.0.0.0/16
├── private-endpoints-subnet: 10.0.1.0/24
│   └── Private endpoints for Azure services
└── app-subnet: 10.0.2.0/24
    └── AKS node pools
```

✅ **Validated:**
- VNet properly segmented with dedicated subnets
- Private endpoint network policies disabled (required for private endpoints)
- AKS uses Azure CNI with network policy enabled

### 1.2 Private Endpoints - Data Plane Services

All backing services properly configured with private endpoints when `vnetEnabled=true`:

#### ✅ Cosmos DB (`infra/app/cosmos-PrivateEndpoint.bicep`)
- Private endpoint: `{resourceName}-cosmos-private-endpoint`
- DNS Zone: `privatelink.documents.azure.com`
- Group ID: `Sql`
- Public access: `Disabled` (when VNet enabled)
- **Status:** ✅ Properly configured

#### ✅ Azure AI Foundry (`infra/app/foundry-PrivateEndpoint.bicep`)
- Private endpoint: `pe-{resourceName}-account`
- DNS Zones:
  - `privatelink.cognitiveservices.azure.com`
  - `privatelink.openai.azure.com`
  - `privatelink.services.ai.azure.com`
- Group ID: `account`
- Public access: `Disabled` (when VNet enabled)
- **Status:** ✅ Properly configured

#### ✅ Azure Storage (`infra/app/storage-PrivateEndpoint.bicep`)
- Blob private endpoint: `blob-private-endpoint`
- Queue private endpoint: `queue-private-endpoint`
- DNS Zones:
  - `privatelink.blob.core.windows.net`
  - `privatelink.queue.core.windows.net`
- Public access: `Disabled` (when VNet enabled)
- Network ACLs: `defaultAction: Deny`
- **Status:** ✅ Properly configured

#### ✅ Azure AI Search (`infra/core/search/search-service.bicep`)
- Built-in private endpoint support
- DNS Zone: `privatelink.search.windows.net`
- Public access: `Disabled` (when VNet enabled)
- Network bypass: `AzurePortal` (required for management)
- **Status:** ✅ Properly configured

#### ✅ Microsoft Fabric (Optional) (`infra/app/fabric-PrivateEndpoint.bicep`)
- OneLake private endpoint for Fabric capacity
- Conditional deployment when `fabricEnabled=true`
- **Status:** ✅ Properly configured

### 1.3 APIM → AKS Communication Path

⚠️ **PRIMARY GAP IDENTIFIED**

**Current Architecture:**
```
APIM Gateway (Public)
    ↓ HTTP
Public LoadBalancer (Azure-assigned IP)
    ↓
AKS Service (LoadBalancer type)
    ↓
MCP Server Pods
```

**Current Configuration:**
- File: `k8s/mcp-agents-loadbalancer.yaml`
- LoadBalancer type: `Public` (`azure-load-balancer-internal: "false"`)
- Backend URL: `http://${publicIpAddress}/runtime/webhooks/mcp`
- Static public IP allocated via `infra/core/network/public-ip.bicep`

**Security Implications:**
1. ⚠️ MCP server endpoints exposed on public internet (mitigated by APIM authentication)
2. ⚠️ Traffic between APIM and AKS traverses public internet
3. ⚠️ Potential for direct access to AKS LoadBalancer if IP is discovered

**Mitigation in Place:**
- OAuth authentication enforced at APIM layer
- AKS workload identity provides service-to-service auth
- No secrets exposed in URLs

**Recommended Remediation:**
1. **Short-term:** Deploy Internal LoadBalancer with VNet integration
2. **Medium-term:** Deploy APIM in VNet-integrated mode (Internal or External SKU)
3. **Long-term:** Consider Azure Private Link for APIM

See [Section 6: Remediation Plan](#6-remediation-plan-for-private-networking) for detailed implementation.

### 1.4 Azure Container Registry (ACR)

⚠️ **GAP IDENTIFIED**

**Current Configuration:** `infra/core/acr/container-registry.bicep`
- Public network access: `Enabled`
- No private endpoint configured

**Security Implications:**
1. ⚠️ Image pulls occur over public internet
2. ⚠️ Container images potentially accessible publicly (mitigated by authentication)

**Mitigation in Place:**
- AKS cluster identity has `AcrPull` role only (least privilege)
- ACR requires authentication for all operations

**Recommended Remediation:**
- Add private endpoint for ACR to VNet
- Set `publicNetworkAccess: 'Disabled'`
- Update AKS to use private endpoint for image pulls

### 1.5 API Management (APIM)

⚠️ **GAP IDENTIFIED**

**Current Configuration:** `infra/core/apim/apim.bicep`
- SKU: `Basicv2` (no VNet integration support)
- Gateway: Public endpoint only
- No VNet configuration

**Security Implications:**
1. ⚠️ APIM gateway accessible from public internet (expected for client access)
2. ⚠️ Backend communication to AKS uses public paths

**Mitigation in Place:**
- OAuth authentication required for all MCP API calls
- Rate limiting policies enforced
- Entra ID integration for identity

**Recommended Remediation Options:**
1. **Option A:** Keep APIM public, use Internal LoadBalancer for AKS (Recommended)
2. **Option B:** Upgrade to Premium SKU with Internal VNet mode
3. **Option C:** Use Application Gateway with APIM for additional security layer

### 1.6 Monitoring and Observability Services

**Current Configuration:**
- Application Insights: Public access required for portal integration
- Log Analytics: Public access required for data ingestion
- Azure Monitor Workspace: Public access (`Enabled`)
- Managed Grafana: Configurable (`publicNetworkAccess` parameter)

✅ **Validated:**
- Public access is required for Azure portal integration and metrics collection
- No sensitive data exposed through monitoring endpoints (authentication required)
- Local authentication disabled where supported (`disableLocalAuth: true`)

---

## 2. Identity & Authentication Review

### 2.1 Identity Architecture Overview

The solution implements a **multi-layered identity model** with clear security boundaries:

```
Infrastructure Plane: AKS Cluster Control
    └── id-aks-{token} (User Assigned Managed Identity)

Monitoring Plane: Log Collection
    └── omsagent-aks-{token} (System Managed by AKS)

Deployment Plane: Identity Bootstrap
    ├── entra-app-user-assigned-identity (APIM OAuth)
    └── id-mcp-{token} (Agent Blueprint Creation)

AI Agent Runtime Plane: Workload Authentication
    └── NextBestAction-Agent-{token} (Entra Agent Identity)
        └── Workload Identity Federation → mcp-agent-sa
```

✅ **Validated:** Clear separation of concerns with dedicated identities per plane.

### 2.2 Entra Agent Identity (Primary Runtime Identity)

**Configuration:** `infra/core/identity/agentIdentity.bicep`

✅ **Validated Features:**
- **Type:** Entra Agent Identity (purpose-built for AI agents)
- **Blueprint-based:** Uses Agent Identity Blueprint pattern
- **Sponsor accountability:** Configurable sponsor via `agentSponsorPrincipalId`
- **Workload Identity Federation:** AKS ServiceAccount `mcp-agent-sa` federated
- **OAuth2 scope:** `next_best_action` (fine-grained access control)

**Authentication Flow:**
```
MCP Pod (mcp-agent-sa)
    → AKS OIDC Token
    → Federated Credential Exchange
    → Entra Agent Identity Token
    → Azure Resource Access
```

✅ **Validation:** No secrets required - completely passwordless authentication.

### 2.3 Managed Identities Inventory

#### ✅ Infrastructure Plane

**1. AKS Cluster Identity** (`id-aks-{token}`)
- **Type:** User Assigned Managed Identity
- **Purpose:** AKS control plane operations
- **Roles:** AcrPull (ACR access only)
- **Created by:** `infra/core/identity/userAssignedIdentity.bicep`

**2. Kubelet Identity** (`aks-{token}-agentpool`)
- **Type:** System-managed by AKS
- **Purpose:** Node-level operations (image pulls, storage)
- **Location:** AKS managed resource group
- **Status:** ✅ Properly isolated from application workloads

#### ✅ Monitoring Plane

**3. Container Insights Identity** (`omsagent-aks-{token}`)
- **Type:** System-managed by AKS Container Insights add-on
- **Purpose:** Log and metrics collection
- **Scope:** Read-only access to container logs
- **Status:** ✅ Properly scoped

#### ✅ Deployment Plane

**4. APIM OAuth Identity** (`entra-app-user-assigned-identity`)
- **Type:** User Assigned Managed Identity
- **Purpose:** APIM OAuth/Entra ID operations
- **Required Permissions:** Application.ReadWrite.All (Graph API)
- **Usage:** Dynamic Entra app registration for OAuth clients
- **Status:** ✅ Necessary for APIM OAuth functionality

**5. MCP Deployment Identity** (`id-mcp-{token}`)
- **Type:** User Assigned Managed Identity
- **Purpose:** Agent Identity Blueprint creation via Graph API
- **Required Permissions:**
  - `AgentIdentityBlueprint.Create`
  - `AgentIdentityBlueprint.AddRemoveCreds.All`
  - `AgentIdentityBlueprint.ReadWrite.All`
- **RBAC:**
  - Storage: Blob Data Owner (deployment scripts)
  - Storage: Queue Data Contributor (deployment scripts)
- **Status:** ✅ Properly scoped for deployment tasks

#### ✅ AI Agent Runtime Plane

**6. Entra Agent Identity** (`NextBestAction-Agent-{token}`)
- **Type:** Entra Agent Identity
- **Blueprint:** `NextBestAction-Blueprint-{token}`
- **Federated to:** `mcp-agent-sa` ServiceAccount in AKS
- **RBAC:** See [Section 3: RBAC Review](#3-rbac--authorization-review)
- **Status:** ✅ Fully configured with workload identity

### 2.4 Secrets Management

✅ **No Hard-Coded Secrets Validation:**

**Checked Locations:**
1. ✅ Infrastructure code (Bicep) - No connection strings or keys
2. ✅ Application code (Python) - Uses Azure SDK with DefaultAzureCredential
3. ✅ Kubernetes manifests - ServiceAccount token projection only
4. ✅ Environment variables - Identity client IDs only (not secrets)

**Authentication Methods Used:**
- Managed Identity / Workload Identity: ✅ All Azure service access
- OAuth2 with Entra ID: ✅ APIM authentication
- AKS OIDC tokens: ✅ Workload identity federation

**Key Vault Usage:**
- Not currently deployed
- Not required - no secrets to store

✅ **Validation:** Complete passwordless architecture. No secrets required for service-to-service authentication.

### 2.5 APIM Authentication Model

**OAuth Configuration:** `infra/app/apim-oauth/oauth.bicep`

✅ **Validated:**
- OAuth 2.0 with Entra ID integration
- Dynamic client registration support
- Token validation at APIM gateway
- No shared secrets for client authentication (using OAuth flows)

**OAuth Scopes:**
- `openid` - Identity token
- `https://graph.microsoft.com/.default` - Graph API access
- Custom agent scopes (e.g., `next_best_action`)

---

## 3. RBAC & Authorization Review

### 3.1 RBAC Architecture

All role assignments follow the **least-privilege principle** with clear justification for each permission.

### 3.2 Agent Identity RBAC

**File:** `infra/app/agent-RoleAssignments.bicep`

Comprehensive RBAC for the Entra Agent Identity:

#### ✅ Cosmos DB

**Role:** `Cosmos DB Built-in Data Contributor` (Data plane, not control plane)
- **Role ID:** `00000000-0000-0000-0000-000000000002`
- **Scope:** Database account
- **Justification:** Read/write access to tasks, plans, and short-term memory
- **Data Access:** Tasks, plans, execution history
- **No Control Plane Access:** Cannot modify database configuration
- **Status:** ✅ Least privilege - data plane only

#### ✅ Azure AI Search

**Role 1:** `Search Index Data Contributor`
- **Role ID:** `8ebe5a00-799e-43f5-93ac-243d3dce84a7`
- **Scope:** Search service
- **Justification:** Read/write data in search indexes for Foundry IQ
- **Status:** ✅ Appropriate for AI Search operations

**Role 2:** `Search Service Contributor`
- **Role ID:** `7ca78c08-252a-4471-8644-bb5ff32d4ba0`
- **Scope:** Search service
- **Justification:** Manage indexes and knowledge bases
- **Note:** ⚠️ Broader than data-only access, but required for index management
- **Recommendation:** Consider if index management could be separated
- **Status:** ⚠️ Acceptable with justification

#### ✅ Azure Storage

**Role 1:** `Storage Blob Data Owner`
- **Role ID:** `b7e6dc6d-f1e8-4753-8033-0f276bb0955b`
- **Scope:** Storage account
- **Justification:** Full access to ontologies and snippets in blob storage
- **Containers:** `ontologies`, `snippets`
- **Status:** ✅ Appropriate for agent storage needs

**Role 2:** `Storage Queue Data Contributor`
- **Role ID:** `974c5e8b-45b9-4653-ba55-5f855dd0fb88`
- **Scope:** Storage account
- **Justification:** Message queue processing for agent communication
- **Status:** ✅ Least privilege for queue operations

#### ✅ Azure AI Foundry

**Role 1:** `Cognitive Services OpenAI User`
- **Role ID:** `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`
- **Scope:** Foundry account
- **Justification:** Access to GPT-5.2-chat model for agent inference
- **Status:** ✅ Least privilege for model access

**Role 2:** `Cognitive Services OpenAI Contributor`
- **Role ID:** `a001fd3d-188f-4b5d-821b-7da978bf7442`
- **Scope:** Foundry account
- **Justification:** Manage model deployments
- **Note:** ⚠️ Broader than read-only, but required for deployment management
- **Recommendation:** Consider if deployment management could be separated
- **Status:** ⚠️ Acceptable with justification

### 3.3 MCP Deployment Identity RBAC

**Assigned Roles:**
- Storage Blob Data Owner (deployment scripts)
- Storage Queue Data Contributor (deployment scripts)

✅ **Validation:** Minimal permissions for deployment operations only.

### 3.4 Fabric Data Agents RBAC (Optional)

**File:** `infra/app/fabric-data-agents.bicep`

When `fabricDataAgentsEnabled=true`:
- Contributor role on Fabric capacity (for lakehouse/warehouse operations)
- Scoped to specific Fabric workspace

✅ **Validation:** Properly isolated to Fabric resources only.

### 3.5 Developer Access (Optional)

**Configuration:** `main.bicep`

Optional developer access for local development:
- Parameter: `developerPrincipalId`
- Cosmos DB: Data Contributor role
- IP-based firewall rule: `developerIpAddress`

✅ **Validation:** Optional and properly scoped to individual developer identity.

### 3.6 Monitoring Access

**File:** `infra/main.bicep` (line 843)

**Role:** `Monitoring Metrics Publisher`
- **Role ID:** `3913510d-42f4-4e42-8a64-420c390055eb`
- **Assigned to:** Agent identity
- **Scope:** Subscription/Resource Group
- **Justification:** Publish custom metrics to Azure Monitor
- **Status:** ✅ Read-only equivalent for metrics publishing

### 3.7 Grafana Access (Optional)

**File:** `infra/app/grafana-RoleAssignment.bicep`

**Role:** `Grafana Admin`
- **Assigned to:** Configurable principal (typically admin user)
- **Justification:** Dashboard management and monitoring
- **Status:** ✅ Administrative access appropriately separated

### 3.8 RBAC Summary Table

| Identity | Service | Role | Justification | Status |
|----------|---------|------|---------------|--------|
| Agent Identity | Cosmos DB | Data Contributor | Task/plan storage | ✅ Least Privilege |
| Agent Identity | AI Search | Index Data Contributor | Search operations | ✅ Least Privilege |
| Agent Identity | AI Search | Service Contributor | Index management | ⚠️ Justified |
| Agent Identity | Storage (Blob) | Blob Data Owner | Ontology access | ✅ Least Privilege |
| Agent Identity | Storage (Queue) | Queue Contributor | Messaging | ✅ Least Privilege |
| Agent Identity | Foundry | OpenAI User | Model inference | ✅ Least Privilege |
| Agent Identity | Foundry | OpenAI Contributor | Deployment mgmt | ⚠️ Justified |
| Agent Identity | Monitor | Metrics Publisher | Telemetry | ✅ Least Privilege |
| MCP Identity | Storage | Blob Data Owner | Deployment | ✅ Justified |
| MCP Identity | Storage | Queue Contributor | Deployment | ✅ Justified |
| AKS Identity | ACR | AcrPull | Image pulls | ✅ Least Privilege |

**Overall RBAC Assessment:** ✅ **Strong** - All assignments follow least-privilege with clear justification.

### 3.9 RBAC Recommendations

1. ⚠️ **Consider:** Separate index management from runtime agent identity
   - Create dedicated "admin" identity for Search Service Contributor
   - Agent runtime uses only Index Data Contributor

2. ⚠️ **Consider:** Separate model deployment from runtime agent identity
   - Create dedicated "admin" identity for OpenAI Contributor
   - Agent runtime uses only OpenAI User

3. ✅ **Best Practice:** Document RBAC decisions in code comments
   - Current: Role IDs documented
   - Enhancement: Add justification comments in Bicep files

---

## 4. Trust Boundaries and Data Flow

### 4.1 Trust Boundary Map

```
┌─────────────────────────────────────────────────────────────────┐
│                     EXTERNAL / PUBLIC INTERNET                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  AI Agent Clients (Claude, ChatGPT, Custom)              │   │
│  └─────────────────────────┬────────────────────────────────┘   │
└────────────────────────────┼────────────────────────────────────┘
                             │ HTTPS + OAuth
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   AZURE API MANAGEMENT (DMZ)                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  OAuth Validation, Rate Limiting, Routing                │   │
│  └─────────────────────────┬────────────────────────────────┘   │
└────────────────────────────┼────────────────────────────────────┘
                             │ HTTP (⚠️ Public Path)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              AKS CLUSTER (PRIVATE COMPUTE)                      │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  LoadBalancer → MCP Server Pods                          │   │
│  │  Identity: Workload Identity (Entra Agent)               │   │
│  └─────────────────────────┬────────────────────────────────┘   │
└────────────────────────────┼────────────────────────────────────┘
                             │ Private Endpoints
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                  DATA PLANE SERVICES (PRIVATE)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────────┐   │
│  │  Cosmos DB   │  │  AI Foundry  │  │  Storage + Search  │   │
│  │  (Private)   │  │  (Private)   │  │  (Private)         │   │
│  └──────────────┘  └──────────────┘  └─────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**Trust Boundaries:**
1. **Internet → APIM:** OAuth + TLS encryption
2. **APIM → AKS:** ⚠️ Public internet (mitigated by auth)
3. **AKS → Data Services:** ✅ Private endpoints only
4. **Within AKS:** ✅ Kubernetes network policies

### 4.2 Authentication Flow

**MCP Client Request Flow:**
```
1. Client → APIM /authorize
   └─> Entra ID authentication

2. Client receives OAuth token

3. Client → APIM /mcp/sse or /mcp/message
   ├─> Token validation at APIM
   └─> Request forwarded to AKS

4. AKS Pod (with workload identity)
   ├─> Authenticates to Cosmos DB (Entra Agent Identity)
   ├─> Authenticates to AI Foundry (Entra Agent Identity)
   ├─> Authenticates to Storage (Entra Agent Identity)
   └─> Authenticates to AI Search (Entra Agent Identity)

5. Response → Client
```

✅ **Validation:** Every hop authenticated with identity-based credentials (no secrets).

### 4.3 Data Plane Traffic Analysis

**All data plane traffic uses private endpoints when `vnetEnabled=true`:**

| Source | Destination | Path | Protocol | Status |
|--------|-------------|------|----------|--------|
| AKS Pod | Cosmos DB | Private Endpoint | HTTPS | ✅ Private |
| AKS Pod | AI Foundry | Private Endpoint | HTTPS | ✅ Private |
| AKS Pod | Storage (Blob) | Private Endpoint | HTTPS | ✅ Private |
| AKS Pod | Storage (Queue) | Private Endpoint | HTTPS | ✅ Private |
| AKS Pod | AI Search | Private Endpoint | HTTPS | ✅ Private |
| AKS Pod | Fabric (OneLake) | Private Endpoint | HTTPS | ✅ Private |
| APIM | AKS | Public LoadBalancer | HTTP | ⚠️ Public |

---

## 5. Security Best Practices Validation

### ✅ Identity-First Security
- [x] All service-to-service auth uses managed identities
- [x] Entra Agent Identity for AI workloads
- [x] Workload Identity federation for Kubernetes
- [x] No connection strings or API keys in code

### ✅ Least-Privilege RBAC
- [x] Data plane roles (not control plane where possible)
- [x] Scoped to specific resources
- [x] Justification documented for each role
- [x] Separation of deployment vs. runtime identities

### ✅ Private Networking (Partial)
- [x] All data services use private endpoints
- [x] VNet with proper subnet segmentation
- [x] Public access disabled for data services when VNet enabled
- [ ] ⚠️ APIM → AKS uses public path (gap identified)
- [ ] ⚠️ ACR lacks private endpoint (gap identified)

### ✅ Audit and Compliance
- [x] Microsoft Defender for Cloud integration available
- [x] Application Insights with Azure Monitor
- [x] Log Analytics for audit trail
- [x] Managed Grafana for observability

### ✅ Defense in Depth
- [x] OAuth authentication at API gateway
- [x] Workload identity for runtime
- [x] Network segmentation with subnets
- [x] Rate limiting policies at APIM
- [x] Container vulnerability scanning (via Defender)

### ✅ Secrets Management
- [x] No secrets in code or configuration
- [x] No connection strings stored
- [x] Key-based auth disabled (`disableLocalAuth: true`)
- [x] Shared key access minimal (Storage only for deployment)

---

## 6. Remediation Plan for Private Networking

### Priority 1: Internal LoadBalancer for AKS ⚠️

**Current:** Public LoadBalancer with static IP  
**Target:** Internal LoadBalancer with VNet integration

**Implementation Steps:**

1. **Update LoadBalancer Configuration**
   
   File: `k8s/mcp-agents-loadbalancer.yaml`
   
   ```yaml
   metadata:
     annotations:
       service.beta.kubernetes.io/azure-load-balancer-internal: "true"  # Changed from "false"
       service.beta.kubernetes.io/azure-load-balancer-internal-subnet: "app"  # Added
   spec:
     type: LoadBalancer
     # Remove or comment out static public IP
     # loadBalancerIP: "${MCP_PUBLIC_IP_ADDRESS}"
   ```

2. **Deploy APIM in VNet-Integrated Mode**

   **Option A: External VNet Mode** (Recommended)
   - APIM gateway remains publicly accessible
   - Backend pool uses private VNet connectivity
   - Requires Premium SKU

   **Option B: Internal VNet Mode**
   - APIM gateway only accessible from VNet
   - Requires Application Gateway for external access
   - Requires Premium SKU

3. **Update APIM Backend URL**

   File: `infra/main.bicep` (line 202)
   
   ```bicep
   // Before:
   mcpServerBackendUrl: 'http://${mcpPublicIp.outputs.publicIpAddress}/runtime/webhooks/mcp'
   
   // After:
   mcpServerBackendUrl: 'http://mcp-agents-loadbalancer.mcp-agents.svc.cluster.local/runtime/webhooks/mcp'
   // OR if using Internal LB with VNet:
   mcpServerBackendUrl: 'http://${internalLoadBalancerIP}/runtime/webhooks/mcp'
   ```

4. **Remove Public IP Module** (if using fully internal)

   File: `infra/main.bicep` (lines 338-349)
   - Comment out or conditionally deploy public IP module

**Impact:**
- **Security:** ✅ Eliminates public exposure of MCP endpoints
- **Cost:** Neutral (Premium SKU required for VNet mode costs more)
- **Complexity:** Medium (requires APIM SKU upgrade)
- **Downtime:** Yes (requires LoadBalancer recreation)

**Testing:**
```bash
# Verify internal LoadBalancer
kubectl get svc -n mcp-agents mcp-agents-loadbalancer

# Test connectivity from APIM
# (requires APIM to be in VNet or peered VNet)
```

### Priority 2: Private Endpoint for Azure Container Registry

**Implementation Steps:**

1. **Create ACR Private Endpoint Module**

   File: `infra/app/acr-PrivateEndpoint.bicep` (new file)

   ```bicep
   param virtualNetworkName string
   param subnetName string
   param acrName string
   param location string = resourceGroup().location
   param tags object = {}

   resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
     name: acrName
   }

   resource vnet 'Microsoft.Network/virtualNetworks@2021-08-01' existing = {
     name: virtualNetworkName
   }

   resource privateDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
     name: 'privatelink.azurecr.io'
     location: 'global'
     tags: tags
   }

   resource privateDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
     parent: privateDnsZone
     name: '${acrName}-dns-link'
     location: 'global'
     properties: {
       registrationEnabled: false
       virtualNetwork: {
         id: vnet.id
       }
     }
   }

   resource privateEndpoint 'Microsoft.Network/privateEndpoints@2021-08-01' = {
     name: '${acrName}-pe'
     location: location
     tags: tags
     properties: {
       privateLinkServiceConnections: [
         {
           name: 'acrConnection'
           properties: {
             privateLinkServiceId: acr.id
             groupIds: ['registry']
           }
         }
       ]
       subnet: {
         id: '${vnet.id}/subnets/${subnetName}'
       }
     }
   }

   resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2021-08-01' = {
     parent: privateEndpoint
     name: 'default'
     properties: {
       privateDnsZoneConfigs: [
         {
           name: 'config'
           properties: {
             privateDnsZoneId: privateDnsZone.id
           }
         }
       ]
     }
   }
   ```

2. **Update ACR Module**

   File: `infra/core/acr/container-registry.bicep`

   ```bicep
   // Add parameters
   param publicNetworkAccess string = 'Enabled'
   
   resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
     // ... existing config ...
     properties: {
       // Add:
       publicNetworkAccess: publicNetworkAccess
       networkRuleBypassOptions: 'AzureServices'
     }
   }
   ```

3. **Add Module to Main**

   File: `infra/main.bicep`

   ```bicep
   module acrPrivateEndpoint 'app/acr-PrivateEndpoint.bicep' = if (vnetEnabled) {
     name: 'acrPrivateEndpoint'
     scope: rg
     params: {
       location: location
       tags: tags
       virtualNetworkName: serviceVirtualNetworkName
       subnetName: serviceVirtualNetworkPrivateEndpointSubnetName
       acrName: containerRegistry.outputs.containerRegistryName
     }
     dependsOn: [
       serviceVirtualNetwork
     ]
   }
   ```

**Impact:**
- **Security:** ✅ Eliminates public access to container images
- **Cost:** Minimal (private endpoint costs)
- **Complexity:** Low
- **Downtime:** No

### Priority 3: APIM VNet Integration (Optional - Requires Premium SKU)

**Cost Consideration:** APIM Premium SKU significantly more expensive than Basicv2.

**Alternative:** Use APIM Basicv2 with Internal LoadBalancer (Priority 1) for most use cases.

---

## 7. Compliance and Governance

### Microsoft Defender for Cloud Integration

✅ **Available and Configurable**

**Configuration:** `infra/main.bicep` + `infra/core/security/defender.bicep`

**Enabled Plans:**
- Defender for Containers (AKS + ACR)
- Defender for Key Vault
- Defender for Azure Cosmos DB
- Defender for APIs (APIM)
- Defender for Resource Manager
- Defender for Container Registry

**Activation:**
```bash
azd env set DEFENDER_ENABLED true
azd env set DEFENDER_SECURITY_CONTACT_EMAIL "security@example.com"
azd provision
```

✅ **Recommendation:** Enable for production deployments.

### Azure Policy Compliance

**Potential Policies to Consider:**
1. ✅ Require managed identities for Azure resources
2. ✅ Disable public network access for PaaS services
3. ✅ Require private endpoints for Azure services
4. ✅ Enable diagnostic logging for all resources
5. ⚠️ Restrict public IPs on LoadBalancers (after remediation)

### Audit Logging

✅ **Configured:**
- Application Insights: Application telemetry
- Log Analytics: Infrastructure logs
- AKS Container Insights: Pod logs
- Azure Activity Log: Control plane operations

✅ **Recommendation:** Configure log retention policies per compliance requirements.

---

## 8. Recommendations Summary

### Immediate Actions (High Priority)

1. ⚠️ **Implement Internal LoadBalancer for AKS**
   - Impact: High security improvement
   - Effort: Medium (requires SKU considerations for APIM)
   - Timeline: Next deployment cycle

2. ✅ **Add Private Endpoint for ACR**
   - Impact: Medium security improvement
   - Effort: Low
   - Timeline: Next deployment cycle

3. ✅ **Enable Microsoft Defender for Cloud**
   - Impact: Enhanced threat detection
   - Effort: Low (configuration only)
   - Timeline: Immediate

### Short-Term Improvements (Medium Priority)

4. ⚠️ **Separate Admin RBAC from Runtime RBAC**
   - Search Service Contributor → Admin identity only
   - OpenAI Contributor → Admin identity only
   - Impact: Further least-privilege refinement
   - Effort: Medium
   - Timeline: Next major version

5. ✅ **Document RBAC Decisions in Code**
   - Add justification comments to Bicep files
   - Impact: Improved maintainability
   - Effort: Low
   - Timeline: Ongoing

### Long-Term Considerations (Low Priority)

6. **Consider APIM Premium SKU with VNet Integration**
   - Evaluate cost vs. security benefits
   - Required for fully private architecture
   - Timeline: Future architecture evolution

7. **Implement Azure Policy Enforcement**
   - Deploy policy definitions for security controls
   - Automated compliance validation
   - Timeline: Post-production hardening

---

## 9. Conclusion

### Overall Security Assessment: ✅ **GOOD**

The Azure Agents Control Plane implementation demonstrates **strong security fundamentals** with:
- Excellent identity architecture (Entra Agent Identity + Workload Identity)
- Comprehensive RBAC with least-privilege principles
- Proper use of private endpoints for all data services
- No hard-coded secrets or connection strings
- Strong defense-in-depth with multiple security layers

### Key Gaps Identified:

1. **APIM → AKS uses public LoadBalancer** (Highest priority for remediation)
2. **ACR lacks private endpoint** (Medium priority)
3. **Some RBAC roles broader than strictly necessary** (Low priority - justified)

### Recommendation:

**The current architecture is suitable for enterprise deployment with the understanding that APIM → AKS traffic uses public paths mitigated by strong authentication.**

**For environments requiring fully private connectivity, implement Priority 1 remediation (Internal LoadBalancer) before production deployment.**

---

## 10. References

### Documentation
- [Azure Agents Identity Design](AGENTS_IDENTITY_DESIGN.md)
- [Azure Agents Architecture](AGENTS_ARCHITECTURE.md)
- [Microsoft Defender for Cloud Testing](DEFENDER_FOR_CLOUD_TESTING.md)

### Azure Best Practices
- [Azure Well-Architected Framework - Security](https://learn.microsoft.com/azure/well-architected/security/)
- [AKS Security Best Practices](https://learn.microsoft.com/azure/aks/concepts-security)
- [Azure Private Link Best Practices](https://learn.microsoft.com/azure/private-link/private-link-overview)
- [Workload Identity for AKS](https://learn.microsoft.com/azure/aks/workload-identity-overview)
- [Entra Agent Identity](https://learn.microsoft.com/entra/agent-id/)

### Compliance Frameworks
- [Microsoft Cloud Security Benchmark](https://learn.microsoft.com/security/benchmark/azure/)
- [Azure Security Baseline](https://learn.microsoft.com/security/benchmark/azure/security-baselines-overview)

---

**Document Version:** 1.0  
**Last Updated:** February 2026  
**Next Review:** Quarterly or after significant architecture changes
