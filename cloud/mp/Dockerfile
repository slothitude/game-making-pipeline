# cloud/mp — Cloud Run multiplayer skeleton (G3).
# Cloud Run law: the container MUST bind 0.0.0.0:$PORT ($PORT injected by the
# platform). No volume mounts, no build step beyond pip.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

EXPOSE 8080
# --local is NOT set here: production binds 0.0.0.0 and expects MP_TOKEN set.
CMD ["sh", "-c", "exec python main.py --host 0.0.0.0 --port ${PORT:-8080}"]
