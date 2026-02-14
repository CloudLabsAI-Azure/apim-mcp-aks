# Exercise 2: Build Agents using GitHub Copilot and SpecKit

**Duration:** 1 hour

## Overview

In this exercise, you will use GitHub Copilot and the SpecKit to specify, create, unit test, and deploy an **Autonomous Agent** that operates independently without human intervention.

---

## Part A: Review the Project Constitution

Before creating agents, you should understand the SDLC framework defined in the constitution.

### Step A.1: Open the Constitution

In VS Code Explorer, navigate to and open `.speckit/constitution.md`.

### Step A.2: Identify Key Elements

As you review, document at a high-level the key take-home points:

| Element | Your Findings |
|---------|---------------|
| Project Overview | |
| Core Principles | |
| Development Standards | |
| Technical Architecture | |
| Success Criteria | |
| Constraints & Assumptions | |
| Governance | |

### Step A.3: Review Example Specifications

In VS Code Explorer, navigate to and open the following files:

- `.speckit/specifications/healthcare_digital_quality_agent.spec.md`
- `.speckit/specifications/customer_churn_agent.spec.md`
- `.speckit/specifications/devops_cicd_pipeline_agent.spec.md`
- `.speckit/specifications/user_security_agent.spec.mdd`

The first document: - `.speckit/specifications/healthcare_digital_quality_agent.spec.md` is what was used to build the existing next best action agent. 

what did you make of the specifications? What aspects of specification will you re-use for your use-case?
---

## Part B: Build Autonomous Agent

### Step B.1: Create Agent Specification with GitHub Copilot.

Brainstorm what you want your agent to be / done. 
For the purposes of this exercise, there is no user interface for the agent. 
The agent is a python script that can accept an english based description of a task. 
It can then determine the intent of the task, reason and plan out steps to be done and then execute on them. Keep in mind, this agent of yours needs to be given boundaries an identity and RBAC permissions to resources / tools for it to perform its job. 

You'll need to invent or select an existing use case:

- `.speckit/specifications/customer_churn_agent.spec.md`
- `.speckit/specifications/devops_cicd_pipeline_agent.spec.md`
- `.speckit/specifications/user_security_agent.spec.mdd`

Now please move onto the next step of using GitHub Copilot and Claude Opus 4.6 to help you review, write, adjust your specification and then build it. 

```powershell

In VS Code Explorer, navigate to and open `.speckit/specifications`.

**Prompt Copilot with:**
Start from scratch:
> "Create an specification for an autonomous <responsbility_of_your_agent> agent that can <do x,y,z> without human intervention. Follow the SpecKit template format. Utilize the .speckit/specifications as an example.

The specification should include:
- Overview (Spec ID, Version, Domain, Governance Model: Autonomous)
- Business Framing
- MCP Tool Catalog
- Workflow Specification
- Success Metrics
- Security Requirements"

If you want to alter an existing specification:
**Prompt Copilot with:**
> "Make a copy of the .speckit/specifications/<>.spec.md. Make the following changes to it: x,y,z."

### Step B.2: Implement Autonomous Agent, Unit Test(s) and Functional Test(s) with Copilot

For reference, review the existing `src/next_best_action_agent.py`, `src/next_best_action_agent_unit.py`, `src/next_best_action_agent_functional.py` — this is the implementation built from the `healthcare_digital_quality_agent.spec.md` specification. This reference implementation should be used as a pattern for your own agent.

To create the agent,
**Prompt Copilot with:**
> "Implement an MCP-compliant FastAPI agent based on the <autonomous_agent.spec.md> specification. Utilize `src/next_best_action_agent.py` as a reference implementation. Build the implementation similar to the reference implementation but in its own new file `src/autonomous_agent.py. Be sure to include health endpoint, SSE endpoint, and message endpoint with tools/list and tools/call handlers. Also create pytest unit tests in `tests/test_autonomous_agent_unit.py` covering the health endpoint, MCP initialize, tools/list, and tools/call methods. Create functional tests in `tests/test_autonomous_agent_functional.py` covering the health endpoint, MCP initialize, tools/list, and tools/call methods. Make a new DockerFile specific to this new agent. Make a new k8s/autonomous-agent-deployment.yaml config file too."

### Step B.3: Generate Domain Knowledge Facts with Copilot

Your agent needs domain-specific facts to ground its reasoning. Copilot will generate ontology fact files for your agents domain and upload them.

**Prompt Copilot (Agent Mode) with:**
> "Review the existing ontology fact files in `facts/ontology/` (customer_churn_ontology.json, cicd_pipeline_ontology.json, user_management_ontology.json) as examples. Generate a new ontology fact file for my agents domain based on its specification. Save it to `facts/ontology/<my_domain>_ontology.json` following the same JSON structure. Then upload all ontology files from `facts/ontology/` to the Azure Storage accounts ontologies container by creating and running `scripts/upload-ontologies-to-storage.ps1`."

