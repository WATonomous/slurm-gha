#!/bin/bash

# Lint script for local development
# Usage: ./scripts/lint.sh [--fix]

set -e

echo "🔍 Running linting checks..."

# Add common uv installation paths to PATH
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "❌ uv is not installed. Please install it first:"
    echo "   curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    echo "After installation, you may need to add ~/.local/bin to your PATH:"
    echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "📦 Creating virtual environment..."
    uv venv
fi

# Install ruff for linting
echo "📦 Installing ruff..."
uv pip install ruff

# Activate virtual environment and run ruff
echo "🔍 Running ruff check..."
if [[ "$1" == "--fix" ]]; then
    echo "🔧 Auto-fixing issues..."
    source .venv/bin/activate && ruff check . --fix
else
    source .venv/bin/activate && ruff check .
fi

# Run ruff format
echo "🎨 Running ruff format..."
if [[ "$1" == "--fix" ]]; then
    echo "🔧 Auto-formatting code..."
    source .venv/bin/activate && ruff format .
else
    source .venv/bin/activate && ruff format --check .
fi

echo "✅ Linting completed successfully!"
