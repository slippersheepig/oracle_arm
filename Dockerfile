FROM python:slim
WORKDIR /oci
COPY . .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "oracle_arm.py", "main.tf"]
