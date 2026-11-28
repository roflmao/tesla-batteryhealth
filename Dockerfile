FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY scripts scripts
RUN useradd -r -u 10001 bh && mkdir /data && chown bh /data
USER 10001
ENV BH_DB_PATH=/data/batteryhealth.db BH_RUN_LOG_DIR=/data/runs
EXPOSE 8000
# Single worker on purpose: the sampler runs inside the web process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
