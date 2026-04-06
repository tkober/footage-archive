FROM python:3.13-slim

RUN pip install --no-cache-dir mlflow

WORKDIR /mlflow

# Run:
#   docker build -t footage-mlflow mlflow/
#   docker run -p 5000:5000 -v $(pwd)/mlflow/data:/mlflow/data footage-mlflow
#
# UI at http://localhost:5000

EXPOSE 5000

CMD ["mlflow", "server", \
     "--host", "0.0.0.0", \
     "--port", "5000", \
     "--backend-store-uri", "sqlite:////mlflow/data/mlflow.db", \
     "--artifacts-destination", "/mlflow/data/artifacts", \
     "--serve-artifacts"]
