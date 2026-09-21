FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 statetree
WORKDIR /app
COPY pyproject.toml ./
COPY statetree ./statetree
RUN python -m pip install --no-cache-dir . "boto3==1.43.98"
USER statetree
EXPOSE 8080
CMD ["python", "-m", "statetree.web.app"]
