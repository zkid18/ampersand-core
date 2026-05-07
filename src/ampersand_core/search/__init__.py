"""Search module — section-aware FTS5 + vector indices over the markdown vault."""

from ampersand_core.search.errors import SearchError
from ampersand_core.search.index import SearchIndex
from ampersand_core.search.indexer import SearchIndexer
from ampersand_core.search.models import Section, SearchResult
from ampersand_core.search.parser import parse_sections
from ampersand_core.search.vec_index import VectorIndex
from ampersand_core.search.vec_indexer import VectorIndexer

__all__ = [
    "SearchIndex",
    "SearchIndexer",
    "SearchResult",
    "Section",
    "SearchError",
    "VectorIndex",
    "VectorIndexer",
    "parse_sections",
]
