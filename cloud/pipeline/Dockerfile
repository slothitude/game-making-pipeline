FROM python:3.12-slim

# Install system deps (the godot-ci image pattern)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git unzip curl jq fontconfig ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Godot 4.7.1 headless (separate from the godot-ci container for lightweight use)
RUN curl -fsSL -o /tmp/godot.zip \
    "https://github.com/godotengine/godot/releases/download/4.7.1-stable/Godot_v4.7.1-stable_linux.x86_64.zip" \
    && unzip -q /tmp/godot.zip -d /tmp/godot \
    && mv /tmp/godot/Godot_v4.7.1-stable_linux.x86_64 /usr/local/bin/godot \
    && chmod +x /usr/local/bin/godot \
    && rm -rf /tmp/godot /tmp/godot.zip \
    && godot --version

WORKDIR /app

# Copy the Pipeline code
COPY cloud_editor/ /app/cloud_editor/
COPY daily/ /app/daily/
COPY journal/ /app/journal/
COPY instantiate/ /app/instantiate/
COPY schemas/ /app/schemas/

# Install Python deps (stdlib only — no external packages needed)
# cloud_editor uses only: json, os, re, subprocess, sys, time, urllib

# Create the runtime dirs
RUN mkdir -p /app/runtime /app/games

# Default: run the poll loop
CMD ["python3", "/app/cloud_editor/cloud_editor.py"]
