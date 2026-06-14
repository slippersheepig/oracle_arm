FROM python
ENV PYTHONUNBUFFERED=1
WORKDIR /oci
COPY . .
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --use-pep517 --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
CMD ["python", "oracle_arm.py", "main.tf"]
