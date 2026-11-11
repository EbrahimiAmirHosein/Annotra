FROM python:3.11-slim

LABEL org.opencontainers.image.title="Annotra" \
      org.opencontainers.image.description="Annotate images in Google Drive with boxes and polygons"

WORKDIR /app

# gcloud lets the first-run wizard create each person's Google project.
RUN apt-get update && apt-get install -y --no-install-recommends libtk8.6 curl gnupg ca-certificates fonts-dejavu-core \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py /app/
COPY docs/Annotra-single-logo.png /app/docs/

# The config folder is a volume, so credentials stay out of the image.
RUN useradd -m appuser && mkdir -p /home/appuser/.annotra && chown -R appuser:appuser /app /home/appuser
USER appuser

ENV ANNOTATOR_DOCKER=1

CMD ["python", "app.py"]
