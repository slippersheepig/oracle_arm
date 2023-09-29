FROM python:alpine
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "oracle_arm.py", "main.tf"]
