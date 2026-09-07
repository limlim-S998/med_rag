FROM python:3.11-slim
WORKDIR /app
COPY services/reranker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the weights into the image. A model downloaded at pod start is a model
# that can differ between replicas and can fail your readiness probe.
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"
COPY services/reranker/app /app/app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
