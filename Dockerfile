FROM python:3-slim

WORKDIR /app
COPY . .
RUN pip install -r requirements.txt

USER nobody

CMD [ "python", "-u", "/app/standalone_server.py" ]
