#!/bin/bash
# Double-click this file to pull the latest report and open it in your browser

cd /Users/hsarangi/Downloads/nifty-agent
echo "Pulling latest reports from GitHub..."
git pull
echo "Opening report..."
open reports/nifty-history.html
