# test_azure.py  — drop this in your scripts/ folder
from openai import AzureOpenAI
from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
)

response = client.chat.completions.create(
    model=os.environ.get("AZURE_BASE_MODEL", "gpt-4.1-2025-04-14"),
    messages=[{"role": "user", "content": "Reply with just: Azure connection works."}],
    max_tokens=20,
)

print(response.choices[0].message.content)