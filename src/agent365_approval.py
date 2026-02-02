"""
Agent 365 Approval Workflow Module

This module provides the agent approval workflow implementation for Agents pipeline
deployments with Microsoft Teams human-in-the-loop integration.

Components:
1. Agent365AvailabilityChecker - Checks if Microsoft Agent 365 is available
2. EntraAgentRegistryClient - Registers agents with Microsoft Entra Agent Registry
3. ApprovalWorkflowEngine - Orchestrates the dual-track approval (Teams + Agent)
4. TeamsApprovalClient - Handles Microsoft Teams approval card interactions
5. ApprovalContract - Defines the approval data structure

References:
- Microsoft Entra Agent Registry: https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/what-is-agent-registry
- Teams Approvals: https://learn.microsoft.com/en-us/graph/approvals-app-api
- Agent 365 Governance: https://learn.microsoft.com/en-us/microsoft-agent-365/admin/capabilities-entra
"""

import json
import logging
import uuid
import asyncio
import aiohttp
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Callable, Awaitable
from enum import Enum
import os

from azure.identity import DefaultAzureCredential
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions

# Configure logging
logger = logging.getLogger(__name__)


class ApprovalDecision(Enum):
    """Approval decision outcomes."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    ERROR = "error"


class AgentValidationStatus(Enum):
    """Agent validation status after human decision."""
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class ApprovalContract:
    """
    Standard approval contract as specified in requirements.
    
    This contract represents the complete state of an approval request
    including both human decision and agent validation.
    """
    approval_id: str
    requested_by: str
    task: str
    environment: str
    decision: str = ApprovalDecision.PENDING.value
    approved_by: Optional[str] = None
    timestamp: Optional[str] = None
    agent_validation: str = AgentValidationStatus.PENDING.value
    
    # Extended fields for audit trail
    cluster: Optional[str] = None
    namespace: Optional[str] = None
    image_tags: Optional[List[str]] = None
    commit_sha: Optional[str] = None
    pipeline_url: Optional[str] = None
    rollback_url: Optional[str] = None
    comment: Optional[str] = None
    request_timestamp: Optional[str] = None
    resolution_time_seconds: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {k: v for k, v in asdict(self).items() if v is not None}
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalContract":
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    def is_complete(self) -> bool:
        """Check if approval is complete (human decided + agent validated)."""
        return (
            self.decision in [ApprovalDecision.APPROVED.value, ApprovalDecision.REJECTED.value]
            and self.agent_validation == AgentValidationStatus.PASSED.value
        )


@dataclass
class Agent365AvailabilityResult:
    """Result of Agent 365 availability check."""
    available: bool
    frontier_enrolled: bool = False
    agent_registry_accessible: bool = False
    graph_api_accessible: bool = False
    error_message: Optional[str] = None
    tenant_verification_steps: Optional[List[str]] = None


class Agent365AvailabilityChecker:
    """
    Checks whether Microsoft Agent 365 (Frontier preview) is available.
    
    Agent 365 availability requires:
    1. Enrollment in Microsoft Frontier preview program
    2. Access to Microsoft Entra Agent Registry APIs
    3. Proper Graph API permissions
    
    Reference: https://learn.microsoft.com/en-us/microsoft-agent-365/admin/capabilities-entra
    """
    
    GRAPH_API_BASE = "https://graph.microsoft.com/beta"
    AGENT_REGISTRY_PATH = "/agentRegistry/agentInstances"
    
    def __init__(self):
        self.credential = DefaultAzureCredential()
    
    async def check_availability(self) -> Agent365AvailabilityResult:
        """
        Perform comprehensive Agent 365 availability check.
        
        Returns:
            Agent365AvailabilityResult with availability status and diagnostics
        """
        result = Agent365AvailabilityResult(
            available=False,
            tenant_verification_steps=[]
        )
        
        try:
            # Step 1: Get access token for Graph API
            token = self.credential.get_token("https://graph.microsoft.com/.default")
            
            if not token:
                result.error_message = "Failed to acquire Graph API token"
                result.tenant_verification_steps = self._get_verification_checklist()
                return result
            
            result.graph_api_accessible = True
            
            # Step 2: Test Agent Registry API availability
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {token.token}",
                    "Content-Type": "application/json"
                }
                
                # Test agent registry endpoint
                url = f"{self.GRAPH_API_BASE}{self.AGENT_REGISTRY_PATH}"
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        result.agent_registry_accessible = True
                        result.frontier_enrolled = True
                        result.available = True
                    elif response.status == 403:
                        result.error_message = "Access denied - Frontier enrollment may be required"
                        result.tenant_verification_steps = self._get_verification_checklist()
                    elif response.status == 404:
                        result.error_message = "Agent Registry API not available - Frontier preview not enabled"
                        result.tenant_verification_steps = self._get_verification_checklist()
                    else:
                        body = await response.text()
                        result.error_message = f"Unexpected response: {response.status} - {body}"
            
        except Exception as e:
            result.error_message = f"Availability check failed: {str(e)}"
            result.tenant_verification_steps = self._get_verification_checklist()
        
        return result
    
    def _get_verification_checklist(self) -> List[str]:
        """
        Return tenant/admin verification checklist for Agent 365 access.
        
        Reference: https://adoption.microsoft.com/copilot/frontier-program/
        """
        return [
            "1. FRONTIER ENROLLMENT: Verify your organization is enrolled in Microsoft Frontier preview program",
            "   - Visit: https://adoption.microsoft.com/copilot/frontier-program/",
            "   - Contact your Microsoft account team for enrollment",
            "",
            "2. ADMIN CENTER ACCESS: Verify Agent Registry is visible in Microsoft 365 Admin Center",
            "   - Navigate to: https://admin.microsoft.com",
            "   - Look for 'Agent Registry' under Settings > Agents",
            "",
            "3. ENTRA ID PERMISSIONS: Verify required Graph API permissions are granted",
            "   - Required: AgentInstance.ReadWrite.All",
            "   - Required: AgentInstance.ReadWrite.ManagedBy (for app-only flows)",
            "   - Admin consent may be required",
            "",
            "4. ROLE ASSIGNMENT: Verify the Agent Registry Administrator role is assigned",
            "   - Navigate to Entra ID > Roles and administrators",
            "   - Assign 'Agent Registry Administrator' to the service principal",
            "",
            "5. CONDITIONAL ACCESS: Verify no policies are blocking API access",
            "   - Check for location-based restrictions",
            "   - Check for device compliance requirements",
            "",
            "6. TENANT CONFIGURATION: Contact Microsoft support if all above are verified",
            "   - Agent 365 may require additional tenant-level configuration",
            "   - Preview features may have limited regional availability"
        ]


class EntraAgentRegistryClient:
    """
    Client for Microsoft Entra Agent Registry operations.
    
    Handles agent instance and agent card registration with the registry.
    
    Reference: https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/publish-agents-to-registry
    """
    
    GRAPH_API_BASE = "https://graph.microsoft.com/beta"
    
    def __init__(self):
        self.credential = DefaultAzureCredential()
        self._token_cache = None
    
    async def _get_token(self) -> str:
        """Get Graph API access token."""
        token = self.credential.get_token("https://graph.microsoft.com/.default")
        return token.token
    
    async def register_agent_instance(
        self,
        agent_id: str,
        display_name: str,
        description: str,
        url: str,
        originating_store: str = "AzureAIFoundry",
        owner_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Register an agent instance in the Entra Agent Registry.
        
        Args:
            agent_id: Unique identifier for the agent
            display_name: Human-readable agent name
            description: Agent description
            url: Agent endpoint URL
            originating_store: Source platform (e.g., AzureAIFoundry, Custom)
            owner_id: Owner user ID (optional)
        
        Returns:
            Created agent instance data
        
        Reference: https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/publish-agents-to-registry#register-an-agent-instance
        """
        token = await self._get_token()
        
        payload = {
            "id": agent_id,
            "displayName": display_name,
            "description": description,
            "url": url,
            "isBlocked": False,
            "originatingStore": originating_store,
            "sourceAgentId": agent_id
        }
        
        if owner_id:
            payload["ownerId"] = owner_id
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            url = f"{self.GRAPH_API_BASE}/agentRegistry/agentInstances"
            
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 201:
                    return await response.json()
                elif response.status == 409:
                    logger.warning(f"Agent {agent_id} already registered, updating...")
                    return await self.update_agent_instance(agent_id, display_name, description, url)
                else:
                    body = await response.text()
                    raise Exception(f"Failed to register agent: {response.status} - {body}")
    
    async def update_agent_instance(
        self,
        agent_id: str,
        display_name: str,
        description: str,
        url: str
    ) -> Dict[str, Any]:
        """Update an existing agent instance."""
        token = await self._get_token()
        
        payload = {
            "displayName": display_name,
            "description": description,
            "url": url
        }
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            api_url = f"{self.GRAPH_API_BASE}/agentRegistry/agentInstances/{agent_id}"
            
            async with session.patch(api_url, headers=headers, json=payload) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    body = await response.text()
                    raise Exception(f"Failed to update agent: {response.status} - {body}")
    
    async def register_agent_card(
        self,
        agent_instance_id: str,
        name: str,
        description: str,
        skills: List[Dict[str, Any]],
        capabilities: Optional[Dict[str, bool]] = None
    ) -> Dict[str, Any]:
        """
        Register an agent card manifest for discovery.
        
        Reference: https://learn.microsoft.com/en-us/entra/agent-id/identity-platform/publish-agents-to-registry#register-agent-card
        """
        token = await self._get_token()
        
        payload = {
            "name": name,
            "description": description,
            "skills": skills,
            "capabilities": capabilities or {
                "supportsA2A": True,
                "supportsMCP": True
            }
        }
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            url = f"{self.GRAPH_API_BASE}/agentRegistry/agentInstances/{agent_instance_id}/agentCardManifest"
            
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status in [200, 201]:
                    return await response.json()
                else:
                    body = await response.text()
                    raise Exception(f"Failed to register agent card: {response.status} - {body}")
    
    async def get_agent_instance(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve an agent instance by ID."""
        token = await self._get_token()
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            url = f"{self.GRAPH_API_BASE}/agentRegistry/agentInstances/{agent_id}"
            
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 404:
                    return None
                else:
                    body = await response.text()
                    raise Exception(f"Failed to get agent: {response.status} - {body}")


class TeamsApprovalClient:
    """
    Microsoft Teams approval client using Graph API.
    
    Handles creation and management of approval requests in Teams.
    
    Reference: https://learn.microsoft.com/en-us/graph/approvals-app-api
    """
    
    GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
    
    def __init__(self):
        self.credential = DefaultAzureCredential()
    
    async def _get_token(self) -> str:
        """Get Graph API access token."""
        token = self.credential.get_token("https://graph.microsoft.com/.default")
        return token.token
    
    async def create_approval_request(
        self,
        approval_contract: ApprovalContract,
        approvers: List[str],
        callback_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create an approval request in Microsoft Teams.
        
        Args:
            approval_contract: The approval contract with request details
            approvers: List of approver user IDs or email addresses
            callback_url: Webhook URL for approval response (for Logic Apps integration)
        
        Returns:
            Created approval request data
        """
        token = await self._get_token()
        
        # Build approval request payload
        payload = {
            "displayName": f"Agents Deployment Approval - {approval_contract.environment}",
            "description": approval_contract.task,
            "approvalType": "basic",
            "assignedTo": [
                {"user": {"id": approver}} for approver in approvers
            ],
            "customData": json.dumps(approval_contract.to_dict())
        }
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            # TODO: Graph API Approvals endpoint requires specific permissions
            # and may need Power Automate integration for full Teams experience
            url = f"{self.GRAPH_API_BASE}/solutions/approval/approvalItems"
            
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status in [200, 201]:
                    return await response.json()
                else:
                    body = await response.text()
                    logger.warning(f"Graph Approvals API not available: {response.status}")
                    # Fallback to Logic Apps webhook
                    if callback_url:
                        return await self._trigger_logic_app_approval(
                            approval_contract, approvers, callback_url
                        )
                    raise Exception(f"Failed to create approval: {response.status} - {body}")
    
    async def _trigger_logic_app_approval(
        self,
        approval_contract: ApprovalContract,
        approvers: List[str],
        webhook_url: str
    ) -> Dict[str, Any]:
        """
        Trigger approval workflow via Azure Logic Apps.
        
        This is the fallback when direct Graph API is not available.
        """
        payload = {
            **approval_contract.to_dict(),
            "approvers": approvers,
            "callback_url": webhook_url
        }
        
        async with aiohttp.ClientSession() as session:
            headers = {"Content-Type": "application/json"}
            
            async with session.post(webhook_url, headers=headers, json=payload) as response:
                if response.status in [200, 202]:
                    return {"status": "triggered", "approval_id": approval_contract.approval_id}
                else:
                    body = await response.text()
                    raise Exception(f"Failed to trigger Logic App: {response.status} - {body}")
    
    async def get_approval_status(self, approval_id: str) -> Optional[Dict[str, Any]]:
        """Get the status of an approval request."""
        token = await self._get_token()
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            url = f"{self.GRAPH_API_BASE}/solutions/approval/approvalItems/{approval_id}"
            
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 404:
                    return None
                else:
                    body = await response.text()
                    raise Exception(f"Failed to get approval: {response.status} - {body}")


class ApprovalWorkflowEngine:
    """
    Agent Approval Workflow Engine.
    
    Orchestrates the dual-track approval process:
    1. Teams human approval - Decision surface for humans
    2. Agent validation - Validates completeness, records outcome, enforces continuation
    
    CRITICAL: Approval is NOT complete unless:
    - A human approves or rejects in Teams
    - AND the agent workflow validates, records, and returns the decision
    """
    
    # Agents task pattern that requires approval
    CICD_TASK_PATTERN = "Set up a Agents pipeline for deploying microservices to Kubernetes"
    
    def __init__(
        self,
        cosmos_endpoint: Optional[str] = None,
        cosmos_database: str = "mcpdb",
        cosmos_container: str = "approvals",
        logic_app_webhook_url: Optional[str] = None
    ):
        self.cosmos_endpoint = cosmos_endpoint or os.getenv("COSMOSDB_ENDPOINT", "")
        self.cosmos_database = cosmos_database
        self.cosmos_container = cosmos_container
        self.logic_app_webhook_url = logic_app_webhook_url or os.getenv("LOGIC_APP_APPROVAL_WEBHOOK", "")
        
        # Initialize clients
        self.availability_checker = Agent365AvailabilityChecker()
        self.registry_client = EntraAgentRegistryClient()
        self.teams_client = TeamsApprovalClient()
        
        # Initialize CosmosDB
        self._cosmos_client = None
        self._cosmos_container_client = None
        
        # Pending approvals cache
        self._pending_approvals: Dict[str, ApprovalContract] = {}
        
        # Callback handlers
        self._approval_callbacks: Dict[str, Callable[[ApprovalContract], Awaitable[None]]] = {}
    
    async def _init_cosmos(self):
        """Initialize CosmosDB client."""
        if not self._cosmos_client and self.cosmos_endpoint:
            try:
                credential = DefaultAzureCredential()
                self._cosmos_client = CosmosClient(self.cosmos_endpoint, credential=credential)
                database = self._cosmos_client.get_database_client(self.cosmos_database)
                self._cosmos_container_client = database.get_container_client(self.cosmos_container)
                logger.info("CosmosDB initialized for approval workflow")
            except Exception as e:
                logger.error(f"Failed to initialize CosmosDB: {e}")
    
    def requires_approval(self, task: str) -> bool:
        """
        Check if a task requires approval.
        
        Currently, only Agents pipeline tasks (task 2) require approval.
        """
        return self.CICD_TASK_PATTERN.lower() in task.lower() or "ci/cd" in task.lower()
    
    async def initiate_approval(
        self,
        task: str,
        requested_by: str,
        environment: str,
        cluster: str,
        namespace: str = "default",
        image_tags: Optional[List[str]] = None,
        commit_sha: Optional[str] = None,
        pipeline_url: Optional[str] = None,
        rollback_url: Optional[str] = None,
        approvers: Optional[List[str]] = None,
        on_complete: Optional[Callable[[ApprovalContract], Awaitable[None]]] = None
    ) -> ApprovalContract:
        """
        Initiate the approval workflow for a Agents deployment.
        
        This method:
        1. Creates an approval contract
        2. Stores it in CosmosDB for audit
        3. Sends approval request to Teams
        4. Registers a callback for completion
        
        Args:
            task: The task description
            requested_by: User/service requesting the deployment
            environment: Target environment (development, staging, production)
            cluster: Target Kubernetes cluster
            namespace: Target namespace
            image_tags: Container image tags to deploy
            commit_sha: Git commit SHA
            pipeline_url: URL to the pipeline run
            rollback_url: URL for rollback action
            approvers: List of approver user IDs
            on_complete: Async callback when approval completes
        
        Returns:
            ApprovalContract with pending status
        """
        await self._init_cosmos()
        
        # Generate approval ID
        approval_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat() + "Z"
        
        # Create approval contract
        contract = ApprovalContract(
            approval_id=approval_id,
            requested_by=requested_by,
            task=task,
            environment=environment,
            decision=ApprovalDecision.PENDING.value,
            agent_validation=AgentValidationStatus.PENDING.value,
            cluster=cluster,
            namespace=namespace,
            image_tags=image_tags or [],
            commit_sha=commit_sha,
            pipeline_url=pipeline_url,
            rollback_url=rollback_url,
            request_timestamp=timestamp
        )
        
        # Store in pending approvals
        self._pending_approvals[approval_id] = contract
        
        # Register callback
        if on_complete:
            self._approval_callbacks[approval_id] = on_complete
        
        # Store in CosmosDB for audit trail
        if self._cosmos_container_client:
            try:
                doc = {
                    "id": approval_id,
                    "partitionKey": environment,
                    **contract.to_dict(),
                    "status": "pending",
                    "created_at": timestamp
                }
                self._cosmos_container_client.upsert_item(doc)
                logger.info(f"Approval request {approval_id} stored in CosmosDB")
            except Exception as e:
                logger.error(f"Failed to store approval in CosmosDB: {e}")
        
        # Send to Teams
        try:
            if self.logic_app_webhook_url:
                # Use Logic Apps for Teams integration
                await self.teams_client._trigger_logic_app_approval(
                    contract,
                    approvers or [],
                    self.logic_app_webhook_url
                )
                logger.info(f"Approval request {approval_id} sent to Logic Apps")
            else:
                # Try direct Graph API
                await self.teams_client.create_approval_request(
                    contract,
                    approvers or []
                )
                logger.info(f"Approval request {approval_id} sent to Teams")
        except Exception as e:
            logger.error(f"Failed to send approval to Teams: {e}")
            contract.agent_validation = AgentValidationStatus.FAILED.value
        
        return contract
    
    async def process_approval_response(
        self,
        approval_id: str,
        decision: str,
        approved_by: str,
        comment: Optional[str] = None
    ) -> ApprovalContract:
        """
        Process an approval response from Teams.
        
        This method:
        1. Receives the human decision
        2. Validates the decision schema
        3. Records the outcome
        4. Updates the agent validation status
        5. Triggers the completion callback
        
        Args:
            approval_id: The approval ID
            decision: "approved" or "rejected"
            approved_by: The approver's identity
            comment: Optional comment from the approver
        
        Returns:
            Updated ApprovalContract with final status
        """
        await self._init_cosmos()
        
        # Get pending approval
        contract = self._pending_approvals.get(approval_id)
        if not contract:
            # Try to load from CosmosDB
            if self._cosmos_container_client:
                try:
                    items = list(self._cosmos_container_client.query_items(
                        query=f"SELECT * FROM c WHERE c.id = '{approval_id}'",
                        enable_cross_partition_query=True
                    ))
                    if items:
                        contract = ApprovalContract.from_dict(items[0])
                except Exception as e:
                    logger.error(f"Failed to load approval from CosmosDB: {e}")
        
        if not contract:
            raise ValueError(f"Approval {approval_id} not found")
        
        # Validate decision schema
        if decision not in [ApprovalDecision.APPROVED.value, ApprovalDecision.REJECTED.value]:
            raise ValueError(f"Invalid decision: {decision}")
        
        # Calculate resolution time
        request_time = datetime.fromisoformat(contract.request_timestamp.rstrip("Z"))
        resolution_time = (datetime.utcnow() - request_time).total_seconds()
        
        # Update contract
        contract.decision = decision
        contract.approved_by = approved_by
        contract.timestamp = datetime.utcnow().isoformat() + "Z"
        contract.comment = comment
        contract.resolution_time_seconds = resolution_time
        
        # Agent validation - verify decision completeness
        validation_passed = self._validate_approval_decision(contract)
        contract.agent_validation = (
            AgentValidationStatus.PASSED.value if validation_passed
            else AgentValidationStatus.FAILED.value
        )
        
        # Update in CosmosDB
        if self._cosmos_container_client:
            try:
                doc = {
                    "id": approval_id,
                    "partitionKey": contract.environment,
                    **contract.to_dict(),
                    "status": "completed",
                    "completed_at": datetime.utcnow().isoformat() + "Z"
                }
                self._cosmos_container_client.upsert_item(doc)
                logger.info(f"Approval {approval_id} completed and stored")
            except Exception as e:
                logger.error(f"Failed to update approval in CosmosDB: {e}")
        
        # Remove from pending
        self._pending_approvals.pop(approval_id, None)
        
        # Trigger callback
        callback = self._approval_callbacks.pop(approval_id, None)
        if callback:
            try:
                await callback(contract)
            except Exception as e:
                logger.error(f"Approval callback failed: {e}")
        
        return contract
    
    def _validate_approval_decision(self, contract: ApprovalContract) -> bool:
        """
        Validate the approval decision meets all requirements.
        
        Validation checks:
        1. Decision is present and valid
        2. Approver identity is recorded
        3. Timestamp is present
        4. Required fields are complete
        5. Rejection has a comment (optional enforcement)
        
        Returns:
            True if validation passes, False otherwise
        """
        # Required fields
        if not contract.decision or contract.decision == ApprovalDecision.PENDING.value:
            logger.warning("Validation failed: decision not set")
            return False
        
        if not contract.approved_by:
            logger.warning("Validation failed: approver not recorded")
            return False
        
        if not contract.timestamp:
            logger.warning("Validation failed: timestamp not set")
            return False
        
        # Optional: require comment for rejections
        # if contract.decision == ApprovalDecision.REJECTED.value and not contract.comment:
        #     logger.warning("Validation failed: rejection requires comment")
        #     return False
        
        logger.info(f"Approval {contract.approval_id} validation passed")
        return True
    
    async def wait_for_approval(
        self,
        approval_id: str,
        timeout_seconds: int = 7200  # 2 hours default
    ) -> ApprovalContract:
        """
        Wait for an approval to complete (blocking).
        
        This method polls for approval completion with exponential backoff.
        
        Args:
            approval_id: The approval ID to wait for
            timeout_seconds: Maximum time to wait
        
        Returns:
            Completed ApprovalContract
        
        Raises:
            TimeoutError: If approval times out
        """
        start_time = datetime.utcnow()
        poll_interval = 5  # Start with 5 seconds
        max_poll_interval = 60  # Max 1 minute between polls
        
        while True:
            # Check timeout
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed > timeout_seconds:
                # Handle timeout
                contract = self._pending_approvals.get(approval_id)
                if contract:
                    contract.decision = ApprovalDecision.TIMEOUT.value
                    contract.agent_validation = AgentValidationStatus.FAILED.value
                    contract.timestamp = datetime.utcnow().isoformat() + "Z"
                    await self.process_approval_response(
                        approval_id,
                        ApprovalDecision.TIMEOUT.value,
                        "system",
                        "Approval timed out"
                    )
                raise TimeoutError(f"Approval {approval_id} timed out after {timeout_seconds}s")
            
            # Check if completed
            contract = self._pending_approvals.get(approval_id)
            if contract and contract.is_complete():
                return contract
            
            # Poll CosmosDB for external updates
            if self._cosmos_container_client:
                try:
                    items = list(self._cosmos_container_client.query_items(
                        query=f"SELECT * FROM c WHERE c.id = '{approval_id}' AND c.status = 'completed'",
                        enable_cross_partition_query=True
                    ))
                    if items:
                        return ApprovalContract.from_dict(items[0])
                except Exception as e:
                    logger.warning(f"Failed to poll CosmosDB: {e}")
            
            # Wait with exponential backoff
            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, max_poll_interval)


