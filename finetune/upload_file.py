"""
Azure OpenAI — Upload JSONL for Fine-Tuning
============================================
Usage:
    python upload_file.py --file my_data.jsonl

Outputs the uploaded file ID, which you can use either on the
Azure OpenAI Studio dashboard or in finetune_job.py.
"""

import argparse
import os
import sys
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()  # loads variables from .env into os.environ

# ── Configuration ─────────────────────────────────────────────────────────────
AZURE_OPENAI_API_KEY  = os.environ.get("AZURE_OPENAI_API_KEY",  "YOUR_API_KEY_HERE")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "https://<your-resource>.openai.azure.com/")
AZURE_API_VERSION     = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
# ──────────────────────────────────────────────────────────────────────────────


def upload_training_file(file_path: str) -> str:
    """Upload a JSONL file to Azure OpenAI and return its file ID."""

    if not os.path.isfile(file_path):
        print(f"[ERROR] File not found: {file_path}")
        sys.exit(1)

    client = AzureOpenAI(
        api_key=AZURE_OPENAI_API_KEY,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_version=AZURE_API_VERSION,
    )

    print(f"[INFO] Uploading '{file_path}' ...")

    with open(file_path, "rb") as f:
        response = client.files.create(file=f, purpose="fine-tune")

    file_id = response.id
    print(f"[SUCCESS] File uploaded successfully.")
    print(f"          File ID : {file_id}")
    print(f"          Status  : {response.status}")
    print()
    print("Next steps:")
    print("  • Azure dashboard : OpenAI Studio → Fine-tuning → New fine-tune job → paste the File ID above")
    print(f"  • Python script   : python finetune_job.py --training-file {file_id}")
    return file_id


def main():
    parser = argparse.ArgumentParser(description="Upload a JSONL file to Azure OpenAI for fine-tuning.")
    parser.add_argument("--file", required=True, help="Path to the .jsonl training file")
    args = parser.parse_args()

    upload_training_file(args.file)


if __name__ == "__main__":
    main()