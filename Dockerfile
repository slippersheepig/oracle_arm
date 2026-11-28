FROM python
ENV PYTHONUNBUFFERED=1
WORKDIR /oci
COPY requirements.txt .
RUN pip install --use-pep517 --no-cache-dir -r requirements.txt
COPY oracle_arm.py .
CMD ["python", "oracle_arm.py", "main.tf"]
