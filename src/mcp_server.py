"""
FastAPI MCP Server
Implements Model Context Protocol (MCP) with SSE support
Enhanced with Microsoft Agent Framework for AI agent capabilities
"""

import json
import logging
import asyncio
import uuid
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential
import os

# Microsoft Agent Framework imports
from agent_framework import ai_function, AIFunction
from agent_framework.azure import AzureAIAgentClient

from dotenv import load_dotenv

# Load environment variables
load_dotenv(override=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="MCP Server",
    description="Model Context Protocol Server for AI Agents",
    version="1.0.0"
)

# Azure Storage configuration
STORAGE_ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")

# Initialize storage client
if STORAGE_CONNECTION_STRING:
    blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
elif STORAGE_ACCOUNT_URL:
    credential = DefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url=STORAGE_ACCOUNT_URL, credential=credential)
else:
    logger.warning("No storage configuration found - snippet storage will not work")
    blob_service_client = None

SNIPPETS_CONTAINER = "snippets"

# In-memory session storage (replace with Redis for production)
sessions: Dict[str, Dict[str, Any]] = {}

# Microsoft Agent Framework configuration
FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
FOUNDRY_MODEL_DEPLOYMENT_NAME = os.getenv("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-5.2-chat")


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
    """Initialize the AI agent on startup."""
    global mcp_ai_agent
    mcp_ai_agent = create_mcp_agent()
    if mcp_ai_agent:
        logger.info("AI Agent initialized successfully on startup")
    else:
        logger.warning("AI Agent not initialized - check FOUNDRY_PROJECT_ENDPOINT configuration")


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
