# Container image for the Governed Plant Agent Staff Demo (Render or local Docker).
# Simulation only: rule-based agents over mock data. No AI model calls, no API key.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Run as an unprivileged user (uid 1000).
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR $HOME/app

COPY --chown=user pyproject.toml README.md ./
COPY --chown=user aiot_agent ./aiot_agent
RUN pip install --user --no-cache-dir .

# Render injects PORT at runtime; default to 7860 everywhere else.
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '7860'), timeout=3).status == 200 else 1)"

CMD ["sh", "-c", "exec python -m aiot_agent.web --host 0.0.0.0 --port \"${PORT:-7860}\""]
