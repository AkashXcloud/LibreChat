---
name: data-analyst
description: Perform data analysis using the existing isolated Code Interpreter capability.
---

# data-analyst

Purpose: Inspect, clean, transform, and summarize datasets using the existing `execute_code` Code Interpreter.

When to use:
- The user needs statistical analysis, trend identification, charts, or data cleaning.

Inputs:
- Datasets (files, pasted data, or structured records).

Workflow:
1. Inspect the dataset.
2. Clean data (handle nulls, remove duplicates, transform columns).
3. Calculate statistics and mathematical parameters.
4. Identify trends and summarize findings.
5. Generate charts (`.png`) or data files when appropriate.
6. Use the existing `execute_code` capability; do not implement a new sandbox.

Expected outputs:
- Analysis results, tables, `.png` charts, generated data files.

Constraints:
- Do not modify the Code Interpreter architecture.
- Use whatever execution tool is currently available.
