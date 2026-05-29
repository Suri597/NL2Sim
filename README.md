# NL2Sim — Natural Language to Supply Chain Simulation

NL2Sim is an end-to-end pipeline that converts a plain English description of a supply chain into a validated simulation configuration and runs a discrete-event simulation to produce KPI results. No coding or JSON writing required — just describe your supply chain in natural language.

---

## What it does

```
Natural language description
        ↓
LLM generates structured JSON config
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

- **NL → JSON** — GPT-powered translation from plain English to a fully structured supply chain config
- **3-layer validation** — structural, semantic, and simulation-readiness checks with interactive repair
- **Reliability scoring** — quantifies how well the generated JSON matches the original description
- **DES simulation** — SimPy-based engine supporting periodic supply, inventory threshold, demand-driven procurement, backorder and lost sales policies, resource failures, batching
- **What-if analysis** — modify any parameter in plain English and re-simulate
- **Resume-safe pipeline** — interrupted runs resume from where they left off

---

## Project structure

```
NL2Sim_v1/
├── scripts/                   ← all runnable scripts
│   ├── run_pipeline.py        ← main CLI entry point
│   ├── nl_to_json.py          ← NL → JSON generation
│   ├── validate.py            ← run all validation layers
│   ├── iterative_repair.py    ← interactive repair runner
│   ├── score_reliability.py   ← BLEU + Numeric F1 scoring
│   ├── simulate.py            ← SimPy simulation engine
│   ├── nl_to_whatif.py        ← what-if instruction parser
│   ├── what_if_engine.py      ← applies what-if changes
│   ├── prompts.py             ← LLM system instructions
│   ├── schema.py              ← JSON schema example
│   ├── resolvers.py           ← interactive repair resolvers
│   ├── validation_layer_a.py  ← structural validation
│   ├── validation_layer_b.py  ← supply chain semantic validation
│   ├── validation_layer_c.py  ← simulation readiness checks
│   └── data_gen/              ← synthetic dataset generation
│       ├── json_generator.py
│       ├── filter_config.py
│       ├── nl_generator.py
│       ├── config_populate.py
│       ├── process_configs.py
│       ├── dataset_builder.py
│       └── format_dataset.py
├── nl2sim/                    ← importable library
│   ├── pipeline.py            ← Pipeline class
│   └── __init__.py
├── outputs/                   ← generated outputs (git-ignored)
├── .env.example               ← API key template
├── requirements.txt
└── README.md
```

---

## Prerequisites

- Python 3.9+
- An OpenAI API key

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

**4. Set up your API key**

```bash
cp .env.example .env
```

Open `.env` and add your OpenAI API key:

```
OPENAI_API_KEY=sk-...your-key-here...
```

---

## Quick start

Run the full pipeline interactively:

```bash
cd scripts
python run_pipeline.py
```

You will be prompted to:
1. Enter a supply chain description (from a file or directly in the terminal)
2. Choose whether to include system instructions
3. Review the reliability score and decide whether to proceed
4. View simulation results
5. Optionally run what-if analyses

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
python nl_to_json.py description.txt output.json
```

**Validate and repair a JSON config**

```bash
python iterative_repair.py config.json
```

**Score a config against its description**

```bash
python score_reliability.py description.txt config.json
```

**Run simulation directly**

```bash
python simulate.py config.json
```

**Run what-if analysis**

