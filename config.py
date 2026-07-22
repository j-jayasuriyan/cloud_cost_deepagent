import os
from dotenv import load_dotenv
from langchain_aws import ChatBedrockConverse

load_dotenv()

# AWS credentials — boto3 picks these up automatically from env vars
# Required: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
# Optional: AWS_SESSION_TOKEN (for temporary/assumed-role credentials)
AWS_DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "")

_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Use model instances (not strings) so we can set a 5-min read timeout.
# DeepAgents accepts BaseChatModel directly and still detects Bedrock for prompt caching.
ORCHESTRATOR_MODEL = ChatBedrockConverse(
    model_id=_MODEL_ID,
    region_name=AWS_DEFAULT_REGION,
    timeout=300,
)

ANALYST_MODEL = ChatBedrockConverse(
    model_id=_MODEL_ID,
    region_name=AWS_DEFAULT_REGION,
    timeout=300,
)

# Target AWS account metadata (used in prompts)
# In live mode, main.py sets AWS_ACCOUNT_ID in the environment before importing agents.
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "123456789012")
AWS_REGION = AWS_DEFAULT_REGION
