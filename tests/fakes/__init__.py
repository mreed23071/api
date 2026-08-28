"""Test doubles for every port the application defines.

One double per port - `LLMClient`, `EmbeddingService`, `MessageSource` - is what
lets the entire suite run with no network, no GPU, no model download and no
credentials. If a new port appears without a double here, that is the signal
that it was not designed as a port.
"""

from tests.fakes.embeddings import FakeEmbeddingService
from tests.fakes.llm import FailingLLMClient, RecordingLLMClient, ScriptedLLMClient
from tests.fakes.sources import ScriptedMessageSource
from tests.fakes.uow import FakeUnitOfWork

__all__ = [
    "FailingLLMClient",
    "FakeEmbeddingService",
    "FakeUnitOfWork",
    "RecordingLLMClient",
    "ScriptedLLMClient",
    "ScriptedMessageSource",
]
