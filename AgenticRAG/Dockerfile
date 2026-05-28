# Container image for GIHealthcareLexFulfillment_agentic.
#
# Build (locally on arm64 or with --platform):
#   docker build --platform linux/arm64 -t gi-healthcare-lex-fulfillment-agentic .
#
# Test invoke locally (after `aws ecr get-login-password ... | docker login`):
#   docker run --rm -p 9000:8080 gi-healthcare-lex-fulfillment-agentic
#   curl "http://localhost:9000/2015-03-31/functions/function/invocations" -d @sample-event.json
#
# Push to ECR + update Lambda is wired up in Phase 3, not here.

FROM public.ecr.aws/lambda/python:3.13-arm64

# Install pinned dependencies first so this layer caches when only app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code into the Lambda task root.
COPY skills/ ${LAMBDA_TASK_ROOT}/skills/
COPY agentic/ ${LAMBDA_TASK_ROOT}/agentic/
COPY lambda_function.py ${LAMBDA_TASK_ROOT}/

# Lambda handler: <file>.<function>
CMD ["lambda_function.lambda_handler"]
