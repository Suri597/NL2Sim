"""
scripts/nl_to_json.py
----------------------
Inference script — converts a natural language supply chain
description into a structured JSON configuration.

Usage (standalone):
    python nl_to_json.py description.txt output.json
    python nl_to_json.py description.txt output.json --no-schema

Usage (imported):
    from nl_to_json import generate_json
    result = generate_json(description, use_schema=True)
"""

import os
import re
import json
import sys
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

from prompts import SYSTEM_INSTRUCTIONS
from schema  import SCHEMA_EXAMPLE



load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# ── Model config ───────────────────────────────────────────
MODEL       = "ft:gpt-4.1-2025-04-14:personal:nl2sim-ft:Dkz8vnRw"
# MODEL       = "gpt-5.4"
TEMPERATURE = 0.3

# ── Lazy client — only initialised when generate_json() is called ──
_client = None

def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "OPENAI_API_KEY is not set. "
                "Add it to your .env file or export it in your terminal."
            )
        _client = OpenAI(api_key=api_key)
    return _client


# ── Prompt builder ─────────────────────────────────────────

def build_prompt(description: str) -> str:
    return f"""
Convert the following supply chain description into JSON.

{SCHEMA_EXAMPLE}

Place "missing" if any information is not provided. Do not assume any information.

Supply chain description:
{description}
"""


# ── Core function (importable by pipeline) ─────────────────

def generate_json(description: str, use_context: bool = True) -> dict:
    """
    Parameters
    ----------
    use_context : bool
        If True (default), include SYSTEM_INSTRUCTIONS with full schema rules.
        If False, send no system message — rely on the schema example only.
        SCHEMA_EXAMPLE is always included in the user prompt regardless.
    """
    system_message = SYSTEM_INSTRUCTIONS if use_context else ""

    messages = [
        {"role": "system", "content": system_message},
        {"role": "user",   "content": build_prompt(description)},
    ]

    response = _get_client().responses.create(
        model=MODEL,
        temperature=TEMPERATURE,
        input=messages,
    )

    raw = response.output_text.strip()

    if raw.startswith("```"):
        raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM output could not be parsed as JSON: {e}\n\nRaw output:\n{raw}"
        )


# ── Helpers ────────────────────────────────────────────────

def read_description(filepath: str) -> str:
    return Path(filepath).read_text(encoding="utf-8")


def save_json(data: dict, filepath: str) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Saved → {filepath}")


# ── CLI entry point ────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert a supply chain description to JSON."
    )

    # Input — either a file OR a direct string
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
        help="Exclude system instructions — rely on schema example only (fewer tokens)",
    )

    args = parser.parse_args()

    # Load description from file or direct string
    if args.text:
        description = args.text
    else:
        description = read_description(args.description_file)

    use_context = not args.no_context

    print(f"Model       : {MODEL}")
    print(f"Temperature : {TEMPERATURE}")
    print(f"Context     : {'yes' if use_context else 'no'}")
    print("Generating...\n")

    result = generate_json(description, use_context=use_context)
    save_json(result, args.output_file)