# NL2Sim — Natural Language to Supply Chain Simulation

NL2Sim is an end-to-end pipeline that converts a plain English description of a supply chain into a validated simulation configuration and runs a discrete-event simulation to produce KPI results. No coding or JSON writing required — just describe your supply chain in natural language.

---

## What it does

```
Natural language description
        ↓
LLM generates structured JSON config  (OpenAI or Azure fine-tuned model)
        ↓
Pre-validation: resolve "missing" placeholders interactively
        ↓
Multi-layer validation + interactive repair
        ↓
Reliability scoring (BLEU + Numeric F1)
        ↓
Discrete-event simulation (SimPy)
        ↓
KPI results + what-if analysis
```

---

## Key features

- **NL → JSON** — fine-tuned GPT model translates plain English to a fully structured supply chain config
- **OpenAI + Azure support** — choose between OpenAI fine-tuned model or Azure OpenAI fine-tuned model at runtime
- **Pre-validation resolver** — interactively fills in any required fields the LLM left as `"missing"` before validation
- **3-layer validation** — structural, semantic, and simulation-readiness checks with interactive repair
- **Reliability scoring** — quantifies how well the generated JSON matches the original description
- **DES simulation** — SimPy-based engine supporting periodic supply, inventory threshold, demand-driven procurement, backorder and lost sales policies, resource failures, batching
- **What-if analysis** — modify any parameter in plain English and re-simulate
- **Resume-safe pipeline** — `--from-config` flag lets you skip the LLM step and resume from a saved config

---

## Project structure

```
NL2Sim_v1/
├── scripts/                        ← all runnable scripts
│   ├── run_pipeline.py             ← main CLI entry point
│   ├── nl_to_json.py               ← NL → JSON generation (OpenAI + Azure)
│   ├── iterative_repair.py         ← interactive repair runner + pre-validation pass
│   ├── score_reliability.py        ← BLEU + Numeric F1 scoring (OpenAI + Azure)
│   ├── simulate.py                 ← SimPy simulation engine
│   ├── nl_to_whatif.py             ← what-if instruction parser
│   ├── what_if_engine.py           ← applies what-if changes
│   ├── prompts.py                  ← LLM system instructions
│   ├── schema.py                   ← JSON schema example
│   ├── resolvers.py                ← interactive repair resolvers
│   ├── validation_layer_a.py       ← Layer A: structural validation
│   ├── validation_layer_b.py       ← Layer B: supply chain semantic validation
│   ├── validation_layer_c.py       ← Layer C: simulation readiness checks
│   ├── test_azure.py               ← Azure connection test script
│   └── data_gen/                   ← synthetic dataset generation
│       ├── json_generator.py
│       ├── filter_config.py
│       ├── nl_generator.py
│       ├── config_populate.py
│       ├── process_configs.py
│       ├── dataset_builder.py
│       └── format_dataset.py
├── finetune/                       ← Azure fine-tuning scripts
│   ├── upload_file.py              ← upload JSONL training data to Azure OpenAI
│   └── finetune_job.py             ← create and monitor Azure fine-tuning job
├── nl2sim/                         ← importable library
│   ├── pipeline.py                 ← Pipeline class
│   └── __init__.py
├── outputs/                        ← generated outputs (git-ignored)
├── .env.example                    ← API key template
├── requirements.txt
└── README.md
```

---

## Prerequisites

- Python 3.9+
- One of the following:
  - **OpenAI API key** (for OpenAI fine-tuned model)
  - **Azure OpenAI API key + endpoint** (for Azure fine-tuned model)

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/Suri597/NL2Sim.git
cd NL2Sim
```

**2. Create a virtual environment**

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Set up environment variables**

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials. You only need to fill in the section for the model you intend to use:

```env
# ── OpenAI (option 1) ──────────────────────────────────────
OPENAI_API_KEY=sk-...your-key-here...
OPENAI_MODEL=sk-...your-model-here...

# ── Azure OpenAI (option 2) ────────────────────────────────
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_BASE_MODEL=gpt-4.1
AZURE_FINETUNED_MODEL=your-finetuned-deployment-name
```

---

## Quick start

Run the full pipeline interactively:

```bash
cd scripts
python run_pipeline.py
```

You will be prompted to:
1. Choose which model to use (OpenAI or Azure)
2. Enter a supply chain description (from a file or directly in the terminal)
3. Choose whether to include system instructions
4. Fill in any required fields the LLM left as `"missing"`
5. Review the reliability score and decide whether to proceed
6. View simulation results
7. Optionally run what-if analyses

---

## Pipeline flags

```bash
# Skip simulation step
python run_pipeline.py --no-simulate

