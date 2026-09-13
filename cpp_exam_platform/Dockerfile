FROM python:3.12-slim

# g++ is installed inside the hosted Linux container. Students do not need a
# compiler installed on their own computers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run the web app as an unprivileged Linux user.
RUN useradd --create-home --uid 10001 appuser
COPY . .
RUN mkdir -p /app/instance && chown -R appuser:appuser /app/instance
USER appuser

ENV PYTHONUNBUFFERED=1 \
    CPP_COMPILER=/usr/bin/g++

# Render supplies PORT at runtime (normally 10000). The shell form below lets
# us expand it while still providing a local fallback.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 100 --timeout 120 --keep-alive 5 --access-logfile - --error-logfile - --bind 0.0.0.0:${PORT:-10000} run:app"]
