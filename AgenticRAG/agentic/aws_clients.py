"""Lazy-initialized boto3 clients.

Cold-start cost matters in Lambda. Instantiating clients at module import
is the standard Lambda pattern: they're created once per container and
reused across warm invocations. Splitting them into one tiny module makes
patching them in tests trivial (``patch("agentic.aws_clients.bedrock_agent")``).
"""

from __future__ import annotations

import boto3

bedrock_agent = boto3.client("bedrock-agent-runtime")
ddb = boto3.resource("dynamodb")
