FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# system deps (optional but useful)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY realtimepythonweb.py .

# run as non-root
RUN useradd -m appuser
USER appuser

EXPOSE 5000

# Gunicorn (2 workers is plenty for 24 users)
CMD ["gunicorn","-w","2","-k","gthread","--threads","4","-t","60","-b","0.0.0.0:5000","realtimepythonweb:app"]
