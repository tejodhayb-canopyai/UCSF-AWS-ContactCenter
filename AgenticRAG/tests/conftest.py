"""Pytest configuration shared by every test in this folder.

Two responsibilities:

1. Add the AgenticRAG root to ``sys.path`` so ``import skills``,
   ``import agentic``, and ``import lambda_function`` work without an
   editable install.

2. Set env vars and patch the boto3 clients BEFORE ``agentic`` is
   imported anywhere. ``agentic.aws_clients`` calls ``boto3.client(...)``
   at import time, which crashes without a region. ``agentic.settings``
   reads env vars at import time, so the values seen by tests are
   whatever existed when the first import happened.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# 1. sys.path setup -----------------------------------------------------------

AGENTIC_RAG_ROOT = Path(__file__).resolve().parent.parent
if str(AGENTIC_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENTIC_RAG_ROOT))

# 2. Env vars BEFORE first agentic import -------------------------------------
# Use test-only values; individual tests can override with monkeypatch.

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("KNOWLEDGE_BASE_ID", "TEST_KB_ID")
os.environ.setdefault("MODEL_ID", "amazon.nova-lite-v1:0")
os.environ.setdefault("CONVERSATION_TABLE_NAME", "")  # silently skip DDB writes


# 3. Pre-patch boto3 so import-time client construction doesn't fail ---------
# We replace the real clients in agentic.aws_clients with mocks BEFORE any
# test runs. Tests that need to assert Bedrock calls can still patch the
# .return_value of these mocks.

import agentic.aws_clients as _aws_clients  # noqa: E402  (must be after sys.path)

_aws_clients.bedrock_agent = MagicMock(name="bedrock_agent_mock")
_aws_clients.ddb = MagicMock(name="ddb_mock")
