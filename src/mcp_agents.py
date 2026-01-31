"""
FastAPI MCP Agents
Implements Model Context Protocol (MCP) with SSE support
Enhanced with Microsoft Agent Framework for AI agent capabilities
Integrated with CosmosDB for task and plan storage with semantic reasoning
Features Memory Provider abstraction for short-term (CosmosDB) and long-term (AI Search) memory
"""

import json
import logging
import asyncio
import uuid
import numpy as np
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential
from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
import os

# Microsoft Agent Framework imports
from agent_framework import ai_function, AIFunction
from agent_framework.azure import AzureAIAgentClient

# Memory Provider imports
from memory import CosmosDBShortTermMemory, MemoryEntry, MemoryType, CompositeMemory

from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="MCP Server",
    description="Model Context Protocol Server for AI Agents with Semantic Reasoning",
    version="1.0.0"
)

# Azure Storage configuration
STORAGE_ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")

# CosmosDB configuration
COSMOSDB_ENDPOINT = os.getenv("COSMOSDB_ENDPOINT", "")
COSMOSDB_DATABASE_NAME = os.getenv("COSMOSDB_DATABASE_NAME", "mcpdb")
COSMOSDB_TASKS_CONTAINER = "tasks"
COSMOSDB_PLANS_CONTAINER = "plans"

# Initialize storage client
if STORAGE_CONNECTION_STRING:
    blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
elif STORAGE_ACCOUNT_URL:
    credential = DefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url=STORAGE_ACCOUNT_URL, credential=credential)
else:
    logger.warning("No storage configuration found - snippet storage will not work")
    blob_service_client = None

# Initialize CosmosDB client
cosmos_client = None
cosmos_database = None
cosmos_tasks_container = None
cosmos_plans_container = None

if COSMOSDB_ENDPOINT:
    try:
        credential = DefaultAzureCredential()
        cosmos_client = CosmosClient(COSMOSDB_ENDPOINT, credential=credential)
        cosmos_database = cosmos_client.get_database_client(COSMOSDB_DATABASE_NAME)
        cosmos_tasks_container = cosmos_database.get_container_client(COSMOSDB_TASKS_CONTAINER)
        cosmos_plans_container = cosmos_database.get_container_client(COSMOSDB_PLANS_CONTAINER)
        logger.info("CosmosDB client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize CosmosDB client: {e}")
else:
    logger.warning("COSMOSDB_ENDPOINT not configured - task storage will not work")

# Initialize Memory Providers
short_term_memory: Optional[CosmosDBShortTermMemory] = None
composite_memory: Optional[CompositeMemory] = None

if COSMOSDB_ENDPOINT:
    try:
        short_term_memory = CosmosDBShortTermMemory(
            endpoint=COSMOSDB_ENDPOINT,
            database_name=COSMOSDB_DATABASE_NAME,
            container_name="short_term_memory",
            default_ttl=3600,  # 1 hour default TTL
        )
        
        # Create composite memory (long-term will be added later with AI Search)
        composite_memory = CompositeMemory(
            short_term=short_term_memory,
            long_term=None,  # Will be AI Search / FoundryIQ
        )
        
        logger.info("Memory providers initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize memory providers: {e}")
else:
    logger.warning("COSMOSDB_ENDPOINT not configured - memory providers will not work")

SNIPPETS_CONTAINER = "snippets"

# In-memory session storage (replace with Redis for production)
sessions: Dict[str, Dict[str, Any]] = {}

# Microsoft Agent Framework configuration
FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
FOUNDRY_MODEL_DEPLOYMENT_NAME = os.getenv("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-5.2-chat")
EMBEDDING_MODEL_DEPLOYMENT_NAME = os.getenv("EMBEDDING_MODEL_DEPLOYMENT_NAME", "text-embedding-3-large")


# =========================================
# Embedding and Semantic Reasoning Helpers
# =========================================

def get_embedding(text: str) -> List[float]:
    """
    Generate embeddings for text using Azure AI Foundry's text-embedding-3-large model.
    
    Args:
        text: The text to generate embeddings for
    
    Returns:
        A list of floats representing the embedding vector (3072 dimensions)
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        raise ValueError("Foundry endpoint not configured")
    
    from openai import AzureOpenAI
    
    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    
    base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
    
    client = AzureOpenAI(
        azure_endpoint=base_endpoint,
        api_key=token.token,
        api_version="2024-02-15-preview"
    )
    
    response = client.embeddings.create(
        model=EMBEDDING_MODEL_DEPLOYMENT_NAME,
        input=text
    )
    
    return response.data[0].embedding


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Calculate cosine similarity between two vectors.
    
    Args:
        vec1: First embedding vector
        vec2: Second embedding vector
    
    Returns:
        Cosine similarity score between -1 and 1
    """
    arr1 = np.array(vec1)
    arr2 = np.array(vec2)
    
    dot_product = np.dot(arr1, arr2)
    norm1 = np.linalg.norm(arr1)
    norm2 = np.linalg.norm(arr2)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return float(dot_product / (norm1 * norm2))


