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
from datetime import datetime

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

# Path to task instruction documents (in project root)
TASK_INSTRUCTIONS_PATH = Path(__file__).parent.parent / "task_instructions"


def get_azure_openai_client() -> AzureOpenAI:
    """Initialize Azure OpenAI client with managed identity."""
    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    
    # Extract base endpoint
    base_endpoint = FOUNDRY_PROJECT_ENDPOINT.split('/api/projects')[0] if '/api/projects' in FOUNDRY_PROJECT_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
    
    return AzureOpenAI(
        azure_endpoint=base_endpoint,
        api_key=token.token,
        api_version="2024-02-15-preview"
    )


def generate_embeddings(client: AzureOpenAI, texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a list of texts using text-embedding-3-large."""
    embeddings = []
    
    for text in texts:
        response = client.embeddings.create(
            model=EMBEDDING_MODEL_DEPLOYMENT_NAME,
            input=text[:8000]  # Truncate to avoid token limits
        )
        embeddings.append(response.data[0].embedding)
    
    return embeddings


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


def prepare_documents_for_indexing(
    documents: List[Dict[str, Any]],
    openai_client: AzureOpenAI
) -> List[Dict[str, Any]]:
    """Prepare documents for indexing with embeddings."""
    indexed_docs = []
    timestamp = datetime.utcnow().isoformat() + "Z"
    
    for doc in documents:
        document_id = doc.get("id", str(uuid.uuid4()))
        content = doc.get("content", "")
        
        # Chunk the content
        chunks = chunk_content(content)
        total_chunks = len(chunks)
        
        logger.info(f"Processing {document_id}: {total_chunks} chunks")
        
        for chunk_num, chunk_content_text in enumerate(chunks):
            # Create text for embedding (combine title, description, and chunk)
            embedding_text = f"{doc.get('title', '')} {doc.get('description', '')} {chunk_content_text}"
            
            # Generate embedding
            embeddings = generate_embeddings(openai_client, [embedding_text])
            
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
                "steps": json.dumps(doc.get("steps", [])),
                "related_tasks": doc.get("related_tasks", []),
                "created_at": timestamp,
                "embedding": embeddings[0]
            }
            
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
            result = search_client.upload_documents(documents=batch)
            succeeded = sum(1 for r in result if r.succeeded)
            logger.info(f"Uploaded batch {i // batch_size + 1}: {succeeded}/{len(batch)} succeeded")
        except Exception as e:
            logger.error(f"Error uploading batch: {e}")
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
    
    openai_client = get_azure_openai_client()
    
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
    indexed_docs = prepare_documents_for_indexing(documents, openai_client)
    
    logger.info(f"Prepared {len(indexed_docs)} chunks for indexing")
    
    # Upload documents
    logger.info("\n📤 Uploading documents to search index...")
    upload_documents(search_client, indexed_docs)
    
    logger.info("\n✅ Ingestion complete!")
    logger.info(f"   Total documents: {len(documents)}")
    logger.info(f"   Total chunks indexed: {len(indexed_docs)}")


if __name__ == "__main__":
    main()
