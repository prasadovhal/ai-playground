# Feasibility audit — 2026-10-11

## Existing work

- The six historical OpenAI/simulator input and output files are hashed in `results/historical_openai_sha256.json`. They were not regenerated in this phase.
- `results/llm_inference_raw.csv` has 1,800 saved decisions: 60 synthetic simulator scenarios, five repetitions, two information settings, three models. The revised post-pilot rubric makes accuracy exploratory.
- The previous event trace is a SQLite fault-injection trace. It contains no MCP JSON-RPC exchange or Instacart computation.
- Neither this file nor `experiment_config.json` existed at the start of this audit.

## Inputs and implementation

- No Instacart `orders.csv`, `order_products__prior.csv`, `products.csv`, or `departments.csv` was found in the project or the searched `D:\Study` tree. An unrelated `orders.csv` is a restaurant-order file. The Kaggle Python package reports that authentication is unavailable.
- The official [Python SDK migration guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md) says its 2026-07-28 Tasks extension is not implemented.
- The official [C# SDK Tasks guide](https://csharp.sdk.modelcontextprotocol.io/v2/concepts/tasks/tasks.html) documents the extension. Its v2.2.0 conformance server was built and run unmodified, with `InMemoryMcpTaskStore`.
- The official HTTP server passed a protocol smoke test. One complete run (`protocol-042cb7301691`) captured 32 JSON-RPC exchanges and passed 55 checks against the official extension schema. A saved task ID became unresolved after server process restart (`restart-dff04890aa52`). This is expected for an in-memory store and is not a defect claim.
- The sample and conformance tools perform delayed responses, not Instacart processing. No Kaggle workload trial was executed. Configuration A–D and F0–F11 are recorded as `NOT_RUN`, with denominator zero.

## Decision

Do not proceed to reliability rates, paired comparisons, or an OSS defect claim until the actual Instacart inputs and a real-workload Tasks server are available. The protocol smoke test establishes interoperability of the official C# server and the Python wire client; it does not establish application recovery.

## Subsequent resolution on 2026-10-11

The blocker above was resolved after `KAGGLE_API_TOKEN` became available through the ignored root `.env`. The original competition endpoint returned 404 and the renamed competition listed only `sample_submission.csv`; four real Instacart CSVs were downloaded through the official Kaggle API from the public Kaggle dataset `psparks/instacart-market-basket-analysis`. A custom C# server linked against the official v2.2.0 Tasks SDK processed these files through actual JSON-RPC Tasks and ordinary calls. The current measured study has 620 workload trials, 30 supported configuration/fault combinations, and 18 unsupported combinations. See `results/RESULTS.md` and `results/real_mcp_audit.json`. The earlier paragraphs describe the state before this resolution and are not the current trial status.
