FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup --system pricewatch \
    && adduser --system --ingroup pricewatch --home /app pricewatch
COPY src ./src
RUN mkdir -p /data && chown -R pricewatch:pricewatch /app /data
USER pricewatch
CMD ["python", "-m", "pricewatch.main"]
