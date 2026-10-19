---
name: deep-research
description: Perform structured multi-source research using available search and web capabilities.
---

# deep-research

Purpose: Conduct structured multi-source research and produce an analytical brief.

When to use:
- The user needs a research brief that searches multiple sources, cross-checks claims, and organizes findings.

Inputs:
- A research question or topic.

Workflow:
1. Understand the research question.
2. Search relevant sources using existing web/search capabilities.
3. If Tavily is configured and available, use it; if Firecrawl MCP is configured, use it.
4. Retrieve and clean relevant source content.
5. Cross-check important claims where possible.
6. Organize findings into a structured analytical brief.
7. If an integration is unavailable, gracefully fall back to available search/web capabilities.

Expected output:
- A structured research brief with cited sources.

Constraints:
- Do not add or configure Tavily, Firecrawl, or new MCP infrastructure.
- Do not hardcode MCP server URLs.
- Use whatever integrations already exist in the environment.
