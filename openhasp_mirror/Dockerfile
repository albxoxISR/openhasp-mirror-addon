FROM python:3.11-alpine

RUN pip install --no-cache-dir flask==3.1.3 requests==2.32.3 Pillow==11.2.1 paho-mqtt==1.6.1

WORKDIR /app
COPY . .

CMD ["python3", "/app/app.py"]
