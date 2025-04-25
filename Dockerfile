FROM python

WORKDIR /app
COPY . .
RUN pip install -r requirements.txt

USER nobody

CMD [ "python", "/app/standalone_server.py" ]
