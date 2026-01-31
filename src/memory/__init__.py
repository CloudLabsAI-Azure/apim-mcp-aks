"""
Memory Provider Module for MCP Server
Provides short-term (CosmosDB) and long-term (AI Search, FoundryIQ) memory abstractions

Architecture:
┌─────────────────────────────────────────────────────────────────┐
│                      CompositeMemory                            │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐    ┌─────────────────────────────────┐ │
│  │  Short-Term Memory  │    │       Long-Term Memory          │ │
│  │    (CosmosDB)       │    │   (AI Search / FoundryIQ)       │ │
│  │  - Session-based    │    │   - Persistent storage          │ │
│  │  - TTL support      │    │   - Cross-session retrieval     │ │
│  │  - Fast access      │    │   - Hybrid search               │ │
│  └─────────────────────┘    └─────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
"""

from .base import (
    MemoryProvider,
    MemoryEntry,
    MemorySearchResult,
    MemoryType,
    CompositeMemory,
)
from .cosmos_memory import ShortTermMemory
from .aisearch_memory import LongTermMemory, FoundryIQMemory

__all__ = [
    # Base classes
    "MemoryProvider",
    "MemoryEntry",
    "MemorySearchResult",
    "MemoryType",
    "CompositeMemory",
    # Short-term memory
    "ShortTermMemory",
    # Long-term memory with Foundry IQ integration
    "LongTermMemory",
    "FoundryIQMemory",
]
