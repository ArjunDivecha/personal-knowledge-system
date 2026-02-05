#!/usr/bin/env python3
"""
Test MCP server tools with proper SSE handling
"""
import requests
import json
import re

MCP_URL = "https://personal-knowledge-mcp.arjun-divecha.workers.dev"

def parse_sse_response(text):
    """Parse SSE response and extract JSON data"""
    lines = text.strip().split('\n')
    for line in lines:
        if line.startswith('data: '):
            data = line[6:]  # Remove 'data: ' prefix
            try:
                return json.loads(data)
            except:
                pass
    return None

def test_mcp_tools():
    print("=" * 60)
    print("MCP Server Tools Test")
    print("=" * 60)
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    
    # Initialize and get session ID
    print("\n1. Initializing and getting session ID...")
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"}
        }
    }
    
    try:
        resp = requests.post(f"{MCP_URL}/mcp", json=init_payload, headers=headers, timeout=10)
        session_id = resp.headers.get('mcp-session-id')
        
        if session_id:
            print(f"   ✓ Session ID: {session_id[:30]}...")
        else:
            print(f"   ✗ No session ID in response")
            return False
            
        # Parse the SSE response
        result = parse_sse_response(resp.text)
        if result and 'result' in result:
            server_info = result['result'].get('serverInfo', {})
            print(f"   ✓ Server: {server_info.get('name', 'unknown')} v{server_info.get('version', 'unknown')}")
        
    except Exception as e:
        print(f"   ✗ Error: {e}")
        return False
    
    # Add session ID to headers
    headers["Mcp-Session-Id"] = session_id
    
    # List tools
    print("\n2. Listing available tools...")
    list_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    }
    
    try:
        resp = requests.post(f"{MCP_URL}/mcp", json=list_payload, headers=headers, timeout=10)
        result = parse_sse_response(resp.text)
        
        if result and 'result' in result:
            tools = result['result'].get('tools', [])
            print(f"   ✓ Found {len(tools)} tools:")
            for tool in tools:
                print(f"      - {tool['name']}")
        else:
            print(f"   Response: {resp.text[:200]}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
    
    # Test get_index
    print("\n3. Testing get_index tool...")
    index_payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "get_index",
            "arguments": {}
        }
    }
    
    try:
        resp = requests.post(f"{MCP_URL}/mcp", json=index_payload, headers=headers, timeout=30)
        result = parse_sse_response(resp.text)
        
        if result and 'result' in result:
            content = result['result']['content'][0]['text']
            data = json.loads(content)
            print(f"   ✓ Total topics: {data.get('total_topics', 0)}")
            print(f"   ✓ Total projects: {data.get('total_projects', 0)}")
            print(f"   ✓ Showing recent: {data.get('showing_recent', {})}")
            
            # Show a few sample topics
            if 'topics' in data and data['topics']:
                print(f"\n   Sample topics:")
                for t in data['topics'][:3]:
                    print(f"      - {t['domain']}: {t['summary'][:50]}...")
        else:
            print(f"   Response: {resp.text[:300]}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        import traceback
        traceback.print_exc()
    
    # Test search
    print("\n4. Testing search tool...")
    search_payload = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {
                "query": "trading",
                "limit": 3
            }
        }
    }
    
    try:
        resp = requests.post(f"{MCP_URL}/mcp", json=search_payload, headers=headers, timeout=30)
        result = parse_sse_response(resp.text)
        
        if result and 'result' in result:
            content = result['result']['content'][0]['text']
            data = json.loads(content)
            
            if 'results' in data:
                print(f"   ✓ Found {len(data['results'])} results")
                for i, r in enumerate(data['results'][:2], 1):
                    metadata = r.get('metadata', {})
                    print(f"      {i}. Score: {r.get('final_score', 0):.3f} | Type: {metadata.get('type', 'unknown')}")
            elif 'error' in data:
                print(f"   ✗ Error: {data['error']}")
        else:
            print(f"   Response: {resp.text[:300]}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        import traceback
        traceback.print_exc()
    
    # Test get_context
    print("\n5. Testing get_context tool...")
    context_payload = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "get_context",
            "arguments": {
                "topic": "machine learning"
            }
        }
    }
    
    try:
        resp = requests.post(f"{MCP_URL}/mcp", json=context_payload, headers=headers, timeout=30)
        result = parse_sse_response(resp.text)
        
        if result and 'result' in result:
            content = result['result']['content'][0]['text']
            data = json.loads(content)
            
            if 'found' in data:
                if data['found']:
                    print(f"   ✓ Found entry: {data.get('domain', data.get('name', 'unknown'))}")
                    print(f"      State: {data.get('state', data.get('status', 'unknown'))}")
                    if 'key_insights' in data:
                        print(f"      Insights: {len(data['key_insights'])}")
                else:
                    print(f"   ✓ No entry found (expected for test query)")
            elif 'error' in data:
                print(f"   ✗ Error: {data['error']}")
        else:
            print(f"   Response: {resp.text[:300]}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("✓ MCP Server Tools Test Complete")
    print("=" * 60)

if __name__ == "__main__":
    test_mcp_tools()
