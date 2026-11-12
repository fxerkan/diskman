# DiskMan konteyneri (CasaOS vb.): ana makinenin / dizini /host'a bağlanır, yollar arayüzde /host'suz görünür.
FROM python:3.13-slim
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY diskman.py cleaner.py index.html ./
ENV DISKMAN_HOSTFS=/host PYTHONUNBUFFERED=1
EXPOSE 8791
CMD ["python3", "diskman.py", "--serve", "--host", "0.0.0.0", "8791"]
