---
name: spreadsheet-creator
description: Automate structural table compilation into a clean .xlsx spreadsheet.
---

# spreadsheet-creator

Purpose: Convert unstructured or disorganized data into a structured Microsoft Excel `.xlsx` file.

When to use:
- Raw text, CSV-like strings, or disorganized numeric records need normalization.
- The user requests a downloadable spreadsheet.

Inputs:
- Unformatted text, structured records, or numeric data.

Workflow:
1. Parse and normalize input data.
2. Identify logical columns and detect headers.
3. Normalize rows and handle missing/null values.
4. Use the existing `execute_code` Code Interpreter capability to generate a valid `.xlsx`.
5. Return the file through LibreChat's existing artifact/file mechanism.

Expected output: A downloadable `.xlsx` file.

Constraints:
- Do not invent new executors; use the existing Code Interpreter (`execute_code`).
- Handle missing values sensibly.