# Singleton instance for import
_workflow_engine: Optional[ApprovalWorkflowEngine] = None


def get_approval_workflow_engine() -> ApprovalWorkflowEngine:
    """Get or create the approval workflow engine singleton."""
    global _workflow_engine
    if _workflow_engine is None:
        _workflow_engine = ApprovalWorkflowEngine()
    return _workflow_engine


# Convenience function for Agents approval checkpoint
async def require_agents_approval(
    task: str,
    requested_by: str,
    environment: str,
    cluster: str,
    **kwargs
) -> ApprovalContract:
    """
    Require Agents approval before proceeding.
    
    This is the main entry point for the approval checkpoint in mcp_agents.py.
    It blocks execution until approval is received.
    
    Args:
        task: Task description
        requested_by: Requester identity
        environment: Target environment
        cluster: Target cluster
        **kwargs: Additional deployment parameters
    
    Returns:
        ApprovalContract with final decision
    
    Raises:
        ValueError: If approval is rejected
        TimeoutError: If approval times out
    """
    engine = get_approval_workflow_engine()
    
    # Check if approval is required
    if not engine.requires_approval(task):
        # Return auto-approved contract for non-Agents tasks
        return ApprovalContract(
            approval_id=str(uuid.uuid4()),
            requested_by=requested_by,
            task=task,
            environment=environment,
            decision=ApprovalDecision.APPROVED.value,
            approved_by="system",
            timestamp=datetime.utcnow().isoformat() + "Z",
            agent_validation=AgentValidationStatus.PASSED.value,
            cluster=cluster
        )
    
    # Initiate approval
    contract = await engine.initiate_approval(
        task=task,
        requested_by=requested_by,
        environment=environment,
        cluster=cluster,
        **kwargs
    )
    
    logger.info(f"Agents approval initiated: {contract.approval_id}")
    logger.info(f"Waiting for human approval in Microsoft Teams...")
    
    # Wait for approval
    try:
        completed = await engine.wait_for_approval(contract.approval_id)
        
        if completed.decision == ApprovalDecision.REJECTED.value:
            raise ValueError(
                f"Agents deployment rejected by {completed.approved_by}: {completed.comment or 'No reason provided'}"
            )
        
        if completed.agent_validation != AgentValidationStatus.PASSED.value:
            raise ValueError(
                f"Agent validation failed for approval {completed.approval_id}"
            )
        
        logger.info(f"Agents approval granted by {completed.approved_by}")
        return completed
        
    except TimeoutError:
        logger.error(f"Agents approval timed out for {contract.approval_id}")
        raise


# Export public API
__all__ = [
    "ApprovalContract",
    "ApprovalDecision",
    "AgentValidationStatus",
    "Agent365AvailabilityChecker",
    "Agent365AvailabilityResult",
    "EntraAgentRegistryClient",
    "TeamsApprovalClient",
    "ApprovalWorkflowEngine",
    "get_approval_workflow_engine",
    "require_agents_approval",
]
