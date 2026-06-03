#!/bin/bash
# Double-click this file to run the Nifty 50 analysis locally
# and open the report in your browser

cd /Users/hsarangi/Downloads/nifty-agent

echo "============================================"
echo "  Nifty 50 Daily Analysis Agent"
echo "============================================"
echo ""

# Install/update dependencies silently
echo "Checking dependencies..."
pip3 install -r requirements.txt -q

echo ""
echo "Running analysis... (this takes ~30 seconds)"
echo ""

# Run the agent (FORCE_RUN=1 skips the 7 PM time check)
FORCE_RUN=1 python3 nifty_agent.py

echo ""
echo "============================================"
echo "  Done! Opening report in browser..."
echo "============================================"

open reports/nifty-history.html

# Keep terminal window open so you can read any errors
echo ""
echo "Press any key to close this window..."
read -n 1
