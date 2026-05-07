#!/usr/bin/env python3
"""
Simple test script for MCP server using HTTP requests
"""
import requests
import json
import time

MCP_URL = "https://personal-knowledge-mcp.arjun-divecha.workers.dev/mcp"

def test_mcp_server():
    print("=" * 60)
    print("Testing MCP Server")
    print("=" * 60)
    
    # Initialize session
    print("\n1. Initializing MCP session...")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "test-client",
                "version": "1.0"
            }
        }
    }
    
    try:
        response = requests.post(MCP_URL, json=init_payload, headers=headers, timeout=10)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print(f"   ✓ Server: {result['result']['serverInfo']['name']}")
            print(f"   ✓ Version: {result['result']['serverInfo']['version']}")
        else:
            print(f"   ✗ Error: {response.text}")
            return False
    except Exception as e:
        print(f"   ✗ Exception: {e}")
        return False
    
    # Get session ID from SSE endpoint
    print("\n2. Getting session ID from SSE endpoint...")
    try:
        sse_response = requests.get(
            "https://personal-knowledge-mcp.arjun-divecha.workers.dev/sse",
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=3
        )
        
        # Read first line to get session ID
        for line in sse_response.iter_lines():
            if line:
                line = line.decode('utf-8')
                if 'sessionId=' in line:
                    session_id = line.split('sessionId=')[1].strip()
                    print(f"   ✓ Session ID: {session_id[:20]}...")
                    break
            break
        
        sse_response.close()
    except Exception as e:
        print(f"   ✗ Could not get session ID: {e}")
        return False
    
    # List tools
    print("\n3. Listing available tools...")
    headers["Mcp-Session-Id"] = session_id
    
    list_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    }
    
    try:
        response = requests.post(MCP_URL, json=list_payload, headers=headers, timeout=10)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            if 'result' in result and 'tools' in result['result']:
                tools = result['result']['tools']
                print(f"   ✓ Found {len(tools)} tools:")
                for tool in tools:
                    print(f"      - {tool['name']}: {tool['description'][:60]}...")
            else:
                print(f"   Response: {json.dumps(result, indent=2)[:200]}")
        else:
            print(f"   Error: {response.text}")
    except Exception as e:
        print(f"   Exception: {e}")
    
    # Test get_index tool
    print("\n4. Testing get_index tool...")
    call_payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "get_index",
            "arguments": {}
        }
    }
    
    try:
        response = requests.post(MCP_URL, json=call_payload, headers=headers, timeout=30)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            if 'result' in result:
                content = result['result']['content'][0]['text']
                data = json.loads(content)
                print(f"   ✓ Topics: {data.get('total_topics', 0)}")
                print(f"   ✓ Projects: {data.get('total_projects', 0)}")
                print(f"   ✓ Showing: {data.get('showing_recent', {}).get('topics', 0)} topics, {data.get('showing_recent', {}).get('projects', 0)} projects")
            else:
                print(f"   Response: {json.dumps(result, indent=2)[:300]}")
        else:
            print(f"   Error: {response.text[:200]}")
    except Exception as e:
        print(f"   Exception: {e}")
    
    # Test search tool
    print("\n5. Testing search tool...")
    search_payload = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {
                "query": "machine learning",
                "limit": 3
            }
        }
    }
    
    try:
        response = requests.post(MCP_URL, json=search_payload, headers=headers, timeout=30)
        print(f"   Status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            if 'result' in result:
                content = result['result']['content'][0]['text']
                data = json.loads(content)
                if 'results' in data:
                    print(f"   ✓ Found {len(data['results'])} results")
                    for i, r in enumerate(data['results'][:2], 1):
                        metadata = r.get('metadata', {})
                        print(f"      {i}. Score: {r.get('final_score', 0):.3f} | Type: {metadata.get('type', 'unknown')}")
                else:
                    print(f"   Response: {json.dumps(data, indent=2)[:200]}")
            else:
                print(f"   Response: {json.dumps(result, indent=2)[:300]}")
        else:
            print(f"   Error: {response.text[:200]}")
    except Exception as e:
        print(f"   Exception: {e}")
    
    print("\n" + "=" * 60)
    print("✓ MCP Server Test Complete")
    print("=" * 60)

if __name__ == "__main__":
    test_mcp_server()
