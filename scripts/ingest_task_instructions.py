#!/usr/bin/env python3
"""
Task Instructions Ingestion Script

This script ingests task instruction documents into Azure AI Search with embeddings
for long-term memory retrieval by the next_best_action agent.

The script:
1. Creates or updates the AI Search index with vector search configuration
2. Loads task instruction JSON documents
3. Generates embeddings using text-embedding-3-large via Azure OpenAI/Foundry
4. Uploads documents to the index in chunked format

Usage:
    python ingest_task_instructions.py

Requirements:
    - azure-search-documents
    - azure-identity
    - openai
    - python-dotenv
"""

import os
import json
import uuid
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SemanticConfiguration,
    SemanticSearch,
    SemanticPrioritizedFields,
    SemanticField,
)
from openai import AzureOpenAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
AZURE_SEARCH_INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME", "task-instructions")
FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
EMBEDDING_MODEL_DEPLOYMENT_NAME = os.getenv("EMBEDDING_MODEL_DEPLOYMENT_NAME", "text-embedding-3-large")
EMBEDDING_DIMENSIONS = 3072  # text-embedding-3-large produces 3072 dimensions

# Optional: Foundry IQ Knowledge Base auto-provisioning
KNOWLEDGE_BASE_NAME = os.getenv("KNOWLEDGE_BASE_NAME", "task-instructions-kb")
KNOWLEDGE_SOURCE_NAME = os.getenv("KNOWLEDGE_SOURCE_NAME", "task-instructions-source")

# Embedding controls / fallbacks
# - "foundry" (default): uses AzureOpenAI with DefaultAzureCredential and FOUNDRY_PROJECT_ENDPOINT
# - "azure_openai": uses Azure OpenAI endpoint/key (AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY)
# - "none": skip embeddings (semantic search only)
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "foundry").lower()
SKIP_EMBEDDINGS = os.getenv("SKIP_EMBEDDINGS", "false").lower() == "true"
# Optional overrides for Azure OpenAI
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_SCOPE = os.getenv("AZURE_OPENAI_SCOPE", "https://cognitiveservices.azure.com/.default")
OPENAI_API_VERSION = os.getenv("OPENAI_API_VERSION", "")  # e.g., 2024-02-15-preview for Azure deployments via new SDK

# Path to task instruction documents (in project root)
TASK_INSTRUCTIONS_PATH = Path(__file__).parent.parent / "task_instructions"


