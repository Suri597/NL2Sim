"""
scripts/nl_to_json.py
----------------------
Inference script — converts a natural language supply chain
description into a structured JSON configuration.

Usage (standalone):
    python nl_to_json.py description.txt output.json
    python nl_to_json.py description.txt output.json --no-context
    python nl_to_json.py description.txt output.json --azure

Usage (imported):
    from nl_to_json import generate_json
    result = generate_json(description, use_context=True, use_azure=False)
"""

import os
import re
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

from prompts import SYSTEM_INSTRUCTIONS
from schema  import SCHEMA_EXAMPLE

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# ============================================================
# Model config
# ============================================================
OPENAI_MODEL = os.environ["OPENAI_MODEL"]
AZURE_FINETUNED_MODEL = os.environ.get("AZURE_FINETUNED_MODEL", "")
AZURE_BASE_MODEL      = os.environ.get("AZURE_BASE_MODEL", "")

TEMPERATURE = 0.3


# ============================================================
# Lazy clients
# ============================================================

_openai_client = None
_azure_client  = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "OPENAI_API_KEY is not set. "
                "Add it to your .env file or export it in your terminal."
            )
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _get_azure_client():
    global _azure_client
    if _azure_client is None:
        from openai import AzureOpenAI
        api_key  = os.environ.get("AZURE_OPENAI_API_KEY", "")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        version  = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
        if not api_key or not endpoint:
            raise EnvironmentError(
                "AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be "
                "set in your .env file."
            )
        _azure_client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=version,
        )
    return _azure_client


# ============================================================
# Prompt builder
# ============================================================

def build_prompt(description: str) -> str:
    return f"""
Convert the following supply chain description into JSON structured representation following adhere to provided schema:

{SCHEMA_EXAMPLE}

Place "missing" if any information is not provided. Do not assume any information.

Supply chain description:
{description}
"""


# ============================================================
# Core function (importable by pipeline)
# ============================================================

def generate_json(
    description: str,
    use_context: bool = True,
    use_azure:   bool = False,
) -> dict:
    """
    Convert a natural language supply chain description to JSON.

    Parameters
    ----------
    description : str
        Natural language supply chain description.
    use_context : bool
        If True, include SYSTEM_INSTRUCTIONS with full schema rules.
        If False, send no system message (fewer tokens).
    use_azure : bool
        If True, use Azure OpenAI fine-tuned model.
        If False (default), use OpenAI fine-tuned model.
    """
    system_message = SYSTEM_INSTRUCTIONS if use_context else ""

    messages = [
        {"role": "system", "content": system_message},
        {"role": "user",   "content": build_prompt(description)},
    ]

    if use_azure:
        # ── Azure OpenAI ───────────────────────────────────
        client = _get_azure_client()

        # Use fine-tuned model if set, otherwise fall back to base model
        model = AZURE_FINETUNED_MODEL if AZURE_FINETUNED_MODEL else AZURE_BASE_MODEL
        if not AZURE_FINETUNED_MODEL:
            print(f"  [INFO] AZURE_FINETUNED_MODEL not set — using base model: {model}")

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=TEMPERATURE,
        )
        raw = response.choices[0].message.content.strip()

    else:
        # ── OpenAI ─────────────────────────────────────────
        response = _get_openai_client().responses.create(
            model=OPENAI_MODEL,
            temperature=TEMPERATURE,
            input=messages,
        )
        raw = response.output_text.strip()

    # ── Parse JSON ─────────────────────────────────────────
    if raw.startswith("```"):
        raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM output could not be parsed as JSON: {e}\n\nRaw output:\n{raw}"
        )


# ============================================================
# Helpers
# ============================================================

def read_description(filepath: str) -> str:
    return Path(filepath).read_text(encoding="utf-8")


def save_json(data: dict, filepath: str) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Saved → {filepath}")


# ============================================================
# CLI entry point
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert a supply chain description to JSON."
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "description_file",
        nargs="?",
        help="Path to .txt description file",
    )
    group.add_argument(
        "--text", "-t",
        help="Supply chain description as a direct string",
    )

    parser.add_argument("output_file", help="Path to save output .json file")
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Exclude system instructions (fewer tokens)",
    )
    parser.add_argument(
        "--azure",
        action="store_true",
        help="Use Azure OpenAI fine-tuned model instead of OpenAI",
    )

    args = parser.parse_args()

    description = args.text if args.text else read_description(args.description_file)
    use_context = not args.no_context
    use_azure   = args.azure

    model_label = (
        f"Azure — {AZURE_FINETUNED_MODEL or AZURE_BASE_MODEL}"
        if use_azure else OPENAI_MODEL
    )

    print(f"Model       : {model_label}")
    print(f"Temperature : {TEMPERATURE}")
    print(f"Context     : {'yes' if use_context else 'no'}")
    print("Generating...\n")

    result = generate_json(description, use_context=use_context, use_azure=use_azure)
    save_json(result, args.output_file)