FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory '*'

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY static/ static/

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "src.voitta.main:app", "--host", "0.0.0.0", "--port", "8000"]
