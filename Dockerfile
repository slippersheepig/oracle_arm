FROM python:slim
WORKDIR /oci
COPY . .
RUN pip install --use-pep517 --no-cache-dir -r requirements.txt
CMD ["python", "oracle_arm.py", "main.tf"]
