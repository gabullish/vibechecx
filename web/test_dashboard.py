#!/usr/bin/env python3
"""Test dashboard startup"""
import sys
sys.path.insert(0, "/home/boto/services/vibechecx/web")
try:
    import app
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    resp = client.get("/")
    print(f"Status: {resp.status_code}")
    print(f"Size: {len(resp.text)}")
    if resp.status_code != 200:
        print(resp.text[:500])
    else:
        print("OK — first 100 chars:", resp.text[:100])
except Exception as e:
    import traceback
    print(f"Error: {e}")
    traceback.print_exc()
