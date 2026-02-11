FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/

RUN mkdir -p /app/data
VOLUME /app/data

CMD ["python", "-m", "src.main", "run"]
