# Use ./sync.sh to run.

#!/bin/bash

# Script to automatically commit and push changes with a user-provided message

echo "=========================================="
echo "Git Auto-Sync Script"
echo "=========================================="
echo ""

# Check git status
git status
echo ""

# Ask for commit message
read -p "Enter commit message: " commit_message

# Check if user entered a message
if [ -z "$commit_message" ]; then
    echo "Error: Commit message cannot be empty"
    exit 1
fi

echo ""
echo "Staging changes..."
git add .

echo "Committing with message: '$commit_message'"
git commit -m "$commit_message"

if [ $? -ne 0 ]; then
    echo "Error: Commit failed"
    exit 1
fi

echo ""
echo "Pulling latest changes from GitHub..."
git pull origin main

if [ $? -ne 0 ]; then
    echo "Error: Pull failed (you may have conflicts to resolve)"
    exit 1
fi

echo ""
echo "Pushing to GitHub..."
git push origin main

if [ $? -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo "✓ Success! Changes saved to GitHub"
    echo "=========================================="
else
    echo "Error: Push failed"
    exit 1
fi
