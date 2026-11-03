FROM python:3.12-slim
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends \
    python3 python3-pil tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
ENV OMP_THREAD_LIMIT=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /trial
COPY recognize.py /trial/recognize.py
USER nobody
ENTRYPOINT ["/usr/bin/python3", "/trial/recognize.py"]
