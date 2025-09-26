#!/usr/bin/env python3
"""
Test script to verify Claude installation
"""

import anthropic
import os

def test_claude_installation():
    """Test if Claude SDK is properly installed"""
    try:
        # Initialize the client
        client = anthropic.Anthropic(
            api_key=os.getenv('ANTHROPIC_API_KEY', 'test-key')
        )
        print("✅ Claude Python SDK installed successfully!")
        print(f"SDK Version: {anthropic.__version__}")
        return True
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def test_node_installation():
    """Test if Node.js SDK is available"""
    try:
        import subprocess
        result = subprocess.run(['node', '-e', 'console.log(require("@anthropic-ai/sdk").version)'], 
                              capture_output=True, text=True, cwd='/Users/vishalsharma/Downloads/medibuddy')
        if result.returncode == 0:
            print("✅ Claude Node.js SDK installed successfully!")
            print(f"SDK Version: {result.stdout.strip()}")
            return True
        else:
            print(f"❌ Node.js SDK error: {result.stderr}")
            return False
    except Exception as e:
        print(f"❌ Node.js test error: {e}")
        return False

if __name__ == "__main__":
    print("Testing Claude installations...")
    print("-" * 40)
    
    python_ok = test_claude_installation()
    node_ok = test_node_installation()
    
    print("-" * 40)
    if python_ok and node_ok:
        print("🎉 All installations successful!")
    else:
        print("⚠️  Some installations may need attention")

