# MCP Server Testing Guide

This guide documents approaches for testing the voitta-rag MCP server with both SSE and Streamable-HTTP transports.

## Server Info

| Transport | Endpoint | Session Handling |
|-----------|----------|------------------|
| SSE | `http://localhost:8001/sse` | Session URL in stream |
| Streamable-HTTP | `http://localhost:8001/mcp` | `Mcp-Session-Id` header |

**Protocol:** JSON-RPC 2.0 over MCP

## Quick Start: Streamable-HTTP with curl

The easiest way to test with curl is using `streamable-http` transport:

```bash
# Start server with streamable-http (modify mcp_server.py or use env var)
# mcp.run(transport="streamable-http", host=host, port=port)

# Step 1: Initialize and get session ID
SESSION_ID=$(curl -s -i -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "curl", "version": "1.0"}}}' \
  | grep -i "mcp-session-id" | awk '{print $2}' | tr -d '\r')

echo "Session: $SESSION_ID"

# Step 2: List tools
curl -s -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}'

# Step 3: Call search
curl -s -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "test", "limit": 3}}}'
```

---

## SSE Transport (Default for Claude Code)

SSE is required for Claude Code remote MCP integration.

## The Challenge with curl

SSE-based MCP requires:
1. Opening an SSE connection to `/sse`
2. Receiving a session endpoint (e.g., `/messages/?session_id=abc123`)
3. POSTing JSON-RPC requests to that session endpoint
4. Reading responses from the still-open SSE stream

This is difficult with curl alone because you need concurrent read/write operations.

## Method 1: Python with FastMCP Client (Recommended)

```python
import asyncio
from fastmcp import Client

async def test():
    async with Client("http://localhost:8001/sse") as client:
        # List available tools
        tools = await client.list_tools()
        for tool in tools:
            print(f"- {tool.name}: {tool.description[:60]}...")

        # Test search
        result = await client.call_tool("search", {
            "query": "your search query",
            "limit": 5
        })
        print(result.data)

        # Test list_indexed_folders
        folders = await client.call_tool("list_indexed_folders", {})
        print(folders.data)

        # Test get_file
        file_content = await client.call_tool("get_file", {
            "file_path": "Test Data/report.docx"
        })
        print(file_content.data)

asyncio.run(test())
```

## Method 2: Python with aiohttp (Manual)

```python
import asyncio
import json
import aiohttp

async def test_mcp():
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            "http://localhost:8001/sse",
            headers={"Accept": "text/event-stream"}
        ) as sse:
            # Get session endpoint
            session_endpoint = None
            async for line in sse.content:
                line = line.decode().strip()
                if line.startswith("data:"):
                    session_endpoint = line.replace("data: ", "").strip()
                    print(f"Session: {session_endpoint}")
                    break

            # Send tools/list request
            async with session.post(
                f"http://localhost:8001{session_endpoint}",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            ) as resp:
                print(f"Request status: {resp.status}")

            # Read response from SSE stream
            async for line in sse.content:
                line = line.decode().strip()
                if line.startswith("data:"):
                    data = json.loads(line.replace("data: ", ""))
                    if "result" in data:
                        print(json.dumps(data, indent=2))
                        break

asyncio.run(test_mcp())
```

## Method 3: curl with Background Process

```bash
# Terminal 1: Start SSE listener and capture session
TMPFILE=$(mktemp)
curl -N http://localhost:8001/sse -H "Accept: text/event-stream" | tee "$TMPFILE" &
CURL_PID=$!
sleep 2

# Get session endpoint from output
SESSION=$(grep "data:" "$TMPFILE" | head -1 | sed 's/data: //')
echo "Session: $SESSION"

# Terminal 2: Send request (while Terminal 1 is still running)
curl -X POST "http://localhost:8001${SESSION}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'

# Check Terminal 1 for response
# Cleanup
kill $CURL_PID
```

## Available Tools

### search
Semantic search across indexed documents.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "search",
    "arguments": {
      "query": "document management",
      "limit": 5,
      "include_folders": ["Test Data"],
      "exclude_folders": []
    }
  }
}
```

### list_indexed_folders
List all indexed folders with status and metadata.

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list"
}
```

### get_file
Get full content of an indexed file.

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "get_file",
    "arguments": {
      "file_path": "Test Data/report.docx"
    }
  }
}
```

## Expected Response Format

### tools/list Response
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "search",
        "description": "Search indexed documents using semantic similarity...",
        "inputSchema": {...}
      },
      {
        "name": "list_indexed_folders",
        "description": "List all folders that have been indexed...",
        "inputSchema": {...}
      },
      {
        "name": "get_file",
        "description": "Get the full content of an indexed file...",
        "inputSchema": {...}
      }
    ]
  }
}
```

### search Response
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [{
      "type": "text",
      "text": "[{\"text\":\"...\",\"score\":0.80,\"file_path\":\"...\",\"file_name\":\"...\"}]"
    }]
  }
}
```

## Quick Verification Script

Save as `test_mcp.py` and run with `python test_mcp.py`:

```python
#!/usr/bin/env python3
import asyncio
import json
from fastmcp import Client

async def main():
    print("Connecting to MCP server...")
    async with Client("http://localhost:8001/sse") as client:
        # 1. List tools
        tools = await client.list_tools()
        print(f"\n✓ {len(tools)} tools available:")
        for t in tools:
            print(f"  - {t.name}")

        # 2. Test search
        print("\n--- Search Test ---")
        result = await client.call_tool("search", {"query": "test", "limit": 2})
        if result.data:
            print(f"✓ Found {len(result.data)} results")
        else:
            print("✓ Search returned (no results)")

        # 3. List folders
        print("\n--- Folders Test ---")
        folders = await client.call_tool("list_indexed_folders", {})
        if folders.data:
            for f in folders.data:
                print(f"✓ {f['folder_path']}: {f['file_count']} files, {f['total_chunks']} chunks")

        print("\n✓ All tests passed!")

if __name__ == "__main__":
    asyncio.run(main())
```

## Troubleshooting

### "Connection refused"
Server not running. Start with:
```bash
python -m src.voitta.mcp_server
```

### "'QdrantClient' object has no attribute 'search'"
Qdrant client version mismatch. The code uses `query_points()` for qdrant-client >= 1.16.

### Timeout errors
SSE connections stay open indefinitely. Use appropriate timeouts and keep the connection alive while making requests.

### Empty results
- Check that Qdrant is running: `docker ps | grep qdrant`
- Verify data is indexed: Use `list_indexed_folders` to check chunk counts