```bash
python nl_to_whatif.py "increase supplier capacity of Process Go. to 500000" config.json
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

---

## JSON schema

The full schema supports the following sections:

```
config_info       → scenario name and version
raw_materials     → input materials (names)
intermediate_materials → semi-finished goods with BOM
products          → finished products with BOM
inventory         → procurement scheme, costs, initial stock
supplier          → lead time, cost, capacity, payment terms
resource          → machines with service time, batching, failure
facility          → manufacturing or warehouse, operations
customer          → demand, arrival time, shortage policy, pricing
nodes             → supply chain graph nodes
edges             → material flows with transfer times
simulation        → horizon, replications, warm-up, time unit
```

Full schema example is in `scripts/schema.py`.

---

## Encoding conventions

| Value | Meaning |
|---|---|
| `supplier_capacity = 0` | Unlimited capacity |
| `batch_size = 0` | Batching disabled |
| `review_time = 0` | Defaults to 1 time unit |
| `transfer_time.a = 0` | Immediate transfer |
| `"missing"` | Field not provided — validation will prompt user |

---

## Validation layers

**Layer A — Structural**
Checks types, required fields, allowed values, distribution parameters.

**Layer B — Supply chain semantics**
Checks BOMs reference known materials, every raw material has a supplier, facility operations are consistent, edges are valid.

**Layer C — Simulation readiness**
Checks producibility, inventory alignment, operation consistency.

If any layer finds errors the interactive repair runner prompts you to fix them one by one using guided menus.

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

## Synthetic data generation (Optional, dataset already included. See instruction below for more information)

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

## Using the dataset for fine-tuning

The dataset is provided as a single JSONL file at `outputs/data_gen/dataset/dataset.jsonl`.
Each line is one training instance in OpenAI fine-tuning format:

```json
{
  "messages": [
    {"role": "user",      "content": "instruction prefix + NL description + schema"},
    {"role": "assistant", "content": "{...full JSON config with missing placeholders...}"}
  ]
}
```

### Step 1 — Sample and split

Use `format_dataset.py` to randomly sample a subset and split into train/validation sets:

```bash
cd scripts

# sample 800 from 1000, split 80/20 train/val, validate format
python data_gen/format_dataset.py \
    --input ../outputs/data_gen/dataset/dataset.jsonl \
    --sample 800 \
    --split 0.8 \
    --validate

# use all 1000, split 80/20
python data_gen/format_dataset.py \
    --input ../outputs/data_gen/dataset/dataset.jsonl \
    --split 0.8 \
    --validate

# no split — single formatted file
python data_gen/format_dataset.py \
    --input ../outputs/data_gen/dataset/dataset.jsonl \
    --no-split
```

Output files are saved next to the input with descriptive names:

The 8-character ID at the end matches train and val as a pair.

### Step 2 — Upload to OpenAI for fine-tuning

### Step 3 — Update model in pipeline

Once fine-tuning completes update the model in `scripts/nl_to_json.py`:

```python
MODEL = "ft:gpt-4.1-2025-04-14:personal:nl2sim:XXXXXXXX"
```

### format_dataset.py options

| Flag | Default | Description |
|---|---|---|
| `--input` | required | Path to dataset.jsonl |
| `--sample` | all | Randomly sample N records before splitting |
| `--split` | 0.8 | Train/validation ratio |
| `--no-split` | off | Save single file without splitting |
| `--validate` | off | Validate OpenAI format before saving |
| `--seed` | 42 | Random seed for sampling and splitting |
| `--output-dir` | same as input | Directory to save output files |
---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key for LLM calls |

---

## Dependencies

Key packages (see `requirements.txt` for full list):

```
simpy           ← discrete-event simulation
openai          ← LLM API calls
python-dotenv   ← .env file loading
scipy           ← statistical distributions
nltk            ← BLEU score computation
numpy           ← numerical operations
```

---

## Troubleshooting

**`OPENAI_API_KEY is not set`**
Make sure you have a `.env` file in the root directory with your API key.

**`ModuleNotFoundError`**
Make sure you are running from inside the `scripts/` directory and your virtual environment is activated.

**Validation errors after generation**
The interactive repair runner will guide you through fixing them. Press `2` to skip any issue that is not relevant to your scenario.

**Simulation results seem wrong**
Check that procurement types are correct (`periodic_supply` vs `demand_driven`) and that supplier capacity is `0` for unlimited.

---


---

## License

MIT License — see `LICENSE` for details.
