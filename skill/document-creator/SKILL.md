---
name: document-creator
description: Convert unstructured text into a professional Microsoft Word .docx document.
---

# document-creator

Purpose: Produce a professional `.docx` from transcripts, meeting notes, rough documentation, or unstructured text.

When to use:
- The user needs a formatted Word document from rough content.

Inputs:
- Transcripts, speech-to-text, project notes, meeting notes, unstructured text.

Workflow:
1. Understand and organize content into logical sections.
2. Apply headings, lists, and clean formatting inconsistencies.
3. Preserve important source information.
4. Generate `.docx` using the existing execution/file-generation capability (`execute_code`).
5. Return the file through LibreChat's existing artifact mechanism.

Expected output: A downloadable `.docx` file.

Constraints:
- Use existing capabilities; do not create a new document backend.
