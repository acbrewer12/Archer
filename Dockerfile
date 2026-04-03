FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl nodejs npm git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Create user (HuggingFace requires non-root)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# Install Python dependencies
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir flask edge-tts SpeechRecognition requests

# Copy app
COPY --chown=user . .

# Expose port
EXPOSE 7860

# Start Ollama and Archer
CMD ollama serve & sleep 8 && ollama pull llama3.2 && python3 archer.py