### Step B.4: Ingest Domain Knowledge into AI Search with Copilot

Your agent needs task instruction documents indexed in Azure AI Search for long-term memory retrieval. Copilot will create the task instruction files, ingest them with embeddings, and provision the agentic retrieval infrastructure.

**Prompt Copilot (Agent Mode) with:**
> "Review the existing task instruction documents in `task_instructions/` as examples. Create a new task instruction JSON file for my agents domain based on its specification, following the same structure (id, title, category, intent, description, content with step-by-step instructions, keywords, estimated_effort, steps array, related_tasks). Save it to `task_instructions/<my_domain>.json`. Then activate the .venv and run `python scripts/ingest_task_instructions.py` to create the AI Search index, generate embeddings with text-embedding-3-large, upload the documents, and provision a Knowledge Source and Knowledge Base for agentic retrieval. Verify the index has documents and the Knowledge Source `task-instructions-source` and Knowledge Base `task-instructions-kb` exist on the search service."

### Step B.5: Enable Agentic Retrieval in K8s Deployment with Copilot

Your agents K8s deployment needs the AI Search environment variables to enable agentic retrieval at runtime.

**Prompt Copilot (Agent Mode) with:**
> "Update my agents K8s deployment YAML (`k8s/autonomous-agent-deployment.yaml` or the configured variant) to add the following environment variables to the container spec: AZURE_SEARCH_ENDPOINT (set to the search service endpoint), AZURE_SEARCH_INDEX_NAME (set to `task-instructions`), and AZURE_SEARCH_KNOWLEDGE_BASE_NAME (set to `task-instructions-kb`). The AZURE_SEARCH_KNOWLEDGE_BASE_NAME is critical — it enables the AzureAISearchContextProvider to run in agentic mode using KnowledgeBaseRetrievalClient for multi-hop reasoning instead of basic hybrid search. Apply the updated deployment to AKS and verify the pods pick up the new environment variables."

After Copilot completes the above steps, your agent will have the following agentic retrieval architecture:

| Component | Purpose |
|---|---|
| `src/memory/aisearch_memory.py` | `LongTermMemory` class wrapping `AzureAISearchContextProvider` |
| `AzureAISearchContextProvider` | Microsoft Agent Framework provider with `mode="agentic"` |
| Knowledge Base (`task-instructions-kb`) | Server-side LLM-driven query planning, sub-query decomposition, answer synthesis |
| Knowledge Source (`task-instructions-source`) | Points to the `task-instructions` search index |
| `AZURE_SEARCH_KNOWLEDGE_BASE_NAME` env var | Tells the agent to use `KnowledgeBaseRetrievalClient.retrieve()` instead of basic `SearchClient.search()` |

### Step B.6: Run Unit Tests with Copilot

**Prompt Copilot (Agent Mode) with:**
> "Check if a .venv virtual environment exists in the project root. If it doesn't, create one. Activate it, install the dependencies from src/requirements.txt, and run the unit tests in tests/test_autonomous_agent_unit.py with verbose output."

### Step B.7: Deploy Autonomous Agent with Copilot

**Prompt Copilot (Agent Mode) with:**
> "Build the new Docker image for this agent in the src/ directory for my autonomous agent, tag it and push it to the Azure Container Registry using the CONTAINER_REGISTRY environment variable, then deploy it to AKS using k8s/autonomous-agent-deployment.yaml and verify the pods are running in the mcp-agents namespace."

### Step B.8: Run Functional Tests with Copilot

**Prompt Copilot (Agent Mode) with:**
> "Set up a kubectl port-forward from the autonomous-agent service in the mcp-agents namespace on port 8080:80. Then check if a .venv virtual environment exists in the project root — if it doesn't, create one. Activate it, install the dependencies from src/requirements.txt, and run the functional tests in tests/test_autonomous_agent_functional.py with verbose output."

---

## Completion Checklist

Before proceeding to Exercise 3, please confirm the following:

- [ ] Specification created/updated
- [ ] Agent created with MCP endpoints
- [ ] Task instructions ingested into AI Search index
- [ ] Knowledge Source and Knowledge Base provisioned (agentic retrieval)
- [ ] Ontology facts uploaded to Azure Storage/Fabric IQ
- [ ] Unit tests passing
- [ ] Docker image built and pushed to ACR
- [ ] Agent deployed to AKS
- [ ] Functional tests passing

---

## Summary

Congratulations!, You have built an autonomous agent using GitHub Copilot and SpecKit.

The agent implements MC protocol and is integrated with the Azure Agents Control Plane for security, governance, adn observability.

---


**Next:** [Exercise 3: Review Agents Control Plane](exercise_03_review_agents_control_plane.md)