# Resume from an existing config (skip LLM step entirely)
python run_pipeline.py --from-config ../outputs/run_xyz/config_raw.json

# Resume and skip simulation
python run_pipeline.py --from-config ../outputs/run_xyz/config_raw.json --no-simulate

# Save outputs to a custom folder
python run_pipeline.py --output-dir ../outputs/my_run
```

---

## Example description

```
A CPU manufacturer sources microprocessors from Process Go., who delivers
with a constant 7-day lead time and charges $200 per unit. Payment is due
30 days after delivery. Microprocessors arrive daily in batches uniformly
distributed between 60,000 and 80,000 units. Memory chips are supplied by
Memory Star with a lead time uniformly between 10 and 18 days, at $100 per
unit, delivered every 14 days in batches of 1,960,000 units.

Assembly takes place at the Fab facility. Each CPU requires 1 microprocessor
and 2 memory chips. One production cycle takes 1 day. Finished CPUs are
transferred immediately to a Warehouse. Initial CPU inventory is 3,000,000
units.

Ross Associates orders CPUs every 30 days. Demand is uniformly distributed
between 1,800,000 and 2,200,000 units. Deliveries take 14 days. If inventory
cannot fully satisfy an order, available stock is shipped and the rest is lost.
CPUs are sold at $500 per unit with 30-day payment terms.

The simulation runs for 365 days with 10 replications.
```

---

## Running individual scripts

**Generate JSON from a description file**

```bash
cd scripts
python nl_to_json.py description.txt output.json           # OpenAI
python nl_to_json.py description.txt output.json --azure   # Azure
```

**Validate and repair a JSON config**

```bash
python iterative_repair.py config.json
```

**Score a config against its description**

```bash
python score_reliability.py description.txt config.json           # OpenAI
python score_reliability.py description.txt config.json --azure   # Azure
```

**Run simulation directly**

```bash
python simulate.py config.json
python simulate.py config.json --output ../outputs/results.json
```

**Test Azure connection**

```bash
python test_azure.py
```

---

## Simulation engine

The simulation engine (`simulate.py`) supports:

| Feature | Details |
|---|---|
| Procurement schemes | `periodic_supply`, `inventory_threshold`, `demand_driven` |
| Shortage policies | `backorder`, `sale_lost`, `backorder_partial`, `sale_lost_partial` |
| Resources | capacity, service time, batching, failure/repair |
| Multi-product | multiple products with different BOMs |
| Intermediate materials | multi-stage production |
| Financial KPIs | revenue, procurement cost, holding cost, shortage cost, profit, cash balance |
| Service KPIs | fill rate, units delivered, units lost, backorder |
| Inventory KPIs | average and ending inventory per material |
| Missing fields | `"missing"` placeholders and absent fields handled automatically via `normalize_config()` |

---

## JSON schema

The full schema supports the following sections:

```
config_info            → scenario name and version
raw_materials          → input materials (names)
intermediate_materials → semi-finished goods with BOM
products               → finished products with BOM
inventory              → procurement scheme, costs, initial stock
supplier               → lead time, cost, capacity, payment terms
resource               → machines with service time, batching, failure
facility               → manufacturing or warehouse, operations
customer               → demand, arrival time, shortage policy, pricing
nodes                  → supply chain graph nodes
edges                  → material flows with transfer times
simulation             → horizon, replications, warm-up, time unit
```

Full schema example is in `scripts/schema.py`.

---

## Encoding conventions

| Value | Meaning |
|---|---|
| `supplier_capacity` absent/`"missing"` | Unlimited capacity (`inf`) |
| `batch_size = 0` | Batching disabled |
| `review_time` absent/`"missing"` | Defaults to 1 time unit |
| `transfer_time` absent/`"missing"` | Immediate transfer (0 delay) |
| `warm_up` absent/`"missing"` | Defaults to 0 |
| `random_seed` absent/`"missing"` | Defaults to 12345 |
| `"missing"` | Field not provided by user — pre-validation will prompt |

---

## Validation layers

**Pre-validation pass**
Scans the filtered config for required fields still set to `"missing"` and interactively prompts the user to fill them in before any validation layer runs.

**Layer A — Structural**
Checks types, allowed values, and distribution parameters.

**Layer B — Supply chain semantics**
Checks BOMs reference known materials, every raw material has a supplier, facility operations are consistent, edges are valid.

**Layer C — Simulation readiness**
Checks producibility, inventory alignment, operation consistency, and distribution parameter constraints (e.g. uniform b > a).

If any layer finds errors, the interactive repair runner prompts you to fix them one by one using guided menus.

---

## Outputs

Each pipeline run saves to `outputs/run_{timestamp}/`:

```
config_raw.json           ← raw LLM output
config.json               ← validated and repaired config
reliability_score.json    ← BLEU + Numeric F1 scores
simulation_results.json   ← full simulation KPIs
whatif_1_changes.json     ← what-if modification spec
whatif_1_config.json      ← modified config
whatif_1_results.json     ← what-if simulation results
pipeline_summary.json     ← full run summary
```

---

## Fine-tuning on Azure OpenAI

Scripts in `finetune/` handle uploading training data and launching fine-tuning jobs on Azure OpenAI / Microsoft Foundry.

**Upload training data**

```bash
cd finetune
# Fill in credentials and TRAINING_FILE_NAME in upload_file.py, then:
python upload_file.py
```

**Start a fine-tuning job**

```bash
# Using an already-uploaded file ID
python finetune_job.py --training-file file-abc123