def get_embedding_client():
    """Return a callable that takes List[str] -> List[List[float]] using configured provider."""

    if SKIP_EMBEDDINGS or EMBEDDING_PROVIDER == "none":
        logger.warning("Embeddings disabled (SKIP_EMBEDDINGS=true). Using semantic/text search only.")

        def _no_embed(texts: List[str]) -> List[List[float]]:
            return [[] for _ in texts]

        return _no_embed

    # Azure OpenAI with API key (works for both Azure OpenAI and Azure AI Foundry workspace deployments)
    if EMBEDDING_PROVIDER in ["azure_openai", "azure-openai"] and AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY:
        from openai import AzureOpenAI as _AzureOpenAI

        client = _AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_key=AZURE_OPENAI_API_KEY,
            api_version=OPENAI_API_VERSION or "2024-02-15-preview",
        )

        def _embed(texts: List[str]) -> List[List[float]]:
            return [
                client.embeddings.create(
                    model=EMBEDDING_MODEL_DEPLOYMENT_NAME,
                    input=text[:8000],
                ).data[0].embedding
                for text in texts
            ]

        logger.info("Embedding provider: Azure OpenAI (api-key)")
        return _embed

    # Default: Foundry/Azure AI project endpoint with DefaultAzureCredential (Managed + CLI + Visual Studio)
    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    # Extract base endpoint
    base_endpoint = (
        FOUNDRY_PROJECT_ENDPOINT.split("/api/projects")[0]
        if "/api/projects" in FOUNDRY_PROJECT_ENDPOINT
        else FOUNDRY_PROJECT_ENDPOINT
    )
    client_cls = AzureOpenAI  # allow test overriding via ingest.AzureOpenAI
    client = client_cls(
        azure_endpoint=base_endpoint,
        api_key=token.token,
        api_version="2024-02-15-preview",
    )

    def _resolve_fallback_endpoint() -> Optional[str]:
        """
        Try to derive the cognitiveservices endpoint from the FOUNDRY/Project endpoint.
        Example:
        - Foundry project endpoint: https://cog-xxx.services.ai.azure.com/api/projects/proj-default
        - Fallback AOAI endpoint:   https://cog-xxx.cognitiveservices.azure.com
        """
        try:
            parsed = urlparse(base_endpoint)
            host = parsed.netloc
            if host.endswith("services.ai.azure.com"):
                aoai_host = host.replace("services.ai.azure.com", "cognitiveservices.azure.com")
                return f"https://{aoai_host}"
        except Exception:
            return None
        return None

    def _create_client_with_token(endpoint: str):
        return AzureOpenAI(
            azure_endpoint=endpoint,
            api_version="2024-02-15-preview",
            azure_ad_token_provider=lambda: _get_cogservices_token(credential),
        )

    def _embed_foundry(texts: List[str]) -> List[List[float]]:
        outputs = []
        current_client = client
        fallback_endpoint = _resolve_fallback_endpoint()

        for text in texts:
            try:
                response = current_client.embeddings.create(
                    model=EMBEDDING_MODEL_DEPLOYMENT_NAME,
                    input=text[:8000],  # Truncate to avoid token limits
                )
                outputs.append(response.data[0].embedding)
            except Exception as exc:
                # Friendly guidance for common networking misconfigurations
                msg = str(exc)
                if ("Public access is disabled" in msg or "403" in msg) and fallback_endpoint:
                    logger.warning(
                        "Embedding call blocked (public access disabled). "
                        "Retrying against cognitiveservices endpoint: %s", fallback_endpoint,
                    )
                    try:
                        # Recreate client targeting cognitive services endpoint with token
                        current_client = _create_client_with_token(fallback_endpoint)
                        response = current_client.embeddings.create(
                            model=EMBEDDING_MODEL_DEPLOYMENT_NAME,
                            input=text[:8000],
                        )
                        outputs.append(response.data[0].embedding)
                        continue
                    except Exception as inner_exc:
                        logger.error(f"Fallback embedding failed: {inner_exc}")
                        # auto-fallback to azure_openai api-key if provided and not already using it
                        if AZURE_OPENAI_API_KEY and EMBEDDING_PROVIDER != "azure_openai":
                            logger.warning("Switching to EMBEDDING_PROVIDER=azure_openai (api-key) due to 403.")
                            os.environ["EMBEDDING_PROVIDER"] = "azure_openai"
                            # Recurse via Azure OpenAI provider
                            aoai_fn = get_embedding_client()
                            return aoai_fn(texts)
                        # Otherwise, propagate
                        raise
                if "public access" in msg.lower():
                    logger.error(
                        "Embedding call blocked (public access disabled). "
                        "Set EMBEDDING_PROVIDER=azure_openai with AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY, "
                        "or run from within the VNET/private endpoint.")
                raise
        return outputs
    logger.info("Embedding provider: Foundry (DefaultAzureCredential)")
    return _embed_foundry


