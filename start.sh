#!/bin/bash
# Start the Multi-Agent Bug Detection & Auto PR System (Streamlit UI)

cd "$(dirname "$0")"

# Create a virtual environment if needed
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate

# Install/update dependencies
echo "Installing Python dependencies..."
pip install -q -r backend/requirements.txt 2>/dev/null || echo "Note: some external CLIs (semgrep, bandit, pylint, eslint) may need manual installation."

echo "Starting Streamlit app at http://localhost:8501 ..."
echo ""

streamlit run streamlit_app.py
