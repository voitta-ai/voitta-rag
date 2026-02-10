FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory '*'

WORKDIR /app

# Install dependencies only (cached unless pyproject.toml changes)
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -c "import tomllib,pathlib;d=tomllib.loads(pathlib.Path('pyproject.toml').read_text());pathlib.Path('_deps.txt').write_text('\n'.join(d['project']['dependencies']))" \
    && pip install -r _deps.txt \
    && rm _deps.txt

# Copy source and install package (fast — deps already cached)
COPY src/ src/
COPY static/ static/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps .

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "src.voitta.main:app", "--host", "0.0.0.0", "--port", "8000"]
