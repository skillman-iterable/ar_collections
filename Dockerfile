FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt cryptography

COPY app.py .
COPY .keys/rsa_key.p8 /secrets/rsa_key.p8

ENV SNOWFLAKE_PRIVATE_KEY_PATH=/secrets/rsa_key.p8
ENV SNOWFLAKE_ACCOUNT=PP13258-ITERABLE
ENV SNOWFLAKE_USER=shawn.skillman@iterable.com
ENV SNOWFLAKE_WAREHOUSE=BILLING_PIPE
ENV PORT=8080

EXPOSE 8080

CMD ["python", "app.py"]
