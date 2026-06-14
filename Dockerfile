FROM python:3.14
ENV PYTHONUNBUFFERED=1
WORKDIR /oci
COPY . .
RUN pip install --use-pep517 --no-cache-dir -r requirements.txt
CMD ["python", "oracle_arm.py", "main.tf"]
