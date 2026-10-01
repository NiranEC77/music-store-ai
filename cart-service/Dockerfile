FROM python:3.12-slim
RUN useradd --uid 1000 --create-home app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
USER 1000
EXPOSE 5002
CMD ["python", "app.py"]
