FROM python:3.11-slim

# Install only what we need
RUN apt-get update && apt-get install -y \
    curl git ffmpeg zstd \
    && rm -rf /var/lib/apt/lists/* \
    && curl -Lo /usr/local/bin/ttyd https://github.com/tsl0922/ttyd/releases/download/1.7.4/ttyd.x86_64 \
    && chmod +x /usr/local/bin/ttyd

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Create non-root user (HuggingFace requirement)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"

WORKDIR /app

# Install Python deps
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir flask edge-tts SpeechRecognition requests pyserial

# Copy app
COPY --chown=user . .

EXPOSE 7860

# Start Ollama, pull model, run Archer
HEALTHCHECK NONE
CMD ollama serve >/tmp/ollama.log 2>&1 & sleep 8 && PORT=7860 python3 archer.py
