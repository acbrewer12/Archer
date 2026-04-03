FROM python:3.11-slim
 
# Install only what we need
RUN apt-get update && apt-get install -y \
    curl git ffmpeg zstd \
    && rm -rf /var/lib/apt/lists/*
 
# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh
 
# Create non-root user (HuggingFace requirement)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"
 
WORKDIR /app
 
# Install Python deps
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir flask edge-tts SpeechRecognition requests
 
# Copy app
COPY --chown=user . .
 
EXPOSE 7860
 
# Start Ollama, pull model, run Archer
CMD ollama serve & sleep 5 && python3 archer.py