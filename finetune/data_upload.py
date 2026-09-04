"""
================================================================================
  Azure OpenAI — Upload a JSONL file for Fine-Tuning
================================================================================

  WHAT THIS SCRIPT DOES:
      Uploads a .jsonl dataset file to your Azure OpenAI resource so it can
      be used for fine-tuning. After running this script, you will get a
      File ID that you can use to start a fine-tuning job.

  BEFORE YOU RUN:
      1. Install the required library by running this command in your terminal:
             pip install openai
      2. Fill in your credentials in the CONFIGURATION section below.
      3. Place your .jsonl data file in the SAME folder as this script.

  HOW TO RUN:
      Open your terminal, navigate to the folder containing this script, and run:
             python upload_file.py

  WHAT YOU WILL SEE:
      If successful, the script prints a File ID like:
             File ID : file-8455a14290de46a785643a41e5ad097a
      Save that File ID — you will need it to start the fine-tuning job.

================================================================================
"""

# ------------------------------------------------------------------------------
# IMPORTS — these are built-in Python tools and the Azure OpenAI library.
# You do not need to change anything here.
# ------------------------------------------------------------------------------
import os   # used to build the file path and check if the file exists
import sys  # used to stop the script early if something goes wrong
from openai import AzureOpenAI  # the official Azure OpenAI Python library


# ==============================================================================
#  *** CONFIGURATION — FILL IN YOUR DETAILS HERE ***
# ==============================================================================

# Your Azure OpenAI API key.
# Find it in: Azure Portal → Your OpenAI Resource → Keys and Endpoint → KEY 1
# AZURE_OPENAI_API_KEY = "YOUR_API_KEY_HERE"
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")

os.environ.get("AZURE_FINETUNED_MODEL", "")
# Your Azure OpenAI endpoint URL.
# Find it in: Azure Portal → Your OpenAI Resource → Keys and Endpoint → Endpoint
# It looks like: https://your-resource-name.openai.azure.com/
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")


# The API version — do not change this unless instructed.
AZURE_API_VERSION = os.environ.get("AZURE_API_VERSION", "")

# The name of your .jsonl file to upload.
# Make sure this file is in the SAME folder as this script.
TRAINING_FILE_NAME = os.environ.get("TRAINING_FILE_NAME", "")


# ==============================================================================


script_folder = os.path.dirname(os.path.abspath(__file__))
file_path = os.path.join(script_folder, TRAINING_FILE_NAME)


# ------------------------------------------------------------------------------
# STEP 2 — Check that the file actually exists before doing anything else.
# If the file is not found, the script will stop and print a helpful message.
# ------------------------------------------------------------------------------
if not os.path.isfile(file_path):
    print(f"[ERROR] Could not find the file: {file_path}")
    print(f"        Make sure '{TRAINING_FILE_NAME}' is in the same folder as this script.")
    sys.exit(1)  # stop the script here


# ------------------------------------------------------------------------------
# STEP 3 — Connect to your Azure OpenAI resource using your credentials.
# ------------------------------------------------------------------------------
client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_API_VERSION,
)


# ------------------------------------------------------------------------------
# STEP 4 — Upload the file.
# ------------------------------------------------------------------------------
print(f"[INFO] Uploading '{TRAINING_FILE_NAME}' to Azure OpenAI ...")

with open(file_path, "rb") as f:
    response = client.files.create(file=f, purpose="fine-tune")


# ------------------------------------------------------------------------------
# STEP 5 — Print the result.
# The File ID is what you need to start a fine-tuning job.
# Status will be "pending" at first — Azure processes it in the background,
# usually within a minute or two.
# ------------------------------------------------------------------------------
print()
print("=" * 60)
print("  [SUCCESS] File uploaded successfully!")
print("=" * 60)
print(f"  File ID : {response.id}")
print(f"  Status  : {response.status}")
print()
print("  NEXT STEPS:")
print("  1. Wait 1-2 minutes for the status to change from 'pending' to 'processed'.")
print("  2. Use the File ID above to start a fine-tuning job, either:")
print("       • Via the Azure OpenAI Studio at https://oai.azure.com")
print(f"       • Via the Python script: python finetune_job.py --training-file {response.id}")
print("=" * 60)