def find_similar_tasks(task_embedding: List[float], threshold: float = 0.7, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Find similar tasks in CosmosDB using cosine similarity.
    
    Args:
        task_embedding: The embedding vector of the current task
        threshold: Minimum similarity score (0-1)
        limit: Maximum number of similar tasks to return
    
    Returns:
        List of similar tasks with their similarity scores
    """
    if not cosmos_tasks_container:
        return []
    
    try:
        # Query all tasks with embeddings
        query = "SELECT c.id, c.task, c.intent, c.embedding, c.created_at FROM c WHERE IS_DEFINED(c.embedding)"
        items = list(cosmos_tasks_container.query_items(query=query, enable_cross_partition_query=True))
        
        similar_tasks = []
        for item in items:
            if 'embedding' in item and item['embedding']:
                similarity = cosine_similarity(task_embedding, item['embedding'])
                if similarity >= threshold:
                    similar_tasks.append({
                        'id': item['id'],
                        'task': item.get('task', ''),
                        'intent': item.get('intent', ''),
                        'similarity': similarity,
                        'created_at': item.get('created_at', '')
                    })
        
        # Sort by similarity descending and limit results
        similar_tasks.sort(key=lambda x: x['similarity'], reverse=True)
        return similar_tasks[:limit]
    
    except Exception as e:
        logger.error(f"Error finding similar tasks: {e}")
        return []


def analyze_intent(task: str) -> str:
    """
    Use the LLM to analyze and categorize the intent of a task.
    
    Args:
        task: The task description in natural language
    
    Returns:
        A string describing the analyzed intent
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return "unknown"
    
    try:
        from openai import AzureOpenAI
        
        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        
        base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
        
        client = AzureOpenAI(
            azure_endpoint=base_endpoint,
            api_key=token.token,
            api_version="2024-02-15-preview"
        )
        
        response = client.chat.completions.create(
            model=FOUNDRY_MODEL_DEPLOYMENT_NAME,
            messages=[
                {
                    "role": "system",
                    "content": "You are a task analyzer. Analyze the given task and provide a brief categorization of its intent. Return only a short phrase describing the primary intent (e.g., 'data analysis', 'code generation', 'information retrieval', 'system configuration')."
                },
                {"role": "user", "content": f"Analyze this task: {task}"}
            ]
        )
        
        if response.choices and len(response.choices) > 0:
            return response.choices[0].message.content.strip()
        return "unknown"
    
    except Exception as e:
        logger.error(f"Error analyzing intent: {e}")
        return "unknown"


def generate_plan(task: str, similar_tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Generate a plan of steps to accomplish the task, optionally learning from similar past tasks.
    
    Args:
        task: The task description
        similar_tasks: List of similar tasks for context
    
    Returns:
        List of planned steps
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return [{"step": 1, "action": "Manual planning required", "description": "Foundry not configured"}]
    
    try:
        from openai import AzureOpenAI
        
        credential = DefaultAzureCredential()
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        
        base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
        
        client = AzureOpenAI(
            azure_endpoint=base_endpoint,
            api_key=token.token,
            api_version="2024-02-15-preview"
        )
        
        # Build context from similar tasks
        context = ""
        if similar_tasks:
            context = "\n\nSimilar past tasks for reference:\n"
            for st in similar_tasks[:3]:
                context += f"- {st['task']} (intent: {st['intent']}, similarity: {st['similarity']:.2f})\n"
        
        response = client.chat.completions.create(
            model=FOUNDRY_MODEL_DEPLOYMENT_NAME,
            messages=[
                {
                    "role": "system",
                    "content": """You are a task planner. Given a task, generate a structured plan with actionable steps.
Return a JSON array of steps, each with:
- "step": step number (integer)
- "action": brief action title
- "description": detailed description of what to do
- "estimated_effort": low/medium/high

Return ONLY valid JSON array, no markdown or explanation."""
                },
                {"role": "user", "content": f"Create a plan for this task: {task}{context}"}
            ]
        )
        
        if response.choices and len(response.choices) > 0:
            content = response.choices[0].message.content.strip()
            # Parse JSON from response
            try:
                # Handle potential markdown code blocks
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                return json.loads(content)
            except json.JSONDecodeError:
                return [{"step": 1, "action": "Execute task", "description": content, "estimated_effort": "medium"}]
        
        return [{"step": 1, "action": "Execute task", "description": task, "estimated_effort": "medium"}]
    
    except Exception as e:
        logger.error(f"Error generating plan: {e}")
        return [{"step": 1, "action": "Error", "description": str(e), "estimated_effort": "unknown"}]


# Define Agent Framework tools using @ai_function decorator
@ai_function
def hello_mcp_tool() -> str:
    """Hello world MCP tool that returns a greeting message."""
    return "Hello I am MCPTool!"


@ai_function
def get_snippet_tool(snippetname: str) -> str:
    """
    Retrieve a snippet by name from Azure Blob Storage.
    
    Args:
        snippetname: The name of the snippet to retrieve
    
    Returns:
        The content of the snippet
    """
    if not blob_service_client:
        return "Error: Storage not configured"
    
    try:
        blob_client = blob_service_client.get_blob_client(
            container=SNIPPETS_CONTAINER,
            blob=f"{snippetname}.json"
        )
        blob_data = blob_client.download_blob().readall()
        return blob_data.decode('utf-8')
    except Exception as e:
        logger.error(f"Error retrieving snippet: {e}")
        return f"Error retrieving snippet: {str(e)}"


@ai_function
def save_snippet_tool(snippetname: str, snippet: str) -> str:
    """
    Save a snippet with a name to Azure Blob Storage.
    
    Args:
        snippetname: The name of the snippet
        snippet: The content of the snippet
    
    Returns:
        Success or error message
    """
    if not blob_service_client:
        return "Error: Storage not configured"
    
    try:
        blob_client = blob_service_client.get_blob_client(
            container=SNIPPETS_CONTAINER,
            blob=f"{snippetname}.json"
        )
        blob_client.upload_blob(snippet.encode('utf-8'), overwrite=True)
        return f"Snippet '{snippetname}' saved successfully"
    except Exception as e:
        logger.error(f"Error saving snippet: {e}")
        return f"Error saving snippet: {str(e)}"


@ai_function
def ask_foundry_tool(question: str) -> str:
    """
    Ask a question and get an answer using the Azure AI Foundry model.
    
    Args:
        question: The question to ask the AI model
    
    Returns:
        The AI model's response to the question
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return "Error: Foundry endpoint not configured"
    
    try:
        from openai import AzureOpenAI
        
        credential = DefaultAzureCredential()
        # Get a token for Azure Cognitive Services
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        
        # Extract the base endpoint (remove /api/projects/proj-default if present)
        # Use the services.ai.azure.com endpoint directly
        base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
        
        client = AzureOpenAI(
            azure_endpoint=base_endpoint,
            api_key=token.token,
            api_version="2024-02-15-preview"
        )
        
        response = client.chat.completions.create(
            model=FOUNDRY_MODEL_DEPLOYMENT_NAME,
            messages=[{"role": "user", "content": question}]
        )
        
        if response.choices and len(response.choices) > 0:
            return response.choices[0].message.content
        return "No response generated"
    except Exception as e:
        logger.error(f"Error calling Foundry model: {e}")
        return f"Error calling Foundry model: {str(e)}"


@ai_function
def next_best_action_tool(task: str) -> str:
    """
    Analyze a task using semantic reasoning, generate embeddings, find similar past tasks,
    and create a plan of steps. Stores the task and plan in CosmosDB for future reference.
    
    Args:
        task: The task description in natural language (English sentence)
    
    Returns:
        A JSON response containing task analysis, similar tasks, and planned steps
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        return json.dumps({"error": "Foundry endpoint not configured"})
    
    if not cosmos_tasks_container or not cosmos_plans_container:
        return json.dumps({"error": "CosmosDB not configured"})
    
    try:
        task_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().isoformat()
        
        # Step 1: Generate embedding for the task
        logger.info(f"Generating embedding for task: {task[:100]}...")
        task_embedding = get_embedding(task)
        
        # Step 2: Analyze intent
        logger.info("Analyzing task intent...")
        intent = analyze_intent(task)
        
        # Step 3: Find similar tasks using cosine similarity
        logger.info("Searching for similar past tasks...")
        similar_tasks = find_similar_tasks(task_embedding, threshold=0.7, limit=5)
        
        # Step 4: Generate plan based on task and similar past tasks
        logger.info("Generating execution plan...")
        plan_steps = generate_plan(task, similar_tasks)
        
        # Step 5: Store task in CosmosDB
        task_doc = {
            'id': task_id,
            'task': task,
            'intent': intent,
            'embedding': task_embedding,
            'created_at': timestamp,
            'similar_task_count': len(similar_tasks)
        }
        cosmos_tasks_container.upsert_item(task_doc)
        logger.info(f"Task stored in CosmosDB with id: {task_id}")
        
        # Step 6: Store plan in CosmosDB
        plan_doc = {
            'id': str(uuid.uuid4()),
            'taskId': task_id,
            'task': task,
            'intent': intent,
            'steps': plan_steps,
            'similar_tasks_referenced': [{'id': st['id'], 'similarity': st['similarity']} for st in similar_tasks],
            'created_at': timestamp,
            'status': 'planned'
        }
        cosmos_plans_container.upsert_item(plan_doc)
        logger.info(f"Plan stored in CosmosDB for task: {task_id}")
        
        # Build response
        response = {
            'task_id': task_id,
            'task': task,
            'intent': intent,
            'analysis': {
                'similar_tasks_found': len(similar_tasks),
                'similar_tasks': [
                    {
                        'task': st['task'],
                        'intent': st['intent'],
                        'similarity_score': round(st['similarity'], 3)
                    }
                    for st in similar_tasks
                ]
            },
            'plan': {
                'steps': plan_steps,
                'total_steps': len(plan_steps)
            },
            'metadata': {
                'created_at': timestamp,
                'embedding_dimensions': len(task_embedding),
                'stored_in_cosmos': True
            }
        }
        
        return json.dumps(response, indent=2)
    
    except Exception as e:
        logger.error(f"Error in next_best_action: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def store_memory_tool(content: str, session_id: str, memory_type: str = "context") -> str:
    """
    Store information in short-term memory for later retrieval.
    
    Args:
        content: The content to remember
        session_id: The session ID to associate the memory with
        memory_type: Type of memory (context, conversation, task, plan)
    
    Returns:
        JSON response with the stored memory ID
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    try:
        import asyncio
        
        # Map string to MemoryType enum
        type_map = {
            "context": MemoryType.CONTEXT,
            "conversation": MemoryType.CONVERSATION,
            "task": MemoryType.TASK,
            "plan": MemoryType.PLAN,
        }
        mem_type = type_map.get(memory_type.lower(), MemoryType.CONTEXT)
        
        # Generate embedding for the content
        embedding = None
        if FOUNDRY_PROJECT_ENDPOINT:
            try:
                embedding = get_embedding(content)
            except Exception as e:
                logger.warning(f"Failed to generate embedding: {e}")
        
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            content=content,
            memory_type=mem_type,
            embedding=embedding,
            session_id=session_id,
        )
        
        # Run async store in sync context
        loop = asyncio.new_event_loop()
        entry_id = loop.run_until_complete(short_term_memory.store(entry))
        loop.close()
        
        return json.dumps({
            "success": True,
            "memory_id": entry_id,
            "session_id": session_id,
            "memory_type": memory_type,
            "has_embedding": embedding is not None,
        })
    
    except Exception as e:
        logger.error(f"Error storing memory: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def recall_memory_tool(query: str, session_id: str, limit: int = 5) -> str:
    """
    Recall relevant memories from short-term memory based on semantic similarity.
    
    Args:
        query: The query to search for relevant memories
        session_id: The session ID to search within
        limit: Maximum number of memories to return
    
    Returns:
        JSON response with relevant memories
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    if not FOUNDRY_PROJECT_ENDPOINT:
        return json.dumps({"error": "Foundry endpoint not configured for embeddings"})
    
    try:
        import asyncio
        
        # Generate embedding for the query
        query_embedding = get_embedding(query)
        
        # Search for similar memories
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(short_term_memory.search(
            query_embedding=query_embedding,
            limit=limit,
            threshold=0.6,
            session_id=session_id,
        ))
        loop.close()
        
        memories = [
            {
                "id": r.entry.id,
                "content": r.entry.content,
                "memory_type": r.entry.memory_type.value,
                "similarity_score": round(r.score, 3),
                "created_at": r.entry.created_at,
            }
            for r in results
        ]
        
        return json.dumps({
            "query": query,
            "session_id": session_id,
            "memories_found": len(memories),
            "memories": memories,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error recalling memory: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def get_session_history_tool(session_id: str, limit: int = 20) -> str:
    """
    Get conversation history for a session.
    
    Args:
        session_id: The session ID to get history for
        limit: Maximum number of messages to return
    
    Returns:
        JSON response with conversation history
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        history = loop.run_until_complete(
            short_term_memory.get_conversation_history(session_id, limit)
        )
        loop.close()
        
        return json.dumps({
            "session_id": session_id,
            "message_count": len(history),
            "messages": history,
        }, indent=2)
    
    except Exception as e:
        logger.error(f"Error getting session history: {e}")
        return json.dumps({"error": str(e)})


@ai_function
def clear_session_memory_tool(session_id: str) -> str:
    """
    Clear all short-term memory for a session.
    
    Args:
        session_id: The session ID to clear
    
    Returns:
        JSON response with number of entries cleared
    """
    if not short_term_memory:
        return json.dumps({"error": "Memory provider not configured"})
    
    try:
        import asyncio
        
        loop = asyncio.new_event_loop()
        count = loop.run_until_complete(short_term_memory.clear_session(session_id))
        loop.close()
        
        return json.dumps({
            "success": True,
            "session_id": session_id,
            "entries_cleared": count,
        })
    
    except Exception as e:
        logger.error(f"Error clearing session memory: {e}")
        return json.dumps({"error": str(e)})


# Create the AI Agent with tools
def create_mcp_agent():
    """Create and configure the MCP AI Agent with Microsoft Agent Framework."""
    if not FOUNDRY_PROJECT_ENDPOINT:
        logger.warning("FOUNDRY_PROJECT_ENDPOINT not configured - AI Agent will not be available")
        return None
    
    try:
        agent_credential = DefaultAzureCredential()
        client = AzureAIAgentClient(
            endpoint=FOUNDRY_PROJECT_ENDPOINT,
            credential=agent_credential,
        )
        logger.info("MCP AI Agent Client created successfully")
        return client
    except Exception as e:
        logger.error(f"Error creating AI Agent: {e}")
        return None


# Initialize the AI agent client (will be set on startup)
mcp_ai_agent = None


@dataclass
class MCPTool:
    """MCP Tool definition"""
    name: str
    description: str
    inputSchema: Dict[str, Any]


@dataclass
class MCPToolResult:
    """MCP Tool execution result"""
    content: list
    isError: bool = False


# Define MCP tools
TOOLS = [
    MCPTool(
        name="hello_mcp",
        description="Hello world MCP tool.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": []
        }
    ),
    MCPTool(
        name="get_snippet",
        description="Retrieve a snippet by name from Azure Blob Storage.",
        inputSchema={
            "type": "object",
            "properties": {
                "snippetname": {
                    "type": "string",
                    "description": "The name of the snippet to retrieve"
                }
            },
            "required": ["snippetname"]
        }
    ),
    MCPTool(
        name="save_snippet",
        description="Save a snippet with a name to Azure Blob Storage.",
        inputSchema={
            "type": "object",
            "properties": {
                "snippetname": {
                    "type": "string",
                    "description": "The name of the snippet"
                },
                "snippet": {
                    "type": "string",
                    "description": "The content of the snippet"
                }
            },
            "required": ["snippetname", "snippet"]
        }
    ),
    MCPTool(
        name="ask_foundry",
        description="Ask a question and get an answer using the Azure AI Foundry model.",
        inputSchema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the AI model"
                }
            },
            "required": ["question"]
        }
    ),
    MCPTool(
        name="next_best_action",
        description="Analyze a task using semantic reasoning with embeddings. Finds similar past tasks using cosine similarity, generates a plan of steps, and stores everything in CosmosDB for future learning. Returns task analysis, similar tasks, and planned steps.",
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description in natural language (English sentence) to analyze and plan"
                }
            },
            "required": ["task"]
        }
    ),
    MCPTool(
        name="store_memory",
        description="Store information in short-term memory for later retrieval. Useful for remembering context, user preferences, or intermediate results within a session.",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The content to remember"
                },
                "session_id": {
                    "type": "string",
                    "description": "The session ID to associate the memory with"
                },
                "memory_type": {
                    "type": "string",
                    "description": "Type of memory: context, conversation, task, or plan",
                    "enum": ["context", "conversation", "task", "plan"]
                }
            },
            "required": ["content", "session_id"]
        }
    ),
    MCPTool(
        name="recall_memory",
        description="Recall relevant memories from short-term memory based on semantic similarity. Returns memories that are contextually related to the query.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The query to search for relevant memories"
                },
                "session_id": {
                    "type": "string",
                    "description": "The session ID to search within"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default: 5)"
                }
            },
            "required": ["query", "session_id"]
        }
    ),
    MCPTool(
        name="get_session_history",
        description="Get conversation history for a session. Returns the messages exchanged in the session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to get history for"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (default: 20)"
                }
            },
            "required": ["session_id"]
        }
    ),
    MCPTool(
        name="clear_session_memory",
        description="Clear all short-term memory for a session. Use when starting fresh or cleaning up.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID to clear"
                }
            },
            "required": ["session_id"]
        }
    )
]


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
    """Execute an MCP tool"""
    try:
        if tool_name == "hello_mcp":
            return MCPToolResult(
                content=[{
                    "type": "text",
                    "text": "Hello I am MCPTool!"
                }]
            )
        
        elif tool_name == "get_snippet":
            snippet_name = arguments.get("snippetname")
            if not snippet_name:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No snippet name provided"}],
                    isError=True
                )
            
            if not blob_service_client:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Storage not configured"}],
                    isError=True
                )
            
            try:
                blob_client = blob_service_client.get_blob_client(
                    container=SNIPPETS_CONTAINER,
                    blob=f"{snippet_name}.json"
                )
                blob_data = blob_client.download_blob().readall()
                snippet_content = blob_data.decode('utf-8')
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": snippet_content
                    }]
                )
            except Exception as e:
                logger.error(f"Error retrieving snippet: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error retrieving snippet: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "save_snippet":
            snippet_name = arguments.get("snippetname")
            snippet_content = arguments.get("snippet")
            
            if not snippet_name:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No snippet name provided"}],
                    isError=True
                )
            
            if not snippet_content:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No snippet content provided"}],
                    isError=True
                )
            
            if not blob_service_client:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Storage not configured"}],
                    isError=True
                )
            
            try:
                blob_client = blob_service_client.get_blob_client(
                    container=SNIPPETS_CONTAINER,
                    blob=f"{snippet_name}.json"
                )
                blob_client.upload_blob(snippet_content.encode('utf-8'), overwrite=True)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": f"Snippet '{snippet_name}' saved successfully"
                    }]
                )
            except Exception as e:
                logger.error(f"Error saving snippet: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error saving snippet: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "ask_foundry":
            question = arguments.get("question")
            if not question:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No question provided"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Foundry endpoint not configured"}],
                    isError=True
                )
            
            try:
                from openai import AzureOpenAI
                
                credential = DefaultAzureCredential()
                # Get a token for Azure Cognitive Services
                token = credential.get_token("https://cognitiveservices.azure.com/.default")
                
                # Extract the base endpoint (remove /api/projects/proj-default if present)
                # Use the services.ai.azure.com endpoint directly
                base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
                
                logger.info(f"Using Foundry endpoint: {base_endpoint}")
                
                client = AzureOpenAI(
                    azure_endpoint=base_endpoint,
                    api_key=token.token,
                    api_version="2024-02-15-preview"
                )
                
                response = client.chat.completions.create(
                    model=FOUNDRY_MODEL_DEPLOYMENT_NAME,
                    messages=[{"role": "user", "content": question}]
                )
                
                answer = "No response generated"
                if response.choices and len(response.choices) > 0:
                    answer = response.choices[0].message.content
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": answer
                    }]
                )
            except Exception as e:
                logger.error(f"Error calling Foundry model: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error calling Foundry model: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "next_best_action":
            task = arguments.get("task")
            if not task:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No task provided"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Foundry endpoint not configured"}],
                    isError=True
                )
            
            if not cosmos_tasks_container or not cosmos_plans_container:
                return MCPToolResult(
                    content=[{"type": "text", "text": "CosmosDB not configured"}],
                    isError=True
                )
            
            try:
                task_id = str(uuid.uuid4())
                timestamp = datetime.utcnow().isoformat()
                
                # Step 1: Generate embedding for the task
                logger.info(f"Generating embedding for task: {task[:100]}...")
                task_embedding = get_embedding(task)
                
                # Step 2: Analyze intent
                logger.info("Analyzing task intent...")
                intent = analyze_intent(task)
                
                # Step 3: Find similar tasks using cosine similarity
                logger.info("Searching for similar past tasks...")
                similar_tasks = find_similar_tasks(task_embedding, threshold=0.7, limit=5)
                
                # Step 4: Generate plan based on task and similar past tasks
                logger.info("Generating execution plan...")
                plan_steps = generate_plan(task, similar_tasks)
                
                # Step 5: Store task in CosmosDB
                task_doc = {
                    'id': task_id,
                    'task': task,
                    'intent': intent,
                    'embedding': task_embedding,
                    'created_at': timestamp,
                    'similar_task_count': len(similar_tasks)
                }
                cosmos_tasks_container.upsert_item(task_doc)
                logger.info(f"Task stored in CosmosDB with id: {task_id}")
                
                # Step 6: Store plan in CosmosDB
                plan_doc = {
                    'id': str(uuid.uuid4()),
                    'taskId': task_id,
                    'task': task,
                    'intent': intent,
                    'steps': plan_steps,
                    'similar_tasks_referenced': [{'id': st['id'], 'similarity': st['similarity']} for st in similar_tasks],
                    'created_at': timestamp,
                    'status': 'planned'
                }
                cosmos_plans_container.upsert_item(plan_doc)
                logger.info(f"Plan stored in CosmosDB for task: {task_id}")
                
                # Build response
                response = {
                    'task_id': task_id,
                    'task': task,
                    'intent': intent,
                    'analysis': {
                        'similar_tasks_found': len(similar_tasks),
                        'similar_tasks': [
                            {
                                'task': st['task'],
                                'intent': st['intent'],
                                'similarity_score': round(st['similarity'], 3)
                            }
                            for st in similar_tasks
                        ]
                    },
                    'plan': {
                        'steps': plan_steps,
                        'total_steps': len(plan_steps)
                    },
                    'metadata': {
                        'created_at': timestamp,
                        'embedding_dimensions': len(task_embedding),
                        'stored_in_cosmos': True
                    }
                }
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps(response, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error in next_best_action: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error in next_best_action: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "store_memory":
            content = arguments.get("content")
            session_id = arguments.get("session_id")
            memory_type = arguments.get("memory_type", "context")
            
            if not content:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No content provided"}],
                    isError=True
                )
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            try:
                # Map string to MemoryType enum
                type_map = {
                    "context": MemoryType.CONTEXT,
                    "conversation": MemoryType.CONVERSATION,
                    "task": MemoryType.TASK,
                    "plan": MemoryType.PLAN,
                }
                mem_type = type_map.get(memory_type.lower(), MemoryType.CONTEXT)
                
                # Generate embedding for the content
                embedding = None
                if FOUNDRY_PROJECT_ENDPOINT:
                    try:
                        embedding = get_embedding(content)
                    except Exception as e:
                        logger.warning(f"Failed to generate embedding: {e}")
                
                entry = MemoryEntry(
                    id=str(uuid.uuid4()),
                    content=content,
                    memory_type=mem_type,
                    embedding=embedding,
                    session_id=session_id,
                )
                
                entry_id = await short_term_memory.store(entry)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "success": True,
                            "memory_id": entry_id,
                            "session_id": session_id,
                            "memory_type": memory_type,
                            "has_embedding": embedding is not None,
                        })
                    }]
                )
            except Exception as e:
                logger.error(f"Error storing memory: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error storing memory: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "recall_memory":
            query = arguments.get("query")
            session_id = arguments.get("session_id")
            limit = arguments.get("limit", 5)
            
            if not query:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No query provided"}],
                    isError=True
                )
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            if not FOUNDRY_PROJECT_ENDPOINT:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Foundry endpoint not configured for embeddings"}],
                    isError=True
                )
            
            try:
                query_embedding = get_embedding(query)
                
                results = await short_term_memory.search(
                    query_embedding=query_embedding,
                    limit=limit,
                    threshold=0.6,
                    session_id=session_id,
                )
                
                memories = [
                    {
                        "id": r.entry.id,
                        "content": r.entry.content,
                        "memory_type": r.entry.memory_type.value,
                        "similarity_score": round(r.score, 3),
                        "created_at": r.entry.created_at,
                    }
                    for r in results
                ]
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "query": query,
                            "session_id": session_id,
                            "memories_found": len(memories),
                            "memories": memories,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error recalling memory: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error recalling memory: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "get_session_history":
            session_id = arguments.get("session_id")
            limit = arguments.get("limit", 20)
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            try:
                history = await short_term_memory.get_conversation_history(session_id, limit)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "session_id": session_id,
                            "message_count": len(history),
                            "messages": history,
                        }, indent=2)
                    }]
                )
            except Exception as e:
                logger.error(f"Error getting session history: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error getting session history: {str(e)}"}],
                    isError=True
                )
        
        elif tool_name == "clear_session_memory":
            session_id = arguments.get("session_id")
            
            if not session_id:
                return MCPToolResult(
                    content=[{"type": "text", "text": "No session_id provided"}],
                    isError=True
                )
            
            if not short_term_memory:
                return MCPToolResult(
                    content=[{"type": "text", "text": "Memory provider not configured"}],
                    isError=True
                )
            
            try:
                count = await short_term_memory.clear_session(session_id)
                
                return MCPToolResult(
                    content=[{
                        "type": "text",
                        "text": json.dumps({
                            "success": True,
                            "session_id": session_id,
                            "entries_cleared": count,
                        })
                    }]
                )
            except Exception as e:
                logger.error(f"Error clearing session memory: {e}")
                return MCPToolResult(
                    content=[{"type": "text", "text": f"Error clearing session memory: {str(e)}"}],
                    isError=True
                )
        
        else:
            return MCPToolResult(
                content=[{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                isError=True
            )
    
    except Exception as e:
        logger.error(f"Error executing tool {tool_name}: {e}")
        return MCPToolResult(
            content=[{"type": "text", "text": f"Error: {str(e)}"}],
            isError=True
        )


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/runtime/webhooks/mcp/sse")
async def mcp_sse_endpoint(request: Request):
    """
    SSE endpoint for MCP protocol
    Establishes a long-lived connection for server-sent events
    """
    session_id = str(uuid.uuid4())
    logger.info(f"New SSE session established: {session_id}")
    
    # Store session
    sessions[session_id] = {
        "created_at": datetime.utcnow().isoformat(),
        "message_queue": asyncio.Queue()
    }
    
    async def event_generator():
        try:
            # Send initial connection event with message endpoint
            message_url = f"message?sessionId={session_id}"
            yield f"data: {message_url}\n\n"
            
            # Keep connection alive and send any queued messages
            while True:
                if session_id not in sessions:
                    break
                
                try:
                    # Wait for messages with timeout
                    message = await asyncio.wait_for(
                        sessions[session_id]["message_queue"].get(),
                        timeout=30.0
                    )
                    yield f"data: {json.dumps(message)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
                    
        except asyncio.CancelledError:
            logger.info(f"SSE connection cancelled for session {session_id}")
        finally:
            # Cleanup session
            if session_id in sessions:
                del sessions[session_id]
            logger.info(f"SSE session closed: {session_id}")
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/runtime/webhooks/mcp/message")
async def mcp_message_endpoint(request: Request):
    """
    Message endpoint for MCP protocol
    Handles JSON-RPC 2.0 requests
    """
    try:
        body = await request.json()
        logger.info(f"Received MCP message: {json.dumps(body)[:200]}")
        
        jsonrpc_version = body.get("jsonrpc")
        method = body.get("method")
        params = body.get("params", {})
        request_id = body.get("id")
        
        if jsonrpc_version != "2.0":
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": "Invalid Request"},
                    "id": request_id
                }
            )
        
        # Handle initialize
        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "mcp-server",
                        "version": "1.0.0"
                    }
                },
                "id": request_id
            }
            return JSONResponse(content=response)
        
        # Handle tools/list
        elif method == "tools/list":
            tools_list = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema
                }
                for tool in TOOLS
            ]
            
            response = {
                "jsonrpc": "2.0",
                "result": {
                    "tools": tools_list
                },
                "id": request_id
            }
            return JSONResponse(content=response)
        
        # Handle tools/call
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            # Execute the tool
            result = await execute_tool(tool_name, arguments)
            
            response = {
                "jsonrpc": "2.0",
                "result": asdict(result),
                "id": request_id
            }
            return JSONResponse(content=response)
        
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": request_id
                }
            )
    
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
                "id": body.get("id") if 'body' in locals() else None
            }
        )


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "MCP Server",
        "version": "1.0.0",
        "endpoints": {
            "sse": "/runtime/webhooks/mcp/sse",
            "message": "/runtime/webhooks/mcp/message",
            "health": "/health",
            "agent_chat": "/agent/chat"
        },
        "agent_enabled": mcp_ai_agent is not None
    }


@app.on_event("startup")
async def startup_event():
    """Initialize the AI agent and memory providers on startup."""
    global mcp_ai_agent
    
    # Initialize AI Agent
    mcp_ai_agent = create_mcp_agent()
    if mcp_ai_agent:
        logger.info("AI Agent initialized successfully on startup")
    else:
        logger.warning("AI Agent not initialized - check FOUNDRY_PROJECT_ENDPOINT configuration")
    
    # Configure embedding function for memory provider
    if short_term_memory and FOUNDRY_PROJECT_ENDPOINT:
        short_term_memory.set_embedding_function(get_embedding)
        logger.info("Memory provider embedding function configured")
    
    # Log memory provider status
    if composite_memory:
        health = await composite_memory.health_check()
        for provider, is_healthy in health.items():
            status = "healthy" if is_healthy else "unhealthy"
            logger.info(f"Memory provider '{provider}': {status}")


@app.post("/agent/chat")
async def agent_chat(request: Request):
    """
    Chat endpoint for Microsoft Agent Framework.
    Processes user messages using the AI agent with tool capabilities.
    """
    if mcp_ai_agent is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "AI Agent not available",
                "message": "Configure FOUNDRY_PROJECT_ENDPOINT and install agent-framework packages to enable AI Agent"
            }
        )
    
    try:
        body = await request.json()
        user_message = body.get("message", "")
        conversation_history = body.get("history", [])
        
        if not user_message:
            return JSONResponse(
                status_code=400,
                content={"error": "No message provided"}
            )
        
        # Build messages list for the agent
        messages = []
        
        # Add conversation history
        for hist_msg in conversation_history:
            messages.append({
                "role": hist_msg.get("role", "user"),
                "content": hist_msg.get("content", "")
            })
        
        # Add current user message
        messages.append({"role": "user", "content": user_message})
        
        # Run the agent
        response = await mcp_ai_agent.run(messages)
        
        # Extract assistant response
        assistant_responses = []
        if hasattr(response, 'messages'):
            for msg in response.messages:
                if hasattr(msg, 'role') and str(msg.role).lower() == 'assistant':
                    if hasattr(msg, 'contents'):
                        for content in msg.contents:
                            if hasattr(content, 'text'):
                                assistant_responses.append(content.text)
                    elif hasattr(msg, 'content'):
                        assistant_responses.append(str(msg.content))
        
        return JSONResponse(content={
            "response": "\n".join(assistant_responses) if assistant_responses else "No response generated",
            "message_id": str(uuid.uuid4())
        })
        
    except Exception as e:
        logger.error(f"Error in agent chat: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Agent error: {str(e)}"}
        )


@app.post("/agent/chat/stream")
async def agent_chat_stream(request: Request):
    """
    Streaming chat endpoint for Microsoft Agent Framework.
    Returns responses as Server-Sent Events for real-time streaming.
    """
    if mcp_ai_agent is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "AI Agent not available",
                "message": "Configure FOUNDRY_PROJECT_ENDPOINT and install agent-framework packages to enable AI Agent"
            }
        )
    
    try:
        body = await request.json()
        user_message = body.get("message", "")
        
        if not user_message:
            return JSONResponse(
                status_code=400,
                content={"error": "No message provided"}
            )
        
        messages = [{"role": "user", "content": user_message}]
        
        async def generate_stream():
            try:
                async for event in mcp_ai_agent.run_stream(messages):
                    if hasattr(event, 'data') and hasattr(event.data, 'contents'):
                        for content in event.data.contents:
                            if hasattr(content, 'text'):
                                yield f"data: {json.dumps({'text': content.text})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                logger.error(f"Streaming error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        
        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive"
            }
        )
        
    except Exception as e:
        logger.error(f"Error in agent chat stream: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Agent error: {str(e)}"}
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
