# MCP servers

## filesystem
command: npx
args: -y, @modelcontextprotocol/server-filesystem, ~/agents/caldwell/raw
description: Read-only access to source documents Caldwell ingests for the wiki

## time
command: npx
args: -y, @modelcontextprotocol/server-time
description: Current date / timezone helpers — useful for tax-quarter calculations
