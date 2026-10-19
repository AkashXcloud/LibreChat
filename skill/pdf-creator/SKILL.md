---
name: pdf-creator
description: Convert supplied content into a professional, print-ready PDF.
---

# pdf-creator

Purpose: Generate a professional `.pdf` from summaries, reports, invoices, or operational documentation.

When to use:
- The user requests a print-ready PDF with consistent margins, typography, and structured sections.

Inputs:
- Summaries, reports, architectural/system logs, invoices, documentation.

Workflow:
1. Organize content with clear sections and visual hierarchy.
2. Apply consistent margins and typography.
3. Include footer/disclaimer when required by context.
4. Generate `.pdf` using existing LibreChat execution/file-generation capabilities (`execute_code`).
5. Return the file through LibreChat's artifact mechanism.

Expected output: A downloadable `.pdf` file.

Constraints:
- Do not introduce new PDF backends or external dependencies.