def generate_embeddings(embed_fn, texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a list of texts using selected provider."""
    return embed_fn(texts)


def create_search_index(index_client: SearchIndexClient) -> None:
    """Create or update the AI Search index with vector search configuration."""
    
    # Define vector search configuration
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="hnsw-config",
                parameters={
                    "m": 4,
                    "efConstruction": 400,
                    "efSearch": 500,
                    "metric": "cosine"
                }
            )
        ],
        profiles=[
            VectorSearchProfile(
                name="vector-profile",
                algorithm_configuration_name="hnsw-config"
            )
        ]
    )
    
    # Define semantic search configuration
    semantic_search = SemanticSearch(
        default_configuration_name="semantic-config",
        configurations=[
            SemanticConfiguration(
                name="semantic-config",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[
                        SemanticField(field_name="content"),
                        SemanticField(field_name="description")
                    ],
                    keywords_fields=[
                        SemanticField(field_name="keywords")
                    ]
                )
            )
        ]
    )
    
    # Define index fields
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="document_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="category", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="intent", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="description", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchableField(name="keywords", type=SearchFieldDataType.String, collection=True, filterable=True),
        SimpleField(name="estimated_effort", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chunk_num", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="total_chunks", type=SearchFieldDataType.Int32),
        SimpleField(name="steps", type=SearchFieldDataType.String),  # JSON array as string
        SimpleField(name="related_tasks", type=SearchFieldDataType.String, collection=True),
        SimpleField(name="created_at", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMENSIONS,
            vector_search_profile_name="vector-profile"
        ),
    ]
    
    # Create the index
    index = SearchIndex(
        name=AZURE_SEARCH_INDEX_NAME,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search
    )
    
    try:
        result = index_client.create_or_update_index(index)
        logger.info(f"Created/updated index: {result.name}")
    except Exception as e:
        logger.error(f"Error creating index: {e}")
        raise


def _parse_project_info(project_endpoint: str) -> Optional[Dict[str, str]]:
    """Best-effort parsing of Foundry/AI Project endpoint to extract base URL and project ID."""
    if not project_endpoint:
        return None
    # Normalize
    endpoint = project_endpoint.rstrip("/")
    # Common pattern: https://<region>.api.cognitive.microsoft.com/api/projects/<projectId> or .../projects/<id>
    if "/projects/" not in endpoint:
        return None
    base, project_id = endpoint.rsplit("/projects/", 1)
    # Remove optional "/api" suffix from base
    base = base.replace("/api", "")
    return {"base": base, "project_id": project_id}


def _get_cogservices_token(credential: DefaultAzureCredential) -> str:
    """Acquire bearer token for Cognitive Services scope."""
    token = credential.get_token(AZURE_OPENAI_SCOPE)
    return token.token


def _get_search_token(credential: DefaultAzureCredential) -> str:
    """Acquire bearer token for Azure AI Search scope."""
    token = credential.get_token("https://search.azure.com/.default")
    return token.token


def ensure_knowledge_source(credential: DefaultAzureCredential) -> None:
    """
    Best-effort creation of a Knowledge Source (search index type) on the Azure AI Search service.
    Uses the 2025-11-01-preview agentic retrieval REST API.
    This lights up the Agentic retrieval > Knowledge sources UX in the Azure portal.
    It will **not** fail the ingestion process if the API contract changes; errors are logged and skipped.
    """
    try:
        if not AZURE_SEARCH_ENDPOINT:
            logger.warning("Knowledge Source provisioning skipped: AZURE_SEARCH_ENDPOINT not set")
            return

        search_base = AZURE_SEARCH_ENDPOINT.rstrip("/")
        api_version = "2025-11-01-preview"
        headers = {
            "Authorization": f"Bearer {_get_search_token(credential)}",
            "Content-Type": "application/json",
        }

        # ── 1. Check / create Knowledge Source ──────────────────────────
        ks_url = f"{search_base}/knowledgesources/{KNOWLEDGE_SOURCE_NAME}?api-version={api_version}"
        resp = requests.get(ks_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"Knowledge Source already exists: {KNOWLEDGE_SOURCE_NAME}")
        else:
            # Create knowledge source (PUT = create-or-update)
            ks_payload = {
                "name": KNOWLEDGE_SOURCE_NAME,
                "kind": "searchIndex",
                "description": "Task instructions knowledge source for agentic retrieval (auto-provisioned)",
                "searchIndexParameters": {
                    "searchIndexName": AZURE_SEARCH_INDEX_NAME,
                    "semanticConfigurationName": "semantic-config",
                    "sourceDataFields": [
                        {"name": "id"},
                        {"name": "title"},
                        {"name": "content"},
                        {"name": "category"},
                        {"name": "description"},
                    ],
                },
            }
            create_resp = requests.put(ks_url, headers=headers, json=ks_payload, timeout=30)
            if create_resp.status_code in (200, 201):
                logger.info(f"✅ Knowledge Source created: {KNOWLEDGE_SOURCE_NAME}")
            else:
                logger.warning(
                    f"Knowledge Source create returned {create_resp.status_code}: {create_resp.text}. "
                    "You can create manually in Azure portal > AI Search > Agentic retrieval > Knowledge sources."
                )
                return  # Don't try to create KB if KS failed

        # ── 2. Check / create Knowledge Base ────────────────────────────
        kb_url = f"{search_base}/knowledgebases/{KNOWLEDGE_BASE_NAME}?api-version={api_version}"
        resp = requests.get(kb_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            logger.info(f"Knowledge Base already exists: {KNOWLEDGE_BASE_NAME}")
            return

        kb_payload = {
            "name": KNOWLEDGE_BASE_NAME,
            "description": "Task instructions knowledge base for agentic retrieval (auto-provisioned)",
            "knowledgeSources": [
                {"name": KNOWLEDGE_SOURCE_NAME},
            ],
        }
        create_resp = requests.put(kb_url, headers=headers, json=kb_payload, timeout=30)
        if create_resp.status_code in (200, 201):
            logger.info(f"✅ Knowledge Base created: {KNOWLEDGE_BASE_NAME}")
        else:
            logger.warning(
                f"Knowledge Base create returned {create_resp.status_code}: {create_resp.text}. "
                "You can create manually in Azure portal > AI Search > Agentic retrieval > Knowledge bases."
            )
    except Exception as ex:
        logger.warning(f"Knowledge Source / Base provisioning skipped: {ex}")


def chunk_content(content: str, max_chunk_size: int = 4000) -> List[str]:
    """Split content into chunks while preserving structure."""
    if len(content) <= max_chunk_size:
        return [content]
    
    chunks = []
    current_chunk = ""
    
    # Split by sections (## headers)
    sections = content.split("\n## ")
    
    for i, section in enumerate(sections):
        if i > 0:
            section = "## " + section
        
        if len(current_chunk) + len(section) <= max_chunk_size:
            current_chunk += section + "\n"
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            
            # If a single section is too large, split it further
            if len(section) > max_chunk_size:
                paragraphs = section.split("\n\n")
                current_chunk = ""
                for para in paragraphs:
                    if len(current_chunk) + len(para) <= max_chunk_size:
                        current_chunk += para + "\n\n"
                    else:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                        current_chunk = para + "\n\n"
            else:
                current_chunk = section + "\n"
    
    if current_chunk:
        chunks.append(current_chunk.strip())
    
    return chunks


def load_task_instructions() -> List[Dict[str, Any]]:
    """Load all task instruction JSON files."""
    documents = []
    
    if not TASK_INSTRUCTIONS_PATH.exists():
        logger.warning(f"Task instructions path does not exist: {TASK_INSTRUCTIONS_PATH}")
        return documents
    
    for json_file in TASK_INSTRUCTIONS_PATH.glob("*.json"):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                doc = json.load(f)
                documents.append(doc)
                logger.info(f"Loaded: {json_file.name}")
        except Exception as e:
            logger.error(f"Error loading {json_file}: {e}")
    
    return documents


def sanitize_for_search(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure payload matches index schema (fix arrays vs primitive mismatches)."""

    def _to_str(val):
        try:
            return json.dumps(val, ensure_ascii=False)
        except Exception:
            return str(val)

    # Fields expected as string (per inspected index schema)
    string_fields = {"estimated_effort", "title", "category", "intent", "description", "content", "steps", "document_id", "id", "created_at", "related_tasks"}
    for field in string_fields:
        if field in payload:
            val = payload[field]
            if isinstance(val, (list, dict)):
                payload[field] = _to_str(val)
            elif val is None:
                payload[field] = ""

    # Fields expected as collection of strings (per index schema)
    collection_fields = {"keywords"}
    for field in collection_fields:
        val = payload.get(field, [])
        if val is None:
            payload[field] = []
        elif not isinstance(val, list):
            payload[field] = [str(val)]
        else:
            payload[field] = [str(v) for v in val]

    # embedding must be list[float]; if empty, keep as empty list (index is non-nullable)
    if "embedding" in payload:
        val = payload["embedding"]
        if not val:  # empty list or None
            payload["embedding"] = []
        else:
            # ensure floats
            try:
                payload["embedding"] = [float(x) for x in val]
            except Exception:
                payload["embedding"] = []

    # enforce ints for numeric fields
    for field in ["chunk_num", "total_chunks"]:
        if field in payload:
            try:
                payload[field] = int(payload[field])
            except Exception:
                payload[field] = 0

    return payload


def prepare_documents_for_indexing(
    documents: List[Dict[str, Any]],
    embed_fn,
) -> List[Dict[str, Any]]:
    """Prepare documents for indexing with embeddings."""
    indexed_docs = []
    timestamp = datetime.now(timezone.utc).isoformat()
    
    for doc in documents:
        document_id = doc.get("id", str(uuid.uuid4()))
        content = doc.get("content", "")
        
        # Chunk the content
        chunks = chunk_content(content)
        total_chunks = len(chunks)
        
        logger.info(f"Processing {document_id}: {total_chunks} chunks")
        
        for chunk_num, chunk_content_text in enumerate(chunks):
            embedding_vector: List[float] = []

            # Create text for embedding (combine title, description, and chunk)
            embedding_text = f"{doc.get('title', '')} {doc.get('description', '')} {chunk_content_text}"
            
            # Generate embedding (safe fallback if provider errors)
            try:
                if not SKIP_EMBEDDINGS:
                    vectors = generate_embeddings(embed_fn, [embedding_text])
                    embedding_vector = vectors[0] if vectors else []
            except Exception as exc:
                logger.warning(f"Embedding generation failed for doc {document_id}: {exc}")
                logger.warning("Continuing without embeddings (semantic search still available).")
                embedding_vector = []
            
            indexed_doc = {
                "id": f"{document_id}-chunk-{chunk_num}",
                "document_id": document_id,
                "title": doc.get("title", ""),
                "category": doc.get("category", ""),
                "intent": doc.get("intent", ""),
                "description": doc.get("description", ""),
                "content": chunk_content_text,
                "keywords": doc.get("keywords", []),
                "estimated_effort": doc.get("estimated_effort", ""),
                "chunk_num": chunk_num,
                "total_chunks": total_chunks,
                "steps": doc.get("steps", []),  # sanitize later
                "related_tasks": doc.get("related_tasks", []),
                "created_at": timestamp,
                "embedding": embedding_vector,
            }
            
            indexed_doc = sanitize_for_search(indexed_doc)
            indexed_docs.append(indexed_doc)
            logger.info(f"  Prepared chunk {chunk_num + 1}/{total_chunks}")
    
    return indexed_docs


def upload_documents(
    search_client: SearchClient,
    documents: List[Dict[str, Any]]
) -> None:
    """Upload documents to the search index."""
    batch_size = 100
    
    for i in range(0, len(documents), batch_size):
        batch = documents[i:i + batch_size]
        try:
            # Smith: index_documents actions expect IndexAction; upload_documents handles dictionary list
            result = search_client.upload_documents(documents=batch)
            succeeded = sum(1 for r in result if r.succeeded)
            logger.info(f"Uploaded batch {i // batch_size + 1}: {succeeded}/{len(batch)} succeeded")
        except Exception as e:
            logger.error(f"Error uploading batch at offset {i}: {e}")
            # Dump a sample payload for diagnostics
            try:
                logger.error("Sample payload: %s", json.dumps(batch[0], ensure_ascii=False)[:2000])
            except Exception:
                pass
            raise


def main():
    """Main ingestion function."""
    logger.info("=" * 60)
    logger.info("Task Instructions Ingestion Script")
    logger.info("=" * 60)
    
    # Validate configuration
    if not AZURE_SEARCH_ENDPOINT:
        logger.error("AZURE_SEARCH_ENDPOINT not configured")
        return
    
    if not FOUNDRY_PROJECT_ENDPOINT:
        logger.error("FOUNDRY_PROJECT_ENDPOINT not configured")
        return
    
    logger.info(f"Search Endpoint: {AZURE_SEARCH_ENDPOINT}")
    logger.info(f"Index Name: {AZURE_SEARCH_INDEX_NAME}")
    logger.info(f"Foundry Endpoint: {FOUNDRY_PROJECT_ENDPOINT}")
    
    # Initialize clients
    credential = DefaultAzureCredential()
    
    index_client = SearchIndexClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        credential=credential
    )
    
    search_client = SearchClient(
        endpoint=AZURE_SEARCH_ENDPOINT,
        index_name=AZURE_SEARCH_INDEX_NAME,
        credential=credential
    )
    
    # Embedding function (provider-resolved)
    embed_fn = get_embedding_client()
    
    # Create or update index
    logger.info("\n📋 Creating/updating search index...")
    create_search_index(index_client)
    
    # Load task instruction documents
    logger.info("\n📂 Loading task instruction documents...")
    documents = load_task_instructions()
    
    if not documents:
        logger.warning("No documents found to ingest")
        return
    
    logger.info(f"Found {len(documents)} documents")
    
    # Prepare documents with embeddings
    logger.info("\n🔄 Preparing documents with embeddings...")
    indexed_docs = prepare_documents_for_indexing(documents, embed_fn)
    
    logger.info(f"Prepared {len(indexed_docs)} chunks for indexing")
    
    # Upload documents
    logger.info("\n📤 Uploading documents to search index...")
    upload_documents(search_client, indexed_docs)
    
    logger.info("\n✅ Ingestion complete!")
    logger.info(f"   Total documents: {len(documents)}")
    logger.info(f"   Total chunks indexed: {len(indexed_docs)}")

    # Best-effort Knowledge Source + Knowledge Base provisioning for Agentic retrieval
    enable_kb = os.getenv("ENABLE_KNOWLEDGE_BASE_PROVISIONING", "true").lower() == "true"
    if enable_kb:
        logger.info("\n🧠 Ensuring Knowledge Source + Knowledge Base exist (Agentic retrieval)...")
        ensure_knowledge_source(credential)
    else:
        logger.info("Knowledge Base provisioning skipped (ENABLE_KNOWLEDGE_BASE_PROVISIONING=false)")


if __name__ == "__main__":
    main()
