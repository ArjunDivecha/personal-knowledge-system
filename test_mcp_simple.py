#!/usr/bin/env python3
"""
Simple direct test of MCP server endpoints
"""
import requests
import json

MCP_URL = "https://personal-knowledge-mcp.arjun-divecha.workers.dev"

def test_server():
    print("=" * 60)
    print("MCP Server Direct Test")
    print("=" * 60)
    
    # Test 1: Root endpoint
    print("\n1. Testing root endpoint...")
    try:
        resp = requests.get(MCP_URL, timeout=5)
        print(f"   Status: {resp.status_code}")
        print(f"   Response: {resp.text}")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Test 2: SSE endpoint (get session ID)
    print("\n2. Testing SSE endpoint...")
    try:
        resp = requests.get(f"{MCP_URL}/sse", headers={"Accept": "text/event-stream"}, stream=True, timeout=3)
        for line in resp.iter_lines():
            if line:
                line = line.decode('utf-8')
                print(f"   {line}")
                if 'sessionId' in line:
                    session_id = line.split('sessionId=')[1].strip()
                    print(f"   ✓ Got session ID: {session_id[:30]}...")
                    break
        resp.close()
    except Exception as e:
        print(f"   Error: {e}")
    
    # Test 3: Initialize with proper headers
    print("\n3. Testing MCP initialization...")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    
    payload = {
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
        resp = requests.post(f"{MCP_URL}/mcp", json=payload, headers=headers, timeout=10)
        print(f"   Status: {resp.status_code}")
        print(f"   Headers: {dict(resp.headers)}")
        print(f"   Response: {resp.text[:500]}")
        
        if resp.status_code == 200:
            # Try to parse as JSON
            try:
                data = json.loads(resp.text)
                print(f"   ✓ Server: {data.get('result', {}).get('serverInfo', {}).get('name', 'unknown')}")
            except:
                print(f"   (Not JSON, likely SSE format)")
    except Exception as e:
        print(f"   Error: {e}")
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    test_server()
