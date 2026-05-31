"""Compatibility wrapper for legacy ``src.storage.entity_graph`` imports.

The deployable storage package now owns the entity graph implementation at
``storage.entity_graph`` so wheels include the full storage boundary.
"""

from storage.entity_graph import *  # noqa: F401,F403
from storage.entity_graph import __all__  # noqa: F401
