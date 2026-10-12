# MCP task recovery study

This project contains two separate phases: an exploratory SQLite simulator evaluation with 1,800 saved OpenAI decisions, and 620 actual Instacart workload trials over the official C# SDK v2.2.0 MCP Tasks extension. The workload uses a custom C# tool linked to the official SDK and compares ordinary MCP calls with Tasks under controlled faults. The two phases do not share outcome denominators.

The OpenAI phase is preserved and no new API calls are needed. The required Instacart CSVs came from the public Kaggle dataset `psparks/instacart-market-basket-analysis` after the renamed competition exposed only a sample submission. See [the findings](results/RESULTS.md), [protocol validation](results/protocol_validation.md), and [handoff](results/HANDOFF.md).

The notebook runs sequentially as a read-only review of historical OpenAI results, protocol traces, and real workload results. It makes no OpenAI requests. For a fresh conformance protocol smoke test, start the unmodified C# conformance server using the commands in `experiment_config.json`, then run from this directory:

```powershell
python main.py --mode protocol
```

For real data and workload work, start `server/InstacartMcp.csproj` with `INSTA_ROOT` set to this directory, then run `python main.py --mode validate-data`, `python main.py --mode instacart-smoke`, and `python main.py --mode instacart-trials --repetitions 10`. Exact build and launch commands are in `experiment_config.json`. The `data/` directory, including downloaded CSVs and artifacts, is ignored by Git. Only `data.py`, `main.py`, and `inference.py` are authored Python sources; the custom server is C#.

The `capture`, `pilot`, and `full` modes require explicit flags before they can rewrite historical simulator inputs or launch new OpenAI calls. They are not part of this phase.

The notebook `mcp_recovery_experiments.ipynb` provides sequential configuration, evaluation, and analysis cells. Change the configuration cell before paid calls. The API key comes from the root `.env` or `OPENAI_API_KEY`; `.env` is ignored by Git. Never commit credentials.

The run resumes from `results/llm_inference_raw.csv`. It does not retry failed API calls. The budget is a conservative token-cost ceiling, using uncached prices even when caching applies. API account access and model compatibility are verified for the exact requested model IDs. No substitution occurs.

Model documentation and pricing: [GPT-5.4 nano](https://developers.openai.com/api/docs/models/gpt-5.4-nano), [GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4), [pricing](https://developers.openai.com/api/docs/pricing), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses). The official model page currently marks GPT-5.4 nano deprecated; the study still requests that exact alias and records call failure if it is inaccessible.
