"""ContextCleanupEngine tests are deprecated — the engine was replaced by RAG memory.
This file is kept as a placeholder so pytest doesn't error on a missing file.
The actual RAG memory tests are in test_memory_reload.py."""
import pytest

pytest.skip("ContextCleanupEngine removed — RAG memory handles cleanup automatically", allow_module_level=True)