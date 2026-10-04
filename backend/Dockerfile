FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY incident_api ./incident_api
RUN useradd --create-home appuser && mkdir /app/data && chown appuser:appuser /app/data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "--factory", "incident_api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
