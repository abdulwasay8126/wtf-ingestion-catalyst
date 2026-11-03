FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system app && useradd --system --gid app --create-home app \
    && pip install --no-cache-dir uv==0.11.28
WORKDIR /app
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
       tesseract-ocr-chi-tra poppler-utils \
    && rm -rf /var/lib/apt/lists/*
ENV OMP_THREAD_LIMIT=1
COPY tools/ocr/download_models.py /tmp/download_models.py
RUN python /tmp/download_models.py /opt/ocr-best && rm /tmp/download_models.py
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python 3.12
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY tests/fixtures ./tests/fixtures
RUN uv sync --locked --no-dev --no-editable --python 3.12 \
    && uv cache clean
RUN mkdir -p /app/outputs /app/raw && chown app:app /app/outputs /app/raw
USER app
ENTRYPOINT ["wtf-ingestion"]
CMD ["status"]
