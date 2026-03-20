FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client \
    libxcb1 libx11-6 libxext6 libxrender1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory '*'

WORKDIR /app

# Install dependencies only (cached unless pyproject.toml changes)
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -c "import tomllib,pathlib;d=tomllib.loads(pathlib.Path('pyproject.toml').read_text());pathlib.Path('_deps.txt').write_text('\n'.join(d['project']['dependencies']))" \
    && pip install -r _deps.txt \
    && rm _deps.txt

# Install MinerU for PDF parsing (separate step — heavy dependency tree)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "mineru[all]" pymupdf

# Symlink so the subprocess path (.mineru-venv/bin/python) resolves in Docker
RUN mkdir -p /app/.mineru-venv/bin && ln -s /usr/local/bin/python3 /app/.mineru-venv/bin/python

# Copy source and install package (fast — deps already cached)
COPY src/ src/
COPY static/ static/
COPY scripts/ scripts/
COPY entrypoint.sh .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps .

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "-m", "uvicorn", "src.voitta.main:app", "--host", "0.0.0.0", "--port", "8000"]
