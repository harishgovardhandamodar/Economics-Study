FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY design/ ./design/
COPY README.md ./

RUN mkdir -p /app/data

EXPOSE 8304

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8304"]