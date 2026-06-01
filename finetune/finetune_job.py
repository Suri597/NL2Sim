"""
Azure OpenAI — Create & Monitor a Fine-Tuning Job
==================================================
Usage:
    # Provide an already-uploaded file ID:
    python finetune_job.py --training-file file-abc123

    # Or upload + fine-tune in one step:
    python finetune_job.py --upload my_data.jsonl

    # With an optional validation file:
    python finetune_job.py --training-file file-abc123 --validation-file file-xyz789

    # Override hyperparameters:
    python finetune_job.py --training-file file-abc123 --epochs 4 --batch-size 4 --lr-multiplier 0.1
"""

import argparse
import os
import sys
import time
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()  # loads variables from .env into os.environ

# ── Configuration ─────────────────────────────────────────────────────────────
AZURE_OPENAI_API_KEY  = os.environ.get("AZURE_OPENAI_API_KEY",  "YOUR_API_KEY_HERE")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "https://<your-resource>.openai.azure.com/")
AZURE_API_VERSION     = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

# The base model you want to fine-tune (must be a fine-tune-capable deployment name on your resource)
BASE_MODEL = os.environ.get("AZURE_BASE_MODEL", "gpt-4.1-2025-04-14")
# ──────────────────────────────────────────────────────────────────────────────

POLL_INTERVAL = 30  # seconds between status checks


def get_client() -> AzureOpenAI:
    return AzureOpenAI(
        api_key=AZURE_OPENAI_API_KEY,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_version=AZURE_API_VERSION,
    )


def upload_file(client: AzureOpenAI, file_path: str) -> str:
    """Upload a JSONL file and return its file ID."""
    if not os.path.isfile(file_path):
        print(f"[ERROR] File not found: {file_path}")
        sys.exit(1)

    print(f"[INFO] Uploading '{file_path}' ...")
    with open(file_path, "rb") as f:
        response = client.files.create(file=f, purpose="fine-tune")

    print(f"[INFO] Uploaded. File ID: {response.id}")
    return response.id


def create_finetune_job(
    client: AzureOpenAI,
    training_file_id: str,
    validation_file_id: str | None,
    epochs: int | None,
    batch_size: int | None,
    lr_multiplier: float | None,
) -> str:
    """Submit a fine-tuning job and return the job ID."""

    hyperparameters = {}
    if epochs is not None:
        hyperparameters["n_epochs"] = epochs
    if batch_size is not None:
        hyperparameters["batch_size"] = batch_size
    if lr_multiplier is not None:
        hyperparameters["learning_rate_multiplier"] = lr_multiplier

    kwargs = dict(
        training_file=training_file_id,
        model=BASE_MODEL,
    )
    if hyperparameters:
        kwargs["hyperparameters"] = hyperparameters
    if validation_file_id:
        kwargs["validation_file"] = validation_file_id

    print(f"\n[INFO] Creating fine-tuning job ...")
    print(f"       Base model      : {BASE_MODEL}")
    print(f"       Training file   : {training_file_id}")
    if validation_file_id:
        print(f"       Validation file : {validation_file_id}")
    if hyperparameters:
        print(f"       Hyperparameters : {hyperparameters}")

    job = client.fine_tuning.jobs.create(**kwargs)

    print(f"\n[SUCCESS] Fine-tuning job created.")
    print(f"          Job ID  : {job.id}")
    print(f"          Status  : {job.status}")
    return job.id


def monitor_job(client: AzureOpenAI, job_id: str):
    """Poll the job until it completes or fails, printing status updates."""

    terminal_states = {"succeeded", "failed", "cancelled"}
    print(f"\n[INFO] Monitoring job {job_id} (polling every {POLL_INTERVAL}s) ...")
    print("       Press Ctrl+C to stop monitoring (job will keep running on Azure).\n")

    try:
        while True:
            job = client.fine_tuning.jobs.retrieve(job_id)
            status = job.status

            # Print latest event if available
            events = client.fine_tuning.jobs.list_events(fine_tuning_job_id=job_id, limit=1)
            latest_msg = ""
            for e in events.data:
                latest_msg = f" — {e.message}"
                break

            print(f"  [{time.strftime('%H:%M:%S')}] Status: {status}{latest_msg}")

            if status in terminal_states:
                break

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[INFO] Monitoring stopped. Job is still running on Azure.")
        return

    # Final summary
    job = client.fine_tuning.jobs.retrieve(job_id)
    print()
    if job.status == "succeeded":
        print("[SUCCESS] Fine-tuning completed!")
        print(f"          Fine-tuned model : {job.fine_tuned_model}")
        print()
        print("Next steps:")
        print("  1. Deploy the fine-tuned model in Azure OpenAI Studio (Deployments → Create)")
        print(f"  2. Use the deployment name in your API calls with model='{job.fine_tuned_model}'")
    elif job.status == "failed":
        print(f"[FAILED] Job failed. Error: {job.error}")
    else:
        print(f"[INFO] Job ended with status: {job.status}")


def main():
    parser = argparse.ArgumentParser(description="Create and monitor an Azure OpenAI fine-tuning job.")

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--training-file", metavar="FILE_ID",
                        help="Already-uploaded training file ID (e.g. file-abc123)")
    source.add_argument("--upload", metavar="PATH",
                        help="Path to a local .jsonl file to upload first, then fine-tune")

    parser.add_argument("--validation-file", metavar="FILE_ID",
                        help="Optional: already-uploaded validation file ID")
    parser.add_argument("--epochs",       type=int,   default=None, help="Number of training epochs")
    parser.add_argument("--batch-size",   type=int,   default=None, help="Batch size")
    parser.add_argument("--lr-multiplier",type=float, default=None, help="Learning rate multiplier")
    parser.add_argument("--no-monitor",   action="store_true",
                        help="Submit the job but don't poll for status")

    args = parser.parse_args()

    client = get_client()

    # Resolve training file ID
    if args.upload:
        training_file_id = upload_file(client, args.upload)
    else:
        training_file_id = args.training_file

    # Create the job
    job_id = create_finetune_job(
        client=client,
        training_file_id=training_file_id,
        validation_file_id=args.validation_file,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr_multiplier=args.lr_multiplier,
    )

    # Monitor unless suppressed
    if not args.no_monitor:
        monitor_job(client, job_id)
    else:
        print(f"\n[INFO] Monitoring skipped. Check status anytime with:")
        print(f"       python finetune_job.py --check {job_id}")


if __name__ == "__main__":
    main()