#!/usr/bin/env python3
"""
Test SSE connection like Claude Desktop would
"""
import requests
import time
import threading

def test_sse_long_connection():
    """Test SSE connection with longer duration to simulate Claude Desktop"""
    print("=" * 60)
    print("SSE Long Connection Test (Simulating Claude Desktop)")
    print("=" * 60)
    
    url = "https://personal-knowledge-mcp.arjun-divecha.workers.dev/sse"
    
    print("\n1. Establishing SSE connection...")
    try:
        resp = requests.get(url, headers={"Accept": "text/event-stream"}, stream=True, timeout=30)
        print(f"   Status: {resp.status_code}")
        print(f"   Headers: {dict(resp.headers)}")
        
        if resp.status_code != 200:
            print(f"   ✗ Failed to connect")
            return False
        
        # Read initial messages
        print("\n2. Reading initial messages...")
        message_count = 0
        start_time = time.time()
        
        for line in resp.iter_lines():
            if line:
                line = line.decode('utf-8')
                message_count += 1
                print(f"   [{message_count}] {line[:100]}")
                
                if message_count >= 5:
                    break
            
            # Timeout after 10 seconds
            if time.time() - start_time > 10:
                print(f"   Timeout after 10s")
                break
        
        resp.close()
        print(f"\n   ✓ Received {message_count} messages")
        
    except requests.exceptions.Timeout:
        print(f"   ✗ Connection timeout")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"   ✗ Connection error: {e}")
        return False
    except Exception as e:
        print(f"   ✗ Error: {e}")
        return False
    
    # Test rapid reconnection
    print("\n3. Testing rapid reconnection...")
    for i in range(3):
        try:
            resp = requests.get(url, headers={"Accept": "text/event-stream"}, stream=True, timeout=5)
            print(f"   Attempt {i+1}: Status {resp.status_code}")
            
            # Read first message
            for line in resp.iter_lines():
                if line:
                    print(f"      ✓ Got message: {line.decode('utf-8')[:80]}")
                    break
            
            resp.close()
            time.sleep(0.5)
            
        except Exception as e:
            print(f"   Attempt {i+1}: ✗ Error: {e}")
    
    print("\n" + "=" * 60)
    print("✓ SSE Connection Test Complete")
    print("=" * 60)
    return True

def test_mcp_with_keepalive():
    """Test MCP with keepalive messages"""
    print("\n" + "=" * 60)
    print("MCP Keepalive Test")
    print("=" * 60)
    
    url = "https://personal-knowledge-mcp.arjun-divecha.workers.dev/mcp"
    
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    
    # Initialize
    print("\n1. Initializing...")
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
        resp = requests.post(url, json=init_payload, headers=headers, timeout=10)
        session_id = resp.headers.get('mcp-session-id')
        print(f"   ✓ Session: {session_id[:30] if session_id else 'None'}...")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        return False
    
    # Add session ID
    headers["Mcp-Session-Id"] = session_id
    
    # Test multiple rapid requests
    print("\n2. Testing multiple rapid requests...")
    for i in range(5):
        payload = {
            "jsonrpc": "2.0",
            "id": i + 2,
            "method": "tools/call",
            "params": {
                "name": "get_index",
                "arguments": {}
            }
        }
        
        try:
            start = time.time()
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            elapsed = time.time() - start
            
            if resp.status_code == 200:
                print(f"   Request {i+1}: ✓ ({elapsed:.2f}s)")
            else:
                print(f"   Request {i+1}: ✗ Status {resp.status_code}")
        except Exception as e:
            print(f"   Request {i+1}: ✗ Error: {e}")
        
        time.sleep(0.2)
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    test_sse_long_connection()
    test_mcp_with_keepalive()
