FROM python:3.9-alpine
RUN pip install -U --no-cache-dir python-snappy
RUN pip install -U --no-cache-dir https://github.com/sazima/proxynt/archive/refs/heads/master.zip
ENTRYPOINT ["nt_server", "-c", "/opt/config.json"]