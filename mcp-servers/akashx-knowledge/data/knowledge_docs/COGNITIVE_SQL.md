# Cognitive SQL

## Overview

Cognitive SQL is a suite of AI-powered SQL functions that bring LLM capabilities directly into SQL queries. It provides synchronous cognitive functions (classify, rate, join, summarize, etc.) that can be used inline in SELECT statements.

For the full production architecture, request flows, and deployment details, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Cognitive Functions (Synchronous UDFs)

These functions are called directly in SQL queries. They batch inputs, POST to the **local** FastAPI app on the same BE node (localhost:8002), and return results inline.

| Function | Endpoint | Description |
|----------|----------|-------------|
| `cognitive_classify` | `/classify_batch` | Text classification |
| `cognitive_classify_with_reasoning` | `/classify_batch_with_reasoning` | Classification + reasoning |
| `cognitive_true_false` | `/true_false_batch` | Boolean classification |
| `cognitive_rate` | `/rate_batch` | Numeric scoring |
| `cognitive_rate_with_reasoning` | `/rate_batch_with_reasoning` | Scoring + reasoning |
| `cognitive_row_classify` | `/row_classify_batch` | Multi-field classification |
| `cognitive_summarize` | `/summarize_batch` | Summarization |
| `cognitive_multi_summarize` | `/multi_summarize_batch` | Multi-level summarization |
| `cognitive_join` | `/join_batch` | Record linkage |
| `cognitive_join_with_logprobs` | `/join_batch_with_logprobs` | Join with probabilities |
| `cognitive_join_with_reasoning` | `/join_batch_with_reasoning` | Join + reasoning |
| `cognitive_row_join` | `/row_join_batch` | Multi-field record linkage |
| `cognitive_query` | `/query_batch` | Generic question answering |

### How Synchronous UDFs Work

```
SQL query with cognitive_classify(text, prompt, labels)
        |
        v
  FE distributes fragments to BE nodes
        |
        v
  +-------------------------------+
  |  BE Node - Python UDF         |
  |  batch_sent() function        |
  |                               |
  |  1. Batch texts (max 30)      |
  |  2. POST to localhost:8002    |
  |     /classify_batch           |
  |  3. Parse JSON response       |
  |  4. Return pa.array()         |
  |     + telemetry metadata      |
  +-------------------------------+
        |
        v
  +-------------------------------+
  |  Local FastAPI App (same BE)  |
  |  process_with_cancellation()  |
  |                               |
  |  1. Route to LLM provider     |
  |  2. Call LLM API              |
  |  3. Log tokens/cost to Redis  |
  |  4. Return results JSON       |
  +-------------------------------+
        |
        v
  Results returned to BE → FE → Client
```

## LLM Provider Support

All cognitive functions support multiple LLM providers:

- **OpenAI** - GPT-4o-mini, etc.
- **Grok** - X.AI models (grok-4-fast, etc.)
- **Anthropic** - Claude models
- **Google Gemini** - Gemini models
- **Cumulus** - IonRouter
- **Local LLM** - Ollama-compatible local models

## Key Files

| Component | Path |
|-----------|------|
| Grammar | `fe/fe-core/src/main/java/com/starrocks/sql/parser/StarRocks.g4` |
| Cognitive Embed (C++) | `be/src/exprs/cognitive_functions.cpp` |
| FastAPI App | `be/UDF_STRUCTURED/app.py` |
| UDF SQL Scripts | `be/UDF_STRUCTURED/UDF_SQL_SCRIPTS/` |