# Upload and fine-tune in one step
python finetune_job.py --upload ../outputs/data_gen/dataset/train.jsonl

# With validation file and custom hyperparameters
python finetune_job.py \
    --training-file file-abc123 \
    --validation-file file-xyz789 \
    --epochs 3 \
    --batch-size 4 \
    --lr-multiplier 0.2
```

Once fine-tuning completes, add the deployment name to `.env`:

```env
AZURE_FINETUNED_MODEL=your-finetuned-deployment-name
```

---

## Synthetic data generation

NL2Sim includes a pipeline for generating synthetic (NL, JSON) training pairs for fine-tuning:

```bash
cd scripts

# Step 1 — generate full JSON configs
python data_gen/config_populate.py --n 1000 --output ../outputs/data_gen/configs

# Step 2 — filter each config to relevant fields
python data_gen/filter_config.py <config_file>

# Step 3 — generate NL descriptions
python data_gen/nl_generator.py <filtered_config>

# Step 4 — apply missing placeholders
python data_gen/process_configs.py --configs-dir ../outputs/data_gen/configs

# Step 5 — assemble fine-tuning JSONL
python data_gen/dataset_builder.py --dataset-dir ../outputs/data_gen/dataset

# Step 6 — format and split train/val
python data_gen/format_dataset.py \
    --input ../outputs/data_gen/dataset/dataset.jsonl \
    --split 0.8 --validate
```

---

## Environment variables

| Variable | Required for | Description |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI model | OpenAI API key |
| `AZURE_OPENAI_API_KEY` | Azure model | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | Azure model | Azure resource endpoint URL |
| `AZURE_OPENAI_API_VERSION` | Azure model | API version (default: `2024-10-21`) |
| `AZURE_BASE_MODEL` | Azure model | Base model deployment name |
| `AZURE_FINETUNED_MODEL` | Azure model | Fine-tuned model deployment name |

---

## Dependencies

Key packages (see `requirements.txt` for full list):

```
simpy           ← discrete-event simulation
openai          ← LLM API calls (OpenAI + Azure)
python-dotenv   ← .env file loading
scipy           ← statistical distributions and confidence intervals
nltk            ← BLEU score computation
word2number     ← numeric F1 scoring
```

---

## Troubleshooting

**`OPENAI_API_KEY is not set`**
Make sure your `.env` file exists in the root directory and contains your API key.

**`AZURE_OPENAI_API_KEY is not set`**
Make sure `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT` are set in your `.env` file.

**`DeploymentNotFound` on Azure**
The model name in `AZURE_BASE_MODEL` or `AZURE_FINETUNED_MODEL` must match the exact deployment name in your Azure resource, not the model name. Check **Models + endpoints** in Microsoft Foundry.

**`ModuleNotFoundError`**
Make sure you are running from inside the `scripts/` directory and your virtual environment is activated.

**Reliability score fails with quota error**
The pipeline will ask if you want to skip the score and continue to simulation. Press `1` to skip.

**Validation errors after generation**
The interactive repair runner will guide you through fixing them. Press `2` to skip any issue not relevant to your scenario.